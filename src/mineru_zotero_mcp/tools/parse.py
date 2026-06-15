"""mineru_parse_pdf and mineru_parse_batch tools.

parse_pdf is the single-paper entry point. parse_batch fans out to many items
using MinerU's batch endpoint (≤50 per submission) and skips cached ones.
"""

from __future__ import annotations

import logging

from .._app import mcp
from .._ctx import ConfigError, get_mineru_client, get_vault_root
from ..mineru_client import MineruError
from ..parse_persist import parse_pdf
from ..zotero_bridge import item_key_to_citekey, resolve_identifier

logger = logging.getLogger(__name__)


@mcp.tool(
    name="mineru_parse_pdf",
    description=(
        "Parse a single Zotero PDF via MinerU (vlm model, tables converted to "
        "Markdown). Resolves the PDF by item_key (preferred) or citekey, sends it "
        "to the MinerU cloud, and persists results to <vault>/.raw/<citekey>/ "
        "(markdown, anchors.json, content.json, meta.json) plus figures under "
        "<vault>/attachments/papers/<citekey>/. "
        "Tables become GFM pipe tables so text-only LLMs can read them. "
        "Re-parses are skipped unless force=true (content-hash cache). "
        "After this, use mineru_read_markdown / mineru_list_anchors / "
        "mineru_capture_region to work with the parsed paper."
    ),
)
def parse_pdf_tool(
    item_key: str | None = None,
    citekey: str | None = None,
    force: bool = False,
    model_version: str = "vlm",
    enable_table: bool = True,
    page_ranges: str | None = None,
) -> str:
    """Parse one paper. Provide item_key (8-char Zotero key) or citekey."""
    if not item_key and not citekey:
        return "Error: provide either item_key or citekey."
    try:
        vault = get_vault_root()
        client = get_mineru_client()
        result = parse_pdf(
            vault_root=vault,
            client=client,
            item_key=item_key,
            citekey=citekey,
            model_version=model_version,
            enable_table=enable_table,
            page_ranges=page_ranges,
            force=force,
        )
    except (ConfigError, MineruError) as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 — surface to the agent
        logger.exception("parse_pdf failed")
        return f"Error: {e}"

    cached_tag = " (cached)" if result.cached else ""
    return (
        f"Parsed **{result.citekey}**{cached_tag}.\n\n"
        f"- item_key: `{result.item_key}`\n"
        f"- markdown: `{result.markdown_path}`\n"
        f"- anchors: `{result.anchors_path}`\n"
        f"- assets: `{result.assets_dir}`\n"
        f"- pages: {result.page_count}, images: {result.image_count}, "
        f"tables: {result.table_count}, chars: {result.char_count}\n\n"
        f"Next: `mineru_read_markdown(citekey=\"{result.citekey}\")` to read."
    )


@mcp.tool(
    name="mineru_parse_batch",
    description=(
        "Batch-parse multiple Zotero PDFs. Iterates item_keys (or every item in a "
        "Zotero collection — pass collection_key and the server resolves its items "
        "via zotero-mcp), skips papers whose content-hash cache matches (force=true "
        "to re-parse), and submits the rest to MinerU sequentially (each paper goes "
        "through mineru_parse_pdf). Returns a per-item summary.\n\n"
        "For very large batches prefer running this as a background job; each paper "
        "can take ~30s–minutes depending on length and MinerU load."
    ),
)
def parse_batch_tool(
    item_keys: list[str] | None = None,
    collection_key: str | None = None,
    force: bool = False,
    ignore_quota: bool = False,
    model_version: str = "vlm",
    enable_table: bool = True,
) -> str:
    """Batch parse. Provide item_keys directly, or collection_key to expand."""
    if not item_keys and not collection_key:
        return "Error: provide item_keys (list) or collection_key."

    keys = list(item_keys or [])
    if collection_key:
        expanded = _expand_collection(collection_key)
        if expanded is None:
            return f"Error: could not expand collection `{collection_key}` via zotero-mcp."
        keys.extend(expanded)

    if not keys:
        return "Error: no items to parse."

    try:
        vault = get_vault_root()
        client = get_mineru_client()
    except (ConfigError, MineruError) as e:
        return f"Error: {e}"

    # Pre-flight quota check: estimate this batch's page cost against today's
    # remaining high-priority quota. Warn (don't block) if it would exceed.
    if not ignore_quota:
        quota_warn = _quota_preflight(vault, len(keys))
        if quota_warn:
            return quota_warn + (
                "\n\nTo proceed anyway, re-run with `ignore_quota=True` "
                "(pages beyond the daily quota run at lower priority, not refused)."
            )

    results: list[dict] = []
    done = skipped = failed = 0
    for item_key in keys:
        try:
            r = parse_pdf(
                vault_root=vault,
                client=client,
                item_key=item_key,
                model_version=model_version,
                enable_table=enable_table,
                force=force,
            )
            if r.cached:
                skipped += 1
            else:
                done += 1
            results.append(
                {
                    "citekey": r.citekey,
                    "item_key": r.item_key,
                    "state": "cached" if r.cached else "done",
                    "markdown_path": r.markdown_path,
                }
            )
        except Exception as e:  # noqa: BLE001 — one failure shouldn't abort the batch
            failed += 1
            logger.warning("parse_batch item %s failed: %s", item_key, e)
            results.append({"item_key": item_key, "state": "failed", "error": str(e)})

    lines = [
        f"# Batch parse: {done} done, {skipped} cached/skipped, {failed} failed "
        f"({len(keys)} total)",
        "",
    ]
    for r in results:
        if r.get("state") in ("done", "cached"):
            lines.append(
                f"- [{r['state']}] `{r.get('citekey', '?')}` "
                f"(item_key={r.get('item_key')}) → {r.get('markdown_path')}"
            )
        else:
            lines.append(f"- [failed] item_key={r.get('item_key')}: {r.get('error')}")
    return "\n".join(lines)


def _expand_collection(collection_key: str) -> list[str] | None:
    """Best-effort: ask zotero-mcp's client for a collection's item keys.

    Falls back to None when the integration isn't available; the caller surfaces
    the error. We avoid a hard dependency on zotero-mcp's collection API by
    importing lazily.
    """
    try:
        from zotero_mcp.client import get_zotero_client

        zot = get_zotero_client()
        if zot is None:
            return None
        items = zot.collection_items(collection_key)
        return [
            it["data"]["key"]
            for it in items
            if it.get("data", {}).get("itemType") not in ("attachment", "note")
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("collection expansion failed for %s: %s", collection_key, e)
        return None


def _quota_preflight(vault, n_items: int) -> str | None:
    """Return a warning string if the batch would exceed the daily quota, else None.

    Uses the local quota estimate (scan_quota) + a conservative 20 pages/paper
    default. Small batches (≤5 papers) skip the check to avoid noise.
    """
    if n_items <= 5:
        return None  # small batch — not worth the warning
    from ..quota import DAILY_HIGH_PRIORITY_PAGES, estimate_batch_pages, format_quota_advice, scan_quota

    report = scan_quota(vault)
    estimated = estimate_batch_pages(["x"] * n_items)
    if report.would_exceed(estimated):
        return format_quota_advice(report, estimated)
    return None
