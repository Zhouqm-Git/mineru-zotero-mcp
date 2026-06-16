"""High-level evidence annotation tool.

This module bridges MinerU anchors to Zotero annotations. Callers provide the
stable `doc_id` and an `anchor_id`; the tool resolves page/text/bbox plus the
Zotero PDF attachment key, then delegates the actual write to zotero-mcp.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .._app import mcp
from .._ctx import get_vault_root
from ..store import content_path, load_manifest, load_meta
from ..zotero_bridge import get_pdf_attachment_key_for_item

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_COLOR = "#a28ae5"
_DEFAULT_TAGS = ["paper-wiki", "evidence"]
_ANNOTATION_KEY_RE = re.compile(r"\*\*Annotation Key:\*\*\s*([A-Za-z0-9]+)")
_AUTO_AREA_KINDS = {"image", "table", "equation"}


@mcp.tool(
    name="mineru_create_evidence_annotation",
    description=(
        "Create a Zotero evidence annotation directly from a MinerU anchor. "
        "Input doc_id + anchor_id; this tool resolves the Zotero PDF attachment, "
        "page, text, and bbox automatically. mode='auto' uses text highlights for "
        "text/list anchors and area annotations for image/table/equation anchors. "
        "Defaults to purple #a28ae5 and tags ['paper-wiki', 'evidence'] so "
        "agent-created evidence is visually distinct from user highlights."
    ),
)
def create_evidence_annotation_tool(
    doc_id: str,
    anchor_id: str,
    comment: str | None = None,
    mode: str = "auto",
    text: str | None = None,
    color: str = _DEFAULT_AGENT_COLOR,
    tags: list[str] | str | None = None,
    text_max_chars: int = 240,
) -> str:
    vault = get_vault_root()
    manifest = load_manifest(vault, doc_id)
    if manifest is None:
        return (
            f"No parsed data for doc_id `{doc_id}`. "
            "Run `mineru_parse_pdf(...)` first and use the returned `doc_id`."
        )

    anchor = _find_anchor(manifest, anchor_id)
    if anchor is None:
        return f"Anchor `{anchor_id}` not found in `{doc_id}`."

    meta = load_meta(vault, doc_id) or {}
    item_key = meta.get("item_key")
    library_id = meta.get("library_id")
    source_pdf = meta.get("source_path") or manifest.get("sourcePdf")
    if not item_key:
        return f"`{doc_id}` meta.json has no item_key; cannot resolve Zotero attachment."

    attachment_key = get_pdf_attachment_key_for_item(
        item_key,
        library_id=library_id,
        pdf_path=source_pdf,
    )
    if not attachment_key:
        return (
            f"No local PDF attachment key found for item `{item_key}` "
            f"(doc_id `{doc_id}`). Ensure the PDF is attached and downloaded in Zotero."
        )

    selected_mode = _choose_mode(anchor, mode)
    final_tags = _merge_tags(tags)
    final_comment = comment if comment is not None else _default_comment(anchor, doc_id)

    try:
        if selected_mode == "text":
            highlight_text = _resolve_highlight_text(
                vault,
                doc_id,
                anchor,
                text_override=text,
                max_chars=text_max_chars,
            )
            if not highlight_text:
                return (
                    f"Anchor `{anchor_id}` has no usable text for a text highlight. "
                    "Use mode='area' instead."
                )
            result = _call_zotero_create_annotation(
                attachment_key=attachment_key,
                page=int(anchor["page"]),
                text=highlight_text,
                comment=final_comment,
                color=color,
                tags=final_tags,
            )
        elif selected_mode == "area":
            x, y, width, height = _bbox_to_rect(anchor.get("bbox") or [])
            result = _call_zotero_create_area_annotation(
                attachment_key=attachment_key,
                page=int(anchor["page"]),
                x=x,
                y=y,
                width=width,
                height=height,
                comment=final_comment,
                color=color,
                tags=final_tags,
            )
        else:
            return "Error: mode must be one of 'auto', 'text', or 'area'."
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to create evidence annotation")
        return f"Error creating Zotero evidence annotation: {e}"

    annotation_key = _extract_annotation_key(result)
    status = "failed" if _is_failure_result(result) or not annotation_key else "created"
    summary = _annotation_summary(
        status=status,
        doc_id=doc_id,
        anchor_id=anchor_id,
        anchor=anchor,
        selected_mode=selected_mode,
        attachment_key=attachment_key,
        color=color,
    )
    if annotation_key:
        summary.append(f"- annotation_key: `{annotation_key}`")
        summary.append(f"- zotero_link: `zotero://select/library/items/{annotation_key}`")
    summary.extend(["", "## Zotero result", "", result])
    return "\n".join(summary)


def _find_anchor(manifest: dict[str, Any], anchor_id: str) -> dict[str, Any] | None:
    return next(
        (a for a in manifest.get("anchors", []) if a.get("anchorId") == anchor_id),
        None,
    )


def _choose_mode(anchor: dict[str, Any], mode: str) -> str:
    mode = (mode or "auto").strip().lower()
    if mode != "auto":
        return mode
    return "area" if anchor.get("kind") in _AUTO_AREA_KINDS else "text"


def _bbox_to_rect(bbox: list[float] | tuple[float, ...]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("anchor bbox must have four normalized coordinates")
    x1, y1, x2, y2 = (float(c) for c in bbox)
    width = x2 - x1
    height = y2 - y1
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid anchor bbox: {bbox}")
    return x1, y1, width, height


def _resolve_highlight_text(
    vault: Path,
    doc_id: str,
    anchor: dict[str, Any],
    *,
    text_override: str | None,
    max_chars: int,
) -> str:
    if text_override:
        return _trim_annotation_text(text_override, max_chars)

    item = _load_content_item(vault, doc_id, int(anchor.get("contentIndex", -1)))
    if item:
        kind = anchor.get("kind")
        if kind in {"text", "equation"}:
            return _trim_annotation_text(item.get("text") or "", max_chars)
        if kind == "list":
            return _trim_annotation_text(" ".join(item.get("list_items") or []), max_chars)

    return _trim_annotation_text(anchor.get("textPreview") or "", max_chars)


def _load_content_item(vault: Path, doc_id: str, index: int) -> dict[str, Any] | None:
    if index < 0:
        return None
    p = content_path(vault, doc_id)
    if not p.is_file():
        return None
    try:
        content = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(content, list) or index >= len(content):
        return None
    item = content[index]
    return item if isinstance(item, dict) else None


def _trim_annotation_text(text: str, max_chars: int) -> str:
    normalized = " ".join((text or "").split())
    if max_chars <= 0 or len(normalized) <= max_chars:
        return normalized
    trimmed = normalized[:max_chars].rsplit(" ", 1)[0]
    return trimmed or normalized[:max_chars]


def _merge_tags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        incoming: list[str] = []
    elif isinstance(tags, str):
        incoming = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        incoming = [str(t).strip() for t in tags if str(t).strip()]

    merged: list[str] = []
    for tag in [*_DEFAULT_TAGS, *incoming]:
        if tag not in merged:
            merged.append(tag)
    return merged


def _default_comment(anchor: dict[str, Any], doc_id: str) -> str:
    kind = anchor.get("kind", "anchor")
    preview = anchor.get("caption") or anchor.get("textPreview") or ""
    preview = _trim_annotation_text(preview, 120)
    suffix = f": {preview}" if preview else ""
    return f"paper-wiki evidence ({doc_id}, {anchor.get('anchorId')}, {kind}){suffix}"


class _ZoteroToolContext:
    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        logger.error(message, *args, **kwargs)


def _call_zotero_create_annotation(**kwargs: Any) -> str:
    from zotero_mcp.tools.annotations import create_annotation

    return create_annotation(ctx=_ZoteroToolContext(), **kwargs)


def _call_zotero_create_area_annotation(**kwargs: Any) -> str:
    from zotero_mcp.tools.annotations import create_area_annotation

    return create_area_annotation(ctx=_ZoteroToolContext(), **kwargs)


def _extract_annotation_key(result: str) -> str | None:
    match = _ANNOTATION_KEY_RE.search(result)
    return match.group(1) if match else None


def _is_failure_result(result: str) -> bool:
    stripped = result.lstrip()
    return stripped.startswith("Error:") or stripped.startswith("Failed")


def _annotation_summary(
    *,
    status: str,
    doc_id: str,
    anchor_id: str,
    anchor: dict[str, Any],
    selected_mode: str,
    attachment_key: str,
    color: str,
) -> list[str]:
    return [
        f"# Evidence annotation {status} for `{doc_id}`",
        "",
        f"- anchor_id: `{anchor_id}`",
        f"- anchor_kind: `{anchor.get('kind')}`",
        f"- mode: `{selected_mode}`",
        f"- attachment_key: `{attachment_key}`",
        f"- page: {anchor.get('page')}",
        f"- color: `{color}`",
    ]
