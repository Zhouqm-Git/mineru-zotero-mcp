"""Bridge to zotero-mcp internals.

We import zotero_mcp's modules directly (not via MCP protocol) so we can reuse:
  - LocalZoteroReader.get_attachment_paths()  → item_key → PDF filesystem path
  - ZoteroBetterBibTexAPI                     → citation key ↔ item_key mapping

These reuse the same SQLite DB and BBT JSON-RPC endpoint that zotero-mcp itself
uses, so a single source of truth is preserved. No PDF path resolution or citekey
lookup logic is duplicated here.

References (from the survey):
  - local_db.py:775  LocalZoteroReader.get_attachment_paths(parent_key) -> list[dict]
                    dict keys: key, content_type, zotero_path, resolved_path(Path), exists(bool)
  - local_db.py:103  LocalZoteroReader(db_path=None, pdf_max_pages=None, pdf_timeout=30)
  - better_bibtex_client.py:38  ZoteroBetterBibTexAPI(port="23119", database="Zotero")
  - better_bibtex_client.py:198 search_citekeys(query, limit=10) -> list[dict]
  - better_bibtex_client.py:236 export_bibtex(item_key) — internally does item.citationkey RPC
  - tools/_helpers.py:24  _load_zotero_mcp_config() -> dict  (~/.config/zotero-mcp/config.json)
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def _zotero_db_path() -> str | None:
    """Read the Zotero SQLite path from zotero-mcp's config (mirrors retrieval.py:270-274)."""
    try:
        from zotero_mcp.tools._helpers import _load_zotero_mcp_config

        cfg = _load_zotero_mcp_config()
        return cfg.get("semantic_search", {}).get("zotero_db_path")
    except Exception as e:  # noqa: BLE001 — config is optional
        logger.debug("Could not load zotero-mcp config: %s", e)
        return None


@lru_cache(maxsize=1)
def _local_reader_factory():
    """Lazy-import LocalZoteroReader. Cached so the class object is stable."""
    from zotero_mcp.local_db import LocalZoteroReader

    return LocalZoteroReader


def get_pdf_path_for_item(item_key: str) -> Path | None:
    """Resolve the local filesystem path of an item's PDF attachment.

    Returns the first existing PDF attachment's path, or None if no local PDF
    is available (cloud-only attachment, or Zotero DB not readable).
    """
    Reader = _local_reader_factory()
    db_path = _zotero_db_path()
    try:
        with Reader(db_path=db_path) as reader:
            attachments = reader.get_attachment_paths(item_key)
    except Exception as e:  # noqa: BLE001 — bridge must degrade gracefully
        logger.warning("get_attachment_paths failed for %s: %s", item_key, e)
        return None

    for att in attachments:
        if att.get("content_type") == "application/pdf" and att.get("exists"):
            resolved = att.get("resolved_path")
            if resolved is not None:
                return Path(resolved)
    # Fall back to any existing PDF regardless of content_type label.
    for att in attachments:
        resolved = att.get("resolved_path")
        if att.get("exists") and resolved and Path(resolved).suffix.lower() == ".pdf":
            return Path(resolved)
    return None


@lru_cache(maxsize=1)
def _bbt_api():
    """Lazy BBT client. Requires Zotero desktop running with BetterBibTeX."""
    try:
        from zotero_mcp.better_bibtex_client import ZoteroBetterBibTexAPI

        return ZoteroBetterBibTexAPI()
    except Exception as e:  # noqa: BLE001
        logger.debug("BBT client unavailable: %s", e)
        return None


def item_key_to_citekey(item_key: str) -> str | None:
    """item_key → citekey. Primary: BBT JSON-RPC. Fallback: Zotero SQLite 'extra' field."""
    api = _bbt_api()
    if api is not None:
        try:
            mapping = api._make_request("item.citationkey", {"item_keys": [item_key]})
            ck = mapping.get(item_key)
            if ck:
                return ck
        except Exception as e:  # noqa: BLE001
            logger.debug("BBT item.citationkey failed for %s: %s", item_key, e)

    # Fallback: scan the 'extra' field in the local DB for "Citation Key: xxx".
    return _citekey_from_extra(item_key)


def citekey_to_item_key(citekey: str) -> str | None:
    """citekey → item_key. Primary: BBT get_item_by_citekey. Fallback: local DB search."""
    api = _bbt_api()
    if api is not None:
        try:
            item = api.get_item_by_citekey(citekey)
            if isinstance(item, dict):
                # BBT returns the 8-char Zotero key in the 'key' field.
                key = item.get("key")
                if key and isinstance(key, str) and len(key) == 8:
                    return key
                # Some BBT versions put it under 'id' (sometimes as a full URI).
                item_id = item.get("id")
                if isinstance(item_id, str):
                    if len(item_id) == 8:
                        return item_id
                    if "/items/" in item_id:
                        return item_id.rsplit("/items/", 1)[-1]
        except Exception as e:  # noqa: BLE001
            logger.debug("BBT citekey lookup failed for %s: %s", citekey, e)

    return _item_key_from_db_search(citekey)


# ─── SQLite fallbacks (no Zotero desktop required) ────────────────

_CITEKEY_RE = re.compile(r"Citation Key:\s*([A-Za-z0-9_:+\-]+)", re.IGNORECASE)


def _citekey_from_extra(item_key: str) -> str | None:
    Reader = _local_reader_factory()
    db_path = _zotero_db_path()
    try:
        with Reader(db_path=db_path) as reader:
            item = reader.get_item_by_key(item_key)
            if item and item.extra:
                m = _CITEKEY_RE.search(item.extra)
                if m:
                    return m.group(1)
    except Exception as e:  # noqa: BLE001
        logger.debug("SQLite extra-field citekey lookup failed: %s", e)
    return None


def _item_key_from_db_search(citekey: str) -> str | None:
    """Best-effort: search the local DB for an item whose extra field cites this key."""
    Reader = _local_reader_factory()
    db_path = _zotero_db_path()
    try:
        with Reader(db_path=db_path) as reader:
            results = reader.search_items_by_text(f"Citation Key: {citekey}", limit=5)
            for item in results:
                if item.extra and citekey.lower() in item.extra.lower():
                    return item.key
    except Exception as e:  # noqa: BLE001
        logger.debug("SQLite item_key search failed for %s: %s", citekey, e)
    return None


def resolve_identifier(item_key: str | None = None, citekey: str | None = None) -> tuple[str | None, str | None]:
    """Convenience: given either identifier, return (item_key, citekey) both filled when possible.

    Used by tools that accept either item_key or citekey. At least one must be provided.
    """
    if item_key and not citekey:
        citekey = item_key_to_citekey(item_key)
    elif citekey and not item_key:
        item_key = citekey_to_item_key(citekey)
    return item_key, citekey
