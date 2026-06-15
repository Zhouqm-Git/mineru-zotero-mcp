"""Parse one Zotero PDF via MinerU and persist artifacts to the vault.

Orchestrates: zotero_bridge (PDF path + citekey) → mineru_client (extract) →
table_normalizer (tables → MD) → anchor_generator (bbox map) → store (atomic
write to .raw/<citekey>/ and user-visible figures to attachments/papers/<citekey>/).

Replaces vspdf/src/parse-and-persist.ts. Key differences from the TS original:
  - Input is item_key/citekey (not a workspace-relative docId).
  - Output dir is <vault>/.raw/<citekey>/ (not .docnotes/parsed/).
  - Tables are normalized to GFM Markdown (not left as dual HTML+image).
  - Cache key is PDF content hash (not mtime).
  - Image paths rewritten to attachments/papers/<citekey>/<name>
    (vault-relative, Obsidian-reachable).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .anchor_generator import generate_anchors
from .figure_merger import merge_fragmented_figures
from .mineru_client import ExtractedZip, MineruClient, MineruError, TaskResult
from .store import (
    anchors_path,
    assets_dir,
    content_path,
    ensure_dir,
    is_cached,
    md_path,
    meta_path,
    now_ms,
    pdf_content_hash,
    raw_dir,
    sanitize_citekey,
    to_vault_relative,
    write_bytes,
    write_json,
    write_text,
)
from .table_normalizer import normalize_table_body
from .types import ContentItem, ParseMeta, ParseResult
from .zotero_bridge import get_pdf_path_for_item, resolve_identifier

logger = logging.getLogger(__name__)

# Matches vspdf parse-and-persist.ts:217 image-reference regex.
_IMG_REF_RE = re.compile(
    r"!\[([^\]]*)\]\((?:images/)?([^)]+\.(?:jpg|jpeg|png|gif|webp|svg))\)",
    re.IGNORECASE,
)


def _rewrite_image_paths(markdown: str, assets_relative: str) -> str:
    """Rewrite MinerU's `images/x.jpg` refs to `<assets>/x.jpg`."""
    def repl(m: re.Match[str]) -> str:
        alt, filename = m.group(1), m.group(2)
        # filename may already be bare ("x.jpg") — keep basename only.
        basename = filename.rsplit("/", 1)[-1]
        return f"![{alt}]({assets_relative}/{basename})"

    return _IMG_REF_RE.sub(repl, markdown)


def _inject_page_markers(markdown: str, content_list: list[ContentItem]) -> str:
    """Insert `<!-- Page N -->` before the first meaningful text of each page.

    Ported from vspdf/src/parse-and-persist.ts:234-268. Inserts in descending
    page order so earlier offsets aren't invalidated as we edit.
    """
    # First ≥20-char text on each page (0-based page_idx).
    page_first_text: dict[int, str] = {}
    for item in content_list:
        page_idx = item.page_idx or 0
        if page_idx in page_first_text:
            continue
        text = (item.text or "").strip()
        if len(text) >= 20:
            page_first_text[page_idx] = text

    result = markdown
    for page_idx in sorted(page_first_text.keys(), reverse=True):
        first_text = page_first_text[page_idx]
        # Escape regex metachars in the search string; take first 50 chars.
        needle = re.escape(first_text[:50])
        m = re.search(needle, result, flags=re.MULTILINE)
        if m:
            marker = f"<!-- Page {page_idx + 1} -->\n\n"
            result = result[: m.start()] + marker + result[m.start():]

    if "<!-- Page 1 -->" not in result:
        result = "<!-- Page 1 -->\n\n" + result
    return result


def _persist_images(
    extracted: ExtractedZip, assets_directory: Path, assets_relative: str
) -> tuple[int, dict[str, str]]:
    """Write every image from the zip into the paper attachment dir."""
    ensure_dir(assets_directory)
    image_map: dict[str, str] = {}
    for basename, data in extracted.images.items():
        out = assets_directory / basename
        write_bytes(out, data)
        image_map[basename] = basename
        image_map[f"images/{basename}"] = basename
    return len(extracted.images), image_map


def _persist_content_with_tables(
    extracted: ExtractedZip, markdown: str, assets_relative: str
) -> tuple[str, list[ContentItem], int, int, int, list[ContentItem]]:
    """Rewrite tables in content_list to markdown and patch the markdown body.

    Returns:
        final_markdown, content_items (parsed), image_count, table_count,
        page_count, raw_content_list (for anchors).
    """
    raw_items: list[dict] = extracted.content_list or []
    content_items = [ContentItem.from_raw(c) for c in raw_items]

    # Normalize tables in the markdown body. MinerU drops a `<table>` block where
    # the table sits; we additionally make sure each table block is the GFM form.
    table_count = 0
    for item in content_items:
        if item.type == "table":
            table_count += 1

    # We do NOT attempt to splice GFM tables back into the markdown by position —
    # MinerU's markdown already contains the table region. Instead we ensure any
    # HTML <table> embedded in the markdown is converted to GFM in place, so text
    # LLMs see pure markdown. Per-tableBodyHtml is preserved on the anchor for
    # agents that want the raw structure.
    final_markdown = _convert_inline_html_tables(markdown)

    final_markdown = _rewrite_image_paths(final_markdown, assets_relative)

    page_count = len({c.page_idx for c in content_items}) if content_items else 0
    image_count = sum(1 for c in content_items if c.type == "image")

    if content_items:
        final_markdown = _inject_page_markers(final_markdown, content_items)

    return final_markdown, content_items, image_count, table_count, page_count, content_items


_HTML_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)


def _convert_inline_html_tables(markdown: str) -> str:
    """Replace every <table>...</table> in the markdown with its GFM equivalent."""
    def repl(m: re.Match[str]) -> str:
        html = m.group(0)
        block, _kept = normalize_table_body(html, caption=None)
        return block

    return _HTML_TABLE_RE.sub(repl, markdown)


# ─── public entry point ─────────────────────────────────────────


def parse_pdf(
    *,
    vault_root: str | Path,
    client: MineruClient,
    item_key: str | None = None,
    citekey: str | None = None,
    model_version: str = "vlm",
    enable_table: bool = True,
    enable_formula: bool = True,
    language: str = "ch",
    page_ranges: str | None = None,
    force: bool = False,
    poll_timeout_s: float = 300.0,
) -> ParseResult:
    """Parse one Zotero PDF via MinerU and persist artifacts into the vault.

    Hidden parse artifacts go to <vault>/.raw/<citekey>/. Figures intended for
    note embeds go to <vault>/attachments/papers/<citekey>/.

    Provide either item_key or citekey; the other is resolved via zotero_bridge.
    """
    vault_root = Path(vault_root)
    item_key, citekey = resolve_identifier(item_key, citekey)
    if not citekey:
        raise MineruError(
            "Could not resolve a citation key for this item. "
            "Is BetterBibTeX running, or is the citekey recorded in the item's 'extra' field?"
        )
    safe_key = sanitize_citekey(citekey)

    # Resolve PDF path via zotero_bridge (reuses LocalZoteroReader).
    if not item_key:
        raise MineruError("item_key is required to locate the PDF attachment")
    pdf_path = get_pdf_path_for_item(item_key)
    if not pdf_path:
        raise MineruError(
            f"No local PDF attachment found for item {item_key}. "
            "Ensure ZOTERO_LOCAL=true and the PDF is downloaded in Zotero."
        )

    # Cache check (content hash).
    if not force:
        cached = is_cached(vault_root, citekey, pdf_path)
        if cached:
            logger.info("Cache hit for %s (citekey=%s)", pdf_path.name, citekey)
            return _result_from_cache(vault_root, citekey, item_key, str(pdf_path), cached)

    # Submit + wait.
    batch_id = client.parse_local_file(
        pdf_path,
        data_id=safe_key,
        model_version=model_version,
        enable_table=enable_table,
        enable_formula=enable_formula,
        language=language,
        page_ranges=page_ranges,
    )
    results = client.wait_for_batch(batch_id, timeout_s=poll_timeout_s)
    done = _first_done(results)
    if not done or not done.full_zip_url:
        msg = done.err_msg if done else "no result"
        raise MineruError(f"MinerU extraction failed for {pdf_path.name}: {msg}")

    # Extract zip contents.
    extracted = client.download_and_extract_zip(done.full_zip_url)
    if not extracted.full_markdown:
        raise MineruError(f"MinerU returned an empty markdown for {pdf_path.name}")

    # Persist images.
    assets_directory = assets_dir(vault_root, citekey)
    assets_relative = to_vault_relative(vault_root, assets_directory)
    image_count, _image_map = _persist_images(extracted, assets_directory, assets_relative)

    # Build markdown + content items.
    (
        final_markdown,
        content_items,
        _img_count_from_list,
        table_count,
        page_count,
        content_list_ref,
    ) = _persist_content_with_tables(extracted, extracted.full_markdown, assets_relative)

    # Use whichever image count is larger (zip vs content_list).
    image_count = max(image_count, _img_count_from_list)

    # Write content.json (raw content_list backup; not touched by figure merge).
    write_json(content_path(vault_root, citekey), extracted.content_list or [])

    # Generate anchors BEFORE writing markdown, so figure-fragment merge can
    # rewrite both the anchors and the markdown in one pass.
    md_p = md_path(vault_root, citekey)
    manifest = generate_anchors(
        citekey=citekey,
        source_pdf=str(pdf_path),
        markdown_path=to_vault_relative(vault_root, md_p),
        content_list_path=to_vault_relative(vault_root, content_path(vault_root, citekey)),
        assets_root=assets_relative,
        content_list=content_list_ref,
    )
    # Attach normalized markdown tables to table anchors for agent consumption.
    _attach_markdown_tables(manifest.anchors, content_list_ref)

    # Auto-merge fragmented figures: re-render each fragmented figure group as
    # one high-DPI PNG from the original PDF, rewrite the markdown, delete the
    # fragment files, and collapse the anchor group. By the time the markdown
    # is written to disk, figures are already whole.
    final_markdown, merged_count = merge_fragmented_figures(
        manifest=manifest,
        markdown=final_markdown,
        pdf_path=pdf_path,
        assets_directory=assets_directory,
        assets_relative=assets_relative,
    )
    if merged_count:
        logger.info("Auto-merged %d fragmented figure(s) for %s", merged_count, citekey)
        # Recount images: fragments collapsed into merged figures.
        image_count = sum(1 for a in manifest.anchors if a.kind == "image")

    # Persist markdown + anchors (both now reflect merged figures).
    write_text(md_p, final_markdown)
    write_json(anchors_path(vault_root, citekey), manifest.to_dict())

    # Write meta.json (cache key = content hash).
    source_hash = pdf_content_hash(pdf_path)
    meta = ParseMeta(
        citekey=citekey,
        item_key=item_key,
        source_path=str(pdf_path),
        source_hash=source_hash,
        model_version=model_version,
        char_count=len(final_markdown),
        page_count=page_count,
        image_count=image_count,
        table_count=table_count,
        cached_at=now_ms(),
        mineru_batch_id=batch_id,
        content_list_path=to_vault_relative(vault_root, content_path(vault_root, citekey)),
        assets_root=assets_relative,
    )
    write_json(meta_path(vault_root, citekey), meta.__dict__)

    logger.info(
        "Parsed %s → %s (%d pages, %d images, %d tables, %d chars)",
        pdf_path.name,
        md_p,
        page_count,
        image_count,
        table_count,
        len(final_markdown),
    )

    return ParseResult(
        citekey=citekey,
        item_key=item_key,
        pdf_path=str(pdf_path),
        markdown_path=to_vault_relative(vault_root, md_p),
        anchors_path=to_vault_relative(vault_root, anchors_path(vault_root, citekey)),
        assets_dir=assets_relative,
        meta_path=to_vault_relative(vault_root, meta_path(vault_root, citekey)),
        page_count=page_count,
        image_count=image_count,
        table_count=table_count,
        char_count=len(final_markdown),
        cached=False,
    )


def _first_done(results: list[TaskResult]) -> TaskResult | None:
    for r in results:
        if r.state == "done":
            return r
    return results[0] if results else None


def _attach_markdown_tables(anchors, content_list: list[ContentItem]) -> None:
    """Populate Anchor.markdownTable for table anchors that converted cleanly."""
    for anchor in anchors:
        if anchor.kind != "table" or anchor.tableBodyHtml is None:
            continue
        block, _kept = normalize_table_body(anchor.tableBodyHtml, anchor.caption)
        # Only store when we got a real GFM table (not the fallback placeholder).
        if not block.startswith("*["):
            anchor.markdownTable = block


def _result_from_cache(
    vault_root: Path,
    citekey: str,
    item_key: str,
    pdf_path: str,
    meta: dict,
) -> ParseResult:
    return ParseResult(
        citekey=citekey,
        item_key=item_key,
        pdf_path=pdf_path,
        markdown_path=to_vault_relative(vault_root, md_path(vault_root, citekey)),
        anchors_path=to_vault_relative(vault_root, anchors_path(vault_root, citekey)),
        assets_dir=to_vault_relative(vault_root, assets_dir(vault_root, citekey)),
        meta_path=to_vault_relative(vault_root, meta_path(vault_root, citekey)),
        page_count=meta.get("page_count", 0),
        image_count=meta.get("image_count", 0),
        table_count=meta.get("table_count", 0),
        char_count=meta.get("char_count", 0),
        cached=True,
    )
