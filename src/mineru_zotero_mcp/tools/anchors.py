"""mineru_list_anchors and mineru_resolve_anchor tools.

Read-only queries over the anchors.json produced by mineru_parse_pdf.
Mirrors vspdf doc.list_anchors / doc.resolve_anchor but keyed by doc_id.
"""

from __future__ import annotations

import logging
from typing import Any

from .._app import mcp
from .._ctx import get_vault_root
from ..store import load_manifest

logger = logging.getLogger(__name__)


def _format_anchor_summary(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "anchorId": a.get("anchorId"),
        "kind": a.get("kind"),
        "page": a.get("page"),
        "bbox": a.get("bbox"),
        "textPreview": a.get("textPreview"),
        "caption": a.get("caption"),
        "imagePath": a.get("imagePath"),
    }


@mcp.tool(
    name="mineru_list_anchors",
    description=(
        "List the structural anchors for a parsed paper. Each anchor maps one "
        "content block (text / image / table / equation / list) to a PDF page + "
        "normalized bbox. Filter by page (1-based) or kind. "
        "Run mineru_parse_pdf first and use the returned doc_id."
    ),
)
def list_anchors_tool(
    doc_id: str,
    page: int | None = None,
    kind: str | None = None,
) -> str:
    vault = get_vault_root()
    manifest = load_manifest(vault, doc_id)
    if manifest is None:
        return (
            f"No anchors found for doc_id `{doc_id}`. "
            "Run `mineru_parse_pdf(...)` first and use the returned `doc_id`."
        )

    anchors = manifest.get("anchors", [])
    if page is not None:
        anchors = [a for a in anchors if a.get("page") == page]
    if kind is not None:
        anchors = [a for a in anchors if a.get("kind") == kind]

    if not anchors:
        return f"No anchors matching page={page}, kind={kind} for `{doc_id}`."

    lines = [f"# {len(anchors)} anchors for `{doc_id}`", ""]
    for a in anchors:
        summary = _format_anchor_summary(a)
        preview = (summary["textPreview"] or summary["caption"] or "")[:60]
        lines.append(
            f"- `{summary['anchorId']}` ({summary['kind']}, p{summary['page']}) "
            f"bbox={[round(c, 3) for c in (summary['bbox'] or [])]}"
            + (f" — {preview}" if preview else "")
        )
    return "\n".join(lines)


@mcp.tool(
    name="mineru_resolve_anchor",
    description=(
        "Resolve a single anchor to its full PDF location and content. Returns "
        "page, normalized + raw bbox, text/caption, and for tables the original "
        "HTML plus the normalized Markdown (if convertible). "
        "Use anchorId from mineru_list_anchors."
    ),
)
def resolve_anchor_tool(doc_id: str, anchor_id: str) -> str:
    vault = get_vault_root()
    manifest = load_manifest(vault, doc_id)
    if manifest is None:
        return (
            f"No anchors found for doc_id `{doc_id}`. "
            "Run `mineru_parse_pdf(...)` first and use the returned `doc_id`."
        )

    anchor = next(
        (a for a in manifest.get("anchors", []) if a.get("anchorId") == anchor_id),
        None,
    )
    if anchor is None:
        return f"Anchor `{anchor_id}` not found in `{doc_id}`."

    lines = [f"# `{anchor_id}`", ""]
    lines.append(f"- kind: `{anchor.get('kind')}`")
    lines.append(f"- page: {anchor.get('page')} (1-based)")
    lines.append(f"- bbox (normalized): {[round(c, 3) for c in anchor.get('bbox', [])]}")
    lines.append(f"- bbox (raw px): {[round(c, 1) for c in anchor.get('bboxRaw', [])]}")
    if anchor.get("textPreview"):
        lines.append(f"- text: {anchor['textPreview']}")
    if anchor.get("caption"):
        lines.append(f"- caption: {anchor['caption']}")
    if anchor.get("imagePath"):
        lines.append(f"- imagePath: `{anchor['imagePath']}`")
    if anchor.get("textFormat"):
        lines.append(f"- textFormat: `{anchor['textFormat']}`")
    if anchor.get("markdownTable"):
        lines.append("")
        lines.append("## Table (Markdown)")
        lines.append("")
        lines.append("```")
        lines.append(anchor["markdownTable"])
        lines.append("```")
    elif anchor.get("tableBodyHtml"):
        lines.append("")
        lines.append("## Table (raw HTML — not convertible to GFM)")
        lines.append("")
        lines.append("```html")
        lines.append(anchor["tableBodyHtml"])
        lines.append("```")
    return "\n".join(lines)
