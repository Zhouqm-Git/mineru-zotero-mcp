"""arXiv → Zotero ingest tools.

Five tools covering the full pipeline from arXiv paper discovery to Zotero
local availability:

  1. arxiv_fetch_pdf        — download single/batch PDFs from arXiv
  2. arxiv_fetch_from_list  — extract arXiv IDs from GitHub awesome-lists / files
  3. zotero_webdav_upload   — upload PDF to WebDAV + create Zotero attachment (bypasses cloud quota)
  4. zotero_local_sync      — copy PDF into Zotero local storage + fix SQLite
  5. arxiv_ingest_paper     — end-to-end: download → Zotero item → WebDAV → local → (optional) parse

The first four are composable atoms; the fifth orchestrates them. Agents
should prefer the atoms for batch workflows (awesome-list ingest) and the
orchestrator for single-paper interactive ingest.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .._app import mcp
from .._ctx import (
    ConfigError,
    get_mineru_client,
    get_vault_root,
    get_webdav_config,
    get_zotero_api_config,
)
from ..arxiv_bridge import (
    fetch_batch,
    fetch_pdf,
    normalize_arxiv_id,
    parse_awesome_list,
)
from ..webdav_bridge import (
    create_attachment_entry,
    find_pdf_attachment,
    fix_local_storage,
    full_webdav_sync,
    upload_to_existing_attachment,
)

logger = logging.getLogger(__name__)


def _arxiv_cache_dir(vault: Path) -> Path:
    """Default PDF cache: <vault>/.raw/.arxiv_cache/"""
    d = vault / ".raw" / ".arxiv_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Tool 1: arxiv_fetch_pdf ─────────────────────────────────────────────────

@mcp.tool(
    name="arxiv_fetch_pdf",
    description=(
        "Download PDF(s) from arXiv. Accepts arXiv IDs, abs URLs, or pdf URLs. "
        "Downloads to <vault>/.raw/.arxiv_cache/{id}.pdf (or dest_dir). Uses 8-thread "
        "parallel download with 180s timeout (proven on 493-paper batches). "
        "Already-downloaded PDFs (>5KB) are skipped (cache). Returns each paper's "
        "local path. Use this before zotero_webdav_upload or arxiv_ingest_paper."
    ),
)
def arxiv_fetch_pdf_tool(
    arxiv_ids: list[str],
    dest_dir: str | None = None,
    parallel: int = 8,
    timeout: int = 180,
) -> str:
    """Download one or more arXiv PDFs. Pass a list of IDs/URLs."""
    try:
        vault = get_vault_root()
    except ConfigError as e:
        return f"Error: {e}"

    dest = Path(dest_dir) if dest_dir else _arxiv_cache_dir(vault)
    results = fetch_batch(arxiv_ids, dest, parallel=parallel, timeout=timeout)

    ok = [r for r in results if r.ok]
    cached = [r for r in ok if r.cached]
    failed = [r for r in results if not r.ok]

    lines = [f"# arXiv download: {len(results)} requested\n"]
    lines.append(f"**{len(ok)} OK** ({len(cached)} cached) · **{len(failed)} failed**\n")
    if ok:
        lines.append("## Downloaded")
        for r in ok:
            tag = " (cached)" if r.cached else ""
            lines.append(f"- `{r.arxiv_id}`{tag} → `{r.path}`")
    if failed:
        lines.append("\n## Failed")
        for r in failed:
            lines.append(f"- `{r.arxiv_id}`: {r.error}")

    return "\n".join(lines)


# ── Tool 2: arxiv_fetch_from_list ───────────────────────────────────────────

@mcp.tool(
    name="arxiv_fetch_from_list",
    description=(
        "Extract arXiv paper IDs from a GitHub awesome-list URL or a local "
        "markdown/text/bib file. Returns the list of {arxiv_id, title} entries. "
        "Set fetch_pdfs=true to also download all PDFs immediately; default "
        "(false) returns just the list so the agent can show the user for "
        "confirmation before a large batch download. GitHub repo URLs are "
        "auto-converted to raw README.md fetches."
    ),
)
def arxiv_fetch_from_list_tool(
    source: str,
    fetch_pdfs: bool = False,
    dest_dir: str | None = None,
    parallel: int = 8,
) -> str:
    """Parse a GitHub awesome-list or local file for arXiv IDs."""
    try:
        vault = get_vault_root()
    except ConfigError as e:
        return f"Error: {e}"

    entries = parse_awesome_list(source)
    if not entries:
        return f"No arXiv IDs found in `{source}`."

    lines = [f"# arXiv list from `{source}`\n", f"**{len(entries)} papers found:**\n"]
    for i, e in enumerate(entries, 1):
        title = f" — {e.title}" if e.title else ""
        lines.append(f"{i}. `{e.arxiv_id}`{title}")

    if not fetch_pdfs:
        lines.append(
            f"\nSet `fetch_pdfs=true` to download all {len(entries)} PDFs, "
            "or pass a subset to `arxiv_fetch_pdf`."
        )
        return "\n".join(lines)

    # Download all
    dest = Path(dest_dir) if dest_dir else _arxiv_cache_dir(vault)
    results = fetch_batch([e.arxiv_id for e in entries], dest, parallel=parallel)
    ok = sum(1 for r in results if r.ok)
    lines.append(f"\n## Downloaded {ok}/{len(entries)} PDFs to `{dest}`")
    return "\n".join(lines)


# ── Tool 3: zotero_webdav_upload ────────────────────────────────────────────

@mcp.tool(
    name="zotero_webdav_upload",
    description=(
        "Upload a local PDF to WebDAV + create a Zotero attachment entry. "
        "Bypasses the Zotero cloud 300MB quota by using WebDAV storage directly. "
        "Three steps: (1) create imported_url attachment via Web API, "
        "(2) ZIP+.prop PUT to WebDAV with User-Agent:Zotero, (3) PATCH md5/mtime. "
        "Requires ZOTERO_USER_ID + ZOTERO_API_KEY + WEBDAV_URL/USER/PASS env vars. "
        "After this, run zotero_local_sync so the current machine can open the PDF."
    ),
)
def zotero_webdav_upload_tool(
    parent_item_key: str,
    pdf_path: str,
    arxiv_url: str | None = None,
) -> str:
    """Upload PDF to WebDAV and create Zotero attachment. parent_item_key is the paper's Zotero item key."""
    api = get_zotero_api_config()
    webdav = get_webdav_config()

    if not api.configured:
        return (
            "Error: Zotero Web API not configured. Set ZOTERO_USER_ID "
            "(or ZOTERO_LIBRARY_ID) + ZOTERO_API_KEY env vars."
        )
    if not webdav.configured:
        return (
            "Error: WebDAV not configured. Set WEBDAV_URL + WEBDAV_USER + "
            "WEBDAV_PASS env vars."
        )

    pdf = Path(pdf_path).expanduser()
    if not pdf.is_file():
        return f"Error: PDF not found: {pdf}"

    # Infer arXiv URL if not provided
    if not arxiv_url:
        aid = normalize_arxiv_id(pdf.stem)
        arxiv_url = f"https://arxiv.org/abs/{aid}" if aid else f"file://{pdf.name}"

    try:
        result = full_webdav_sync(parent_item_key, pdf, arxiv_url, api, webdav)
    except Exception as e:  # noqa: BLE001
        logger.exception("webdav_upload failed")
        return f"Error: {e}"

    lines = ["# WebDAV upload\n"]
    if result.webdav_ok:
        lines.append(f"**Attachment key**: `{result.attachment_key}`")
        lines.append(f"**MD5**: `{result.md5}`")
        lines.append(f"**mtime**: `{result.mtime}`")
        lines.append(f"**API PATCH**: {'OK' if result.api_patched else 'FAILED'}")
        lines.append(
            f"\nNext: `zotero_local_sync(attachment_key=\"{result.attachment_key}\", "
            f"pdf_path=\"{pdf}\")` to make it openable locally."
        )
    else:
        lines.append(f"**FAILED**: {result.error}")
    return "\n".join(lines)


# ── Tool 4: zotero_local_sync ───────────────────────────────────────────────

@mcp.tool(
    name="zotero_local_sync",
    description=(
        "Copy a PDF into Zotero's local storage directory and update the SQLite "
        "database so the PDF opens immediately in Zotero. This fixes the common "
        "problem where WebDAV upload succeeds but Zotero doesn't download the "
        "file (syncState stuck at 1). Steps: copy PDF → ~/Zotero/storage/{key}/, "
        "close Zotero, SQLite UPDATE path/syncState/storageHash/storageModTime, "
        "reopen Zotero. On macOS, Zotero is auto-closed/reopened via AppleScript; "
        "on other platforms the user must close Zotero first."
    ),
)
def zotero_local_sync_tool(
    attachment_key: str,
    pdf_path: str,
    library_id: int = 1,
    close_zotero: bool = True,
) -> str:
    """Fix local Zotero storage so a PDF is immediately openable."""
    pdf = Path(pdf_path).expanduser()
    if not pdf.is_file():
        return f"Error: PDF not found: {pdf}"

    try:
        result = fix_local_storage(attachment_key, pdf, library_id, close_zotero)
    except Exception as e:  # noqa: BLE001
        logger.exception("local_sync failed")
        return f"Error: {e}"

    if result.ok:
        return (
            f"# Local sync OK\n"
            f"- attachment: `{attachment_key}`\n"
            f"- storage: `{result.storage_path}`\n"
            f"- SQLite: syncState=0, storageHash + storageModTime updated\n"
            f"PDF should now open in Zotero."
        )
    return f"# Local sync FAILED\n- error: {result.error}"


# ── Tool 5: arxiv_ingest_paper (end-to-end orchestrator) ────────────────────

@mcp.tool(
    name="arxiv_ingest_paper",
    description=(
        "End-to-end arXiv paper ingest into Zotero. Downloads the PDF from arXiv, "
        "creates the Zotero item with full arXiv metadata, and syncs the PDF. "
        "Sync mode: 'full' = WebDAV + local (recommended), 'local' = local only, "
        "'webdav' = WebDAV only. "
        "Handles the common case where Zotero cloud quota is full: the arXiv "
        "translator creates an item + empty attachment, then this tool uploads "
        "the PDF to WebDAV under that existing attachment (no duplicates). "
        "Optionally assign to collections and trigger MinerU parsing. "
        "For batch ingest (awesome-lists), use arxiv_fetch_from_list + "
        "arxiv_fetch_pdf + individual sync calls instead."
    ),
)
def arxiv_ingest_paper_tool(
    arxiv_id: str,
    sync_mode: str = "full",
    parse_after_ingest: bool = False,
    collections: list[str] | None = None,
    tags: list[str] | None = None,
    library_id: int | None = None,
) -> str:
    """One-shot: arXiv download → Zotero item (full metadata) → PDF sync → collection.

    arxiv_id: arXiv ID or URL.
    sync_mode: 'full' (WebDAV + local), 'local' (local only), 'webDAV' (WebDAV only).
    parse_after_ingest: if True, run mineru_parse_pdf on the ingested PDF after
        local sync completes. Requires local sync to have placed the PDF on disk
        (sync_mode 'full' or 'local'). Falls back to a manual hint if sync failed.
    collections: optional list of collection keys or names to assign the item to.
    tags: optional list of tags.
    """
    aid = normalize_arxiv_id(arxiv_id)
    if not aid:
        return f"Error: invalid arXiv ID: {arxiv_id}"

    sync_mode = sync_mode.strip().lower()
    if sync_mode not in ("full", "local", "webdav"):
        return f"Error: sync_mode must be 'full', 'local', or 'webdav' (got '{sync_mode}')"

    try:
        vault = get_vault_root()
    except ConfigError as e:
        return f"Error: {e}"

    lines = [f"# arXiv ingest: `{aid}`\n"]
    steps_done: list[str] = []
    steps_failed: list[str] = []
    arxiv_abs_url = f"https://arxiv.org/abs/{aid}"

    # ── Step 1: download PDF (with integrity validation) ──
    cache = _arxiv_cache_dir(vault)
    dl = fetch_pdf(aid, cache)
    if not dl.ok:
        return "\n".join(lines + [f"**DOWNLOAD FAILED**: {dl.error}"])
    pdf_path = dl.path
    tag = " (cached)" if dl.cached else ""
    lines.append(f"1. **Download**{tag}: `{pdf_path.name}` ({pdf_path.stat().st_size // 1024}KB)")
    steps_done.append("download")

    # ── Step 2: create Zotero item with full arXiv metadata ──
    # We POST an imported_url attachment with the PDF directly. Zotero's server-side
    # translator fetches arXiv metadata (authors, title, abstract, date) and creates
    # the parent item + attachment atomically. If the PDF upload fails (quota full),
    # the item + empty attachment entry still land — we handle that in Step 3.
    api = get_zotero_api_config()
    if not api.configured:
        lines.append(
            "2. **SKIPPED** (Zotero Web API not configured). "
            "Set ZOTERO_USER_ID + ZOTERO_API_KEY, or create the item manually "
            "via zotero_add_by_url then call zotero_local_sync."
        )
        return "\n".join(lines)

    parent_key, att_key, att_version = _create_zotero_item_with_pdf(
        api, aid, arxiv_abs_url, pdf_path, collections, tags
    )
    if not parent_key:
        lines.append(f"2. **ZOTERO ITEM FAILED**: could not create item for {aid}")
        steps_failed.append("zotero_item")
        return "\n".join(lines)

    lines.append(f"2. **Zotero item**: `{parent_key}`")
    if collections:
        lines.append(f"   Collections: {', '.join(collections)}")
    if att_key:
        lines.append(f"   PDF uploaded to Zotero cloud: `{att_key}` (quota OK — no WebDAV needed)")
    else:
        lines.append("   PDF upload failed (quota full?) — will use WebDAV/local sync")
    steps_done.append("zotero_item")

    # ── Step 3: sync PDF (only if Zotero cloud upload failed) ──
    if att_key:
        # Zotero cloud has the PDF — just do local sync for immediate access
        if sync_mode in ("full", "local"):
            ls = _do_local_sync(att_key, pdf_path, library_id, lines, steps_done, steps_failed)
    else:
        # Quota full — find the empty attachment the translator created, or create one
        if not att_key:
            existing = find_pdf_attachment(api, parent_key)
            if existing:
                att_key, att_version = existing
                lines.append(f"3. **Reusing existing attachment**: `{att_key}` (created by translator)")
            else:
                # No attachment exists — create one
                att = create_attachment_entry(api, parent_key, arxiv_abs_url, pdf_path.name)
                if not att:
                    lines.append("3. **FAILED**: could not create attachment entry")
                    steps_failed.append("attachment")
                    return "\n".join(lines + _summary(steps_done, steps_failed, parent_key, parse_after_ingest))
                att_key, att_version = att
                lines.append(f"3. **Created attachment**: `{att_key}`")

        # WebDAV upload to existing key (no duplicate)
        if sync_mode in ("full", "webdav"):
            webdav = get_webdav_config()
            if not webdav.configured:
                lines.append("   WebDAV SKIPPED (not configured) — local only")
                sync_mode = "local"
            else:
                try:
                    result = upload_to_existing_attachment(att_key, att_version, pdf_path, api, webdav)
                    if result.webdav_ok:
                        lines.append(f"   WebDAV: uploaded to `{att_key}`, PATCH {'OK' if result.api_patched else 'FAILED'}")
                        steps_done.append("webdav")
                    else:
                        lines.append(f"   WebDAV FAILED: {result.error}")
                        steps_failed.append("webdav")
                except Exception as e:  # noqa: BLE001
                    lines.append(f"   WebDAV ERROR: {e}")
                    steps_failed.append("webdav")

        # Local sync for immediate access
        if sync_mode in ("full", "local"):
            _do_local_sync(att_key, pdf_path, library_id, lines, steps_done, steps_failed)

    # ── Step 5 (optional): MinerU parse ──
    # Only when parse_after_ingest=True AND we have a local attachment key.
    # parse_pdf resolves the PDF via the Zotero SQLite by item_key, so the
    # local sync (Step 4) must have run first. If sync was skipped (webdav-only)
    # or failed, we surface a clear hint instead of a cryptic parse error.
    if parse_after_ingest:
        if not att_key:
            lines.append("5. **PARSE SKIPPED**: no attachment key (Zotero item creation failed)")
            steps_failed.append("parse")
        elif "local" not in steps_done:
            lines.append(
                "5. **PARSE SKIPPED**: local sync did not complete — MinerU needs the "
                "PDF on disk. Run `zotero_local_sync` first, then `mineru_parse_pdf`."
            )
            steps_failed.append("parse")
        else:
            try:
                from ..parse_persist import parse_pdf as _parse_pdf
                result = _parse_pdf(
                    vault_root=vault,
                    client=get_mineru_client(),
                    item_key=att_key,
                    library_id=library_id,
                )
                lines.append(f"5. **Parsed**: doc_id=`{result.doc_id}`, citekey=`{result.citekey}`")
                steps_done.append("parse")
            except Exception as e:  # noqa: BLE001 — don't let parse failure undo the ingest
                lines.append(f"5. **PARSE FAILED**: {e}")
                lines.append(f"   Ingest succeeded; parse manually via `mineru_parse_pdf(item_key=\"{att_key}\")`")
                steps_failed.append("parse")

    return "\n".join(lines + _summary(steps_done, steps_failed, parent_key, parse_after_ingest))


def _create_zotero_item_with_pdf(
    api,
    aid: str,
    arxiv_url: str,
    pdf_path: Path,
    collections: list[str] | None,
    tags: list[str] | None,
) -> tuple[str | None, str | None, int | None]:
    """Create a Zotero item from arXiv with PDF attached via the Web API.

    Uses Zotero's server-side translation: POST an imported_url attachment with
    the PDF file, and Zotero creates the parent item with full metadata + the
    attachment atomically.

    Returns (parent_key, attachment_key, version). If the PDF upload fails
    (quota full), parent_key is set but attachment_key is None — the empty
    attachment entry may still exist and can be found via find_pdf_attachment.
    """
    import requests

    # Step 2a: create the attachment entry (Zotero translator fills parent metadata)
    att_payload = [{
        "itemType": "attachment",
        "linkMode": "imported_url",
        "title": pdf_path.name,
        "contentType": "application/pdf",
        "url": arxiv_url,
        "tags": tags or [],
    }]
    if collections:
        att_payload[0]["collections"] = collections

    try:
        r = requests.post(
            f"{api.base_url}/items",
            headers={"Zotero-API-Key": api.api_key, "Content-Type": "application/json"},
            json=att_payload,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()["successful"]["0"]
        att_key = data["key"]
        att_version = data["version"]
    except Exception as e:  # noqa: BLE001
        logger.error("create attachment failed: %s", e)
        return None, None, None

    # Step 2b: upload the PDF file to the attachment
    try:
        import hashlib
        md5 = hashlib.md5(pdf_path.read_bytes()).hexdigest()
        mtime = str(int(pdf_path.stat().st_mtime * 1000))
        files = {
            "md5": (None, md5),
            "mtime": (None, mtime),
            "file": (pdf_path.name, pdf_path.read_bytes(), "application/pdf"),
        }
        r = requests.post(
            f"{api.base_url}/items/{att_key}/file",
            headers={"Zotero-API-Key": api.api_key, "If-None-Match": "*"},
            files=files,
            timeout=120,
        )
        if r.status_code in (200, 204):
            # Success — file uploaded to Zotero cloud. Get the parent item key.
            r2 = requests.get(
                f"{api.base_url}/items/{att_key}",
                headers={"Zotero-API-Key": api.api_key},
                timeout=30,
            )
            parent_key = r2.json()["data"].get("parentItem")
            return parent_key, att_key, att_version
        else:
            # Quota full or other error — attachment entry exists but no file.
            # The parent item may or may not exist yet (translator runs async).
            logger.warning("PDF upload to Zotero cloud failed: %s %s", r.status_code, r.text[:200])
            # Try to find parent via the attachment's parentItem field
            r2 = requests.get(
                f"{api.base_url}/items/{att_key}",
                headers={"Zotero-API-Key": api.api_key},
                timeout=30,
            )
            parent_key = r2.json()["data"].get("parentItem") if r2.status_code == 200 else None
            # If no parent yet, create a minimal one
            if not parent_key:
                parent_key = _create_minimal_parent(api, aid, arxiv_url, collections, tags, att_key)
            return parent_key, None, att_version
    except Exception as e:  # noqa: BLE001
        logger.error("PDF file upload failed: %s", e)
        return None, att_key, att_version


def _create_minimal_parent(api, aid, arxiv_url, collections, tags, att_key) -> str | None:
    """Create a minimal preprint parent item when the translator didn't fire."""
    import requests
    payload = [{
        "itemType": "preprint",
        "title": f"arXiv:{aid}",
        "url": arxiv_url,
        "repository": "arXiv",
        "archiveID": f"arXiv:{aid}",
        "tags": tags or [],
    }]
    if collections:
        payload[0]["collections"] = collections
    try:
        r = requests.post(
            f"{api.base_url}/items",
            headers={"Zotero-API-Key": api.api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["successful"]["0"]["key"]
    except Exception as e:  # noqa: BLE001
        logger.error("create minimal parent failed: %s", e)
        return None


def _do_local_sync(att_key, pdf_path, library_id, lines, steps_done, steps_failed):
    """Run fix_local_storage and append results to lines."""
    lib = library_id if library_id is not None else 1
    try:
        ls = fix_local_storage(att_key, pdf_path, lib, close_zotero=True)
        if ls.ok:
            lines.append(f"4. **Local sync**: `{ls.storage_path.parent.name}/` (syncState=0, linkMode=1)")
            steps_done.append("local")
        else:
            lines.append(f"4. **LOCAL SYNC FAILED**: {ls.error}")
            steps_failed.append("local")
    except Exception as e:  # noqa: BLE001
        lines.append(f"4. **LOCAL SYNC ERROR**: {e}")
        steps_failed.append("local")


def _summary(steps_done, steps_failed, parent_key, parse_after_ingest):
    """Build the summary footer."""
    out = [f"\n**Done**: {', '.join(steps_done) if steps_done else 'nothing'}"]
    if steps_failed:
        out.append(f"**Failed**: {', '.join(steps_failed)}")
    # Only suggest manual parse if auto-parse was requested but didn't run
    # successfully; otherwise just report the parent key.
    parse_ran_ok = parse_after_ingest and "parse" in steps_done
    if parse_after_ingest and not parse_ran_ok:
        out.append(f"\nNext: `mineru_parse_pdf(item_key=\"{parent_key}\")` to parse the PDF.")
    else:
        out.append(f"\nItem key: `{parent_key}`")
    return out
