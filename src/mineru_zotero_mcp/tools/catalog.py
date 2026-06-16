"""Catalog and cross-document evidence search tools."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .._app import mcp
from .._ctx import get_vault_root
from ..store import content_path, load_manifest, sanitize_doc_id

_TERM_RE = re.compile(r"[\w.+-]+", re.UNICODE)


@mcp.tool(
    name="mineru_list_documents",
    description=(
        "List papers already parsed into the vault's .raw/<doc_id>/ layer. "
        "Use this before cross-paper synthesis, collection indexes, or batch QA."
    ),
)
def list_documents_tool(
    library_id: int | str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> str:
    vault = get_vault_root()
    docs = _iter_parsed_documents(vault)
    docs = _filter_documents(docs, library_id=library_id, query=query)
    docs.sort(key=_cached_at_sort_key, reverse=True)
    docs = docs[: max(1, limit)]

    if not docs:
        return "No parsed MinerU documents found for the requested filters."

    lines = [f"# Parsed MinerU documents ({len(docs)})", ""]
    for d in docs:
        stats = []
        if d.get("page_count"):
            stats.append(f"{d['page_count']}p")
        if d.get("table_count"):
            stats.append(f"{d['table_count']} tables")
        if d.get("image_count"):
            stats.append(f"{d['image_count']} images")
        stat_text = f" — {', '.join(stats)}" if stats else ""
        lib = f", library={d.get('library_id')}" if d.get("library_id") is not None else ""
        lines.append(
            f"- `{d['doc_id']}` citekey=`{d.get('citekey') or ''}` "
            f"item=`{d.get('item_key') or ''}`{lib}{stat_text}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="mineru_search_evidence",
    description=(
        "Search text, list, table, figure-caption, and equation anchors across "
        "parsed papers. Returns doc_id + anchor_id + page snippets that can be "
        "resolved with mineru_resolve_anchor or highlighted with "
        "mineru_create_evidence_annotation. Use match='all' for high-precision "
        "queries where every keyword must appear in the same anchor."
    ),
)
def search_evidence_tool(
    query: str,
    doc_ids: list[str] | str | None = None,
    library_id: int | str | None = None,
    kind: str | None = None,
    match: str = "any",
    limit: int = 20,
    snippet_chars: int = 220,
) -> str:
    terms = _query_terms(query)
    if not terms:
        return "Search query is empty."
    match = (match or "any").strip().lower()
    if match not in {"any", "all"}:
        return "Error: match must be 'any' or 'all'."

    vault = get_vault_root()
    docs = _iter_parsed_documents(vault)
    docs = _filter_documents(docs, library_id=library_id, query=None)
    selected = set(_normalize_doc_ids(doc_ids))
    if selected:
        docs = [d for d in docs if d["doc_id"] in selected]

    results: list[dict[str, Any]] = []
    for d in docs:
        manifest = load_manifest(vault, d["doc_id"])
        if manifest is None:
            continue
        content_items = _load_content_items(vault, d["doc_id"])
        for anchor in manifest.get("anchors", []):
            if kind is not None and anchor.get("kind") != kind:
                continue
            haystack = _anchor_search_text(anchor, content_items)
            if match == "all" and not _matches_all_terms(terms, haystack):
                continue
            score = _score_text(query, terms, haystack)
            if score <= 0:
                continue
            results.append({
                "score": score,
                "doc": d,
                "anchor": anchor,
                "snippet": _snippet(haystack, terms, snippet_chars),
            })

    results.sort(key=lambda r: (-r["score"], r["doc"]["doc_id"], r["anchor"].get("anchorId", "")))
    results = results[: max(1, limit)]
    if not results:
        return f"No evidence anchors matched `{query}`."

    lines = [f"# Evidence search: `{query}`", ""]
    if match == "all":
        lines.append("_Match mode: all query terms must appear in the anchor text._")
        lines.append("")
    for r in results:
        doc = r["doc"]
        anchor = r["anchor"]
        lines.append(
            f"- score={r['score']} `{doc['doc_id']}` `{anchor.get('anchorId')}` "
            f"({anchor.get('kind')}, p{anchor.get('page')}) "
            f"citekey=`{doc.get('citekey') or ''}`"
        )
        lines.append(f"  {r['snippet']}")
    lines.extend([
        "",
        "Next steps:",
        "- Use `mineru_resolve_anchor(doc_id=..., anchor_id=...)` for full content.",
        "- Use `mineru_create_evidence_annotation(doc_id=..., anchor_id=...)` "
        "for Zotero evidence marks.",
    ])
    return "\n".join(lines)


def _iter_parsed_documents(vault: Path) -> list[dict[str, Any]]:
    raw = vault / ".raw"
    if not raw.is_dir():
        return []
    docs = []
    for meta_file in raw.glob("**/meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rel_doc_id = meta_file.parent.relative_to(raw).as_posix()
        doc_id = sanitize_doc_id(str(meta.get("doc_id") or rel_doc_id))
        docs.append({
            **meta,
            "doc_id": doc_id,
            "raw_dir": meta_file.parent.as_posix(),
        })
    return docs


def _filter_documents(
    docs: list[dict[str, Any]],
    *,
    library_id: int | str | None,
    query: str | None,
) -> list[dict[str, Any]]:
    if library_id is not None:
        wanted = str(library_id)
        docs = [d for d in docs if str(d.get("library_id")) == wanted]
    if query:
        terms = _query_terms(query)
        docs = [
            d for d in docs
            if _score_text(query, terms, " ".join(str(d.get(k) or "") for k in _DOC_QUERY_KEYS)) > 0
        ]
    return docs


def _cached_at_sort_key(doc: dict[str, Any]) -> float:
    try:
        return float(doc.get("cached_at") or 0)
    except (TypeError, ValueError):
        return 0.0


_DOC_QUERY_KEYS = (
    "doc_id",
    "citekey",
    "item_key",
    "library_name",
    "source_path",
)


def _normalize_doc_ids(doc_ids: list[str] | str | None) -> list[str]:
    if doc_ids is None:
        return []
    if isinstance(doc_ids, str):
        return [d.strip() for d in doc_ids.split(",") if d.strip()]
    return [str(d).strip() for d in doc_ids if str(d).strip()]


def _load_content_items(vault: Path, doc_id: str) -> list[dict[str, Any]]:
    p = content_path(vault, doc_id)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _anchor_search_text(anchor: dict[str, Any], content_items: list[dict[str, Any]]) -> str:
    parts = [
        anchor.get("textPreview"),
        anchor.get("caption"),
        anchor.get("markdownTable"),
        anchor.get("textFormat"),
    ]
    idx = anchor.get("contentIndex")
    if isinstance(idx, int) and 0 <= idx < len(content_items):
        item = content_items[idx]
        if isinstance(item, dict):
            parts.extend([
                item.get("text"),
                " ".join(item.get("list_items") or []),
                item.get("table_body"),
                item.get("table_caption"),
                item.get("img_caption"),
            ])
    return " ".join(str(p) for p in parts if p)


def _query_terms(query: str | None) -> list[str]:
    return [t.lower() for t in _TERM_RE.findall(query or "") if t.strip()]


def _score_text(query: str, terms: list[str], text: str) -> int:
    lower = text.lower()
    score = 0
    phrase = " ".join((query or "").lower().split())
    normalized = " ".join(lower.split())
    if phrase and phrase in normalized:
        score += 10
    for term in terms:
        score += lower.count(term)
    matched_terms = sum(1 for term in set(terms) if term in lower)
    score += matched_terms * 2
    return score


def _matches_all_terms(terms: list[str], text: str) -> bool:
    lower = text.lower()
    return all(term in lower for term in set(terms))


def _snippet(text: str, terms: list[str], max_chars: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    lower = normalized.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) >= 0]
    start = max(0, min(positions) - max_chars // 3) if positions else 0
    end = min(len(normalized), start + max_chars)
    snippet = normalized[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(normalized):
        snippet += "..."
    return snippet
