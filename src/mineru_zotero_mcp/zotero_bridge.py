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
from typing import Any

logger = logging.getLogger(__name__)


class AmbiguousItemKeyError(RuntimeError):
    """Raised when an item key matches more than one Zotero library."""


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


def get_pdf_path_for_item(item_key: str, library_id: int | str | None = None) -> Path | None:
    """Resolve the local filesystem path of an item's PDF attachment.

    Returns the first existing PDF attachment's path, or None if no local PDF
    is available (cloud-only attachment, or Zotero DB not readable).
    """
    attachments = _get_pdf_attachments_for_item(item_key, library_id)
    for att in attachments:
        resolved = att.get("resolved_path")
        if att.get("exists") and resolved:
            return Path(resolved)
    return None


def get_pdf_attachment_key_for_item(
    item_key: str,
    library_id: int | str | None = None,
    pdf_path: str | Path | None = None,
) -> str | None:
    """Return the Zotero attachment key for an item's PDF.

    If `pdf_path` is provided, prefer the attachment whose resolved local path
    matches it. Otherwise return the first existing PDF attachment.
    """
    attachments = _get_pdf_attachments_for_item(item_key, library_id)
    if not attachments:
        return None

    if pdf_path is not None:
        try:
            target = Path(pdf_path).expanduser().resolve()
        except OSError:
            target = Path(pdf_path).expanduser()
        for att in attachments:
            resolved = att.get("resolved_path")
            if not resolved:
                continue
            try:
                candidate = Path(resolved).expanduser().resolve()
            except OSError:
                candidate = Path(resolved).expanduser()
            if candidate == target:
                return att.get("key")

    for att in attachments:
        if att.get("exists") and att.get("key"):
            return att["key"]
    return attachments[0].get("key")


def _get_pdf_attachments_for_item(
    item_key: str, library_id: int | str | None = None
) -> list[dict[str, Any]]:
    Reader = _local_reader_factory()
    db_path = _zotero_db_path()
    try:
        with Reader(db_path=db_path) as reader:
            parent = _find_item_row(reader, item_key, library_id)
            if parent is None:
                return []
            attachments = []
            for att_key, zotero_path, ctype in reader._iter_parent_attachments(parent["itemID"]):
                resolved = reader._resolve_attachment_path(att_key, zotero_path or "")
                if ctype != "application/pdf" and (not resolved or Path(resolved).suffix.lower() != ".pdf"):
                    continue
                attachments.append({
                    "key": att_key,
                    "content_type": ctype,
                    "zotero_path": zotero_path,
                    "resolved_path": resolved,
                    "exists": bool(resolved and resolved.exists()),
                })
            return attachments
    except AmbiguousItemKeyError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("PDF attachment lookup failed for %s: %s", item_key, e)
        return []


def _find_item_row(reader: Any, item_key: str, library_id: int | str | None = None) -> Any | None:
    conn = reader._get_connection()
    params: list[Any] = [item_key]
    library_filter = ""
    if library_id is not None:
        library_filter = "AND i.libraryID = ?"
        params.append(int(library_id))

    rows = conn.execute(
        f"""
        SELECT i.itemID, i.key, i.libraryID
        FROM items i
        WHERE i.key = ?
          {library_filter}
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        ORDER BY i.libraryID
        """,
        params,
    ).fetchall()
    if len(rows) > 1:
        libs = ", ".join(str(r["libraryID"]) for r in rows)
        raise AmbiguousItemKeyError(
            f"Zotero item key `{item_key}` exists in multiple libraries ({libs}); pass library_id."
        )
    return rows[0] if rows else None


def get_item_identity(
    item_key: str, library_id: int | str | None = None
) -> dict[str, Any]:
    """Return library-scoped identity metadata for a Zotero item key.

    Output keys:
      - item_key
      - item_id
      - library_id
      - library_type
      - library_name

    The item key alone should not be used as a vault-wide storage identity.
    """
    Reader = _local_reader_factory()
    db_path = _zotero_db_path()
    params: list[Any] = [item_key]
    library_filter = ""
    if library_id is not None:
        library_filter = "AND i.libraryID = ?"
        params.append(int(library_id))
    try:
        with Reader(db_path=db_path) as reader:
            conn = reader._get_connection()
            rows = conn.execute(
                f"""
                SELECT i.itemID, i.key, i.libraryID,
                       l.type AS libraryType,
                       g.name AS groupName,
                       f.name AS feedName
                FROM items i
                JOIN libraries l ON i.libraryID = l.libraryID
                LEFT JOIN groups g ON i.libraryID = g.libraryID
                LEFT JOIN feeds f ON i.libraryID = f.libraryID
                WHERE i.key = ?
                  {library_filter}
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY i.libraryID
                """,
                params,
            ).fetchall()
    except AmbiguousItemKeyError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("item identity lookup failed for %s: %s", item_key, e)
        return {
            "item_key": item_key,
            "item_id": None,
            "library_id": None,
            "library_type": None,
            "library_name": None,
        }

    if not rows:
        return {
            "item_key": item_key,
            "item_id": None,
            "library_id": None,
            "library_type": None,
            "library_name": None,
        }

    if len(rows) > 1:
        libs = ", ".join(str(r["libraryID"]) for r in rows)
        raise AmbiguousItemKeyError(
            f"Zotero item key `{item_key}` exists in multiple libraries ({libs}); pass library_id."
        )

    row = rows[0]
    library_type = row["libraryType"]
    if library_type == "group":
        library_name = row["groupName"] or f"group-{row['libraryID']}"
    elif library_type == "feed":
        library_name = row["feedName"] or f"feed-{row['libraryID']}"
    else:
        library_name = "My Library"

    return {
        "item_key": row["key"],
        "item_id": row["itemID"],
        "library_id": row["libraryID"],
        "library_type": library_type,
        "library_name": library_name,
    }


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
