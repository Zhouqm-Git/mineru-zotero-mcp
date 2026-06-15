"""mineru_read_markdown tool.

Reads the parsed markdown for a paper, either in full or sliced by page (using
the `<!-- Page N -->` markers injected during parse). Ported from
vspdf/src/mcp-server.ts:371-431 (doc.read_markdown).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .._app import mcp
from .._ctx import get_vault_root
from ..store import load_manifest, md_path

logger = logging.getLogger(__name__)

_PAGE_MARKER_RE = re.compile(r"<!--\s*Page\s+(\d+)\s*-->")
_MAX_CHARS_DEFAULT = 8000


@mcp.tool(
    name="mineru_read_markdown",
    description=(
        "Read the MinerU-parsed Markdown for a paper. By default returns the full "
        "document; pass `page` (1-based) to get one page's slice (boundaries come "
        "from `<!-- Page N -->` markers). The response includes page_hints with "
        "anchorIds per page so you can follow up with mineru_resolve_anchor / "
        "mineru_capture_region. Output is truncated to max_chars to stay LLM-friendly."
    ),
)
def read_markdown_tool(
    citekey: str,
    page: int | None = None,
    max_chars: int = _MAX_CHARS_DEFAULT,
) -> str:
    vault = get_vault_root()
    md_p = md_path(vault, citekey)
    if not md_p.is_file():
        return (
            f"No parsed markdown for `{citekey}`. "
            f"Run `mineru_parse_pdf(citekey=\"{citekey}\")` first."
        )

    content = md_p.read_text(encoding="utf-8")
    manifest = load_manifest(vault, citekey)

    if page is None:
        page_hints = _all_page_hints(manifest)
        truncated = len(content) > max_chars
        snippet = content[:max_chars] if truncated else content
        header = f"# `{citekey}` — full markdown ({len(content)} chars"
        if truncated:
            header += f", truncated to {max_chars}"
        header += ")\n\n"
        return header + snippet + ("\n\n[...truncated...]" if truncated else "")

    # Slice by page marker.
    page_ranges = _compute_page_ranges(content)
    target = next((r for r in page_ranges if r["page"] == page), None)
    if target is None:
        anchor_ids = _anchor_ids_for_page(manifest, page)
        return f"Page {page} not found in `{citekey}`. page_hints={{{page}: {anchor_ids}}}"

    page_content = content[target["start"]: target["end"]].strip()
    truncated = len(page_content) > max_chars
    if truncated:
        page_content = page_content[:max_chars] + "\n\n[...truncated...]"
    anchor_ids = _anchor_ids_for_page(manifest, page)

    return (
        f"# `{citekey}` — page {page} ({len(content[target['start']:target['end']])} chars)\n\n"
        + page_content
        + f"\n\n---\nanchorIds on this page: {anchor_ids}"
    )


def _compute_page_ranges(content: str) -> list[dict]:
    ranges: list[dict] = []
    for m in _PAGE_MARKER_RE.finditer(content):
        ranges.append({"page": int(m.group(1)), "start": m.end(), "end": -1})
    for i in range(len(ranges) - 1):
        ranges[i]["end"] = ranges[i + 1]["start"]
    if ranges:
        ranges[-1]["end"] = len(content)
    return ranges


def _all_page_hints(manifest: dict | None) -> list[dict]:
    if not manifest:
        return []
    anchors = manifest.get("anchors", [])
    pages = sorted({a["page"] for a in anchors if "page" in a})
    return [
        {"page": p, "anchorIds": [a["anchorId"] for a in anchors if a.get("page") == p]}
        for p in pages
    ]


def _anchor_ids_for_page(manifest: dict | None, page: int) -> list[str]:
    if not manifest:
        return []
    return [a["anchorId"] for a in manifest.get("anchors", []) if a.get("page") == page]
