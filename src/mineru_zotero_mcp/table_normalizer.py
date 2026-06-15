"""HTML table → GFM Markdown table conversion.

MinerU emits tables with both `table_body` (HTML <table>) and `img_path` (a
rendered image). The new architecture prefers pure-text Markdown tables so
text-only LLMs can consume them directly; we drop the image and convert the HTML.

Handles the common cases MinerU produces: thead/tbody, rowspan/colspan (expanded
into the cell so the table stays rectangular), and nested inline tags stripped to
text. Complex tables that cannot be flattened are detected and flagged so the
caller can keep the original HTML as a fallback (stored on Anchor.tableBodyHtml).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MAX_CELLS = 500  # guard against pathological input


@dataclass
class TableConversion:
    markdown: str | None  # GFM table, or None if not convertible
    fell_back: bool       # True → caller should keep original HTML


def _strip_tags(html_fragment: str) -> str:
    """Collapse an HTML fragment to plain text, preserving <br> as newlines."""
    # Treat <br> as newline before stripping.
    text = re.sub(r"<br\s*/?>", "\n", html_fragment, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape common entities.
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    # Collapse intra-cell whitespace but keep newlines (for <br>).
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return " ".join(ln for ln in lines if ln).strip()


def _row_to_cells(tr_html: str) -> list[tuple[str, int, int]]:
    """Extract cells from a <tr>: returns list of (text, rowspan, colspan)."""
    cells = re.findall(
        r"<(td|th)([^>]*)>(.*?)</\1>",
        tr_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    out = []
    for _tag, attrs, inner in cells:
        rs_m = re.search(r"rowspan\s*=\s*['\"]?(\d+)", attrs, flags=re.IGNORECASE)
        cs_m = re.search(r"colspan\s*=\s*['\"]?(\d+)", attrs, flags=re.IGNORECASE)
        rowspan = int(rs_m.group(1)) if rs_m else 1
        colspan = int(cs_m.group(1)) if cs_m else 1
        out.append((_strip_tags(inner), rowspan, colspan))
    return out


def _expand_spans(rows_raw: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """Expand rowspan/colspan into a rectangular grid.

    Each cell with rowspan>1 fills the same column in lower rows; colspan>1
    fills consecutive columns in the same row. The cell's text is duplicated
    into every position it covers (standard "unmerge" for GFM consumption).
    """
    # Determine grid width from the row with the max total col-span.
    width = max((sum(c[2] for c in row) for row in rows_raw), default=0)
    if width == 0:
        return []

    height = len(rows_raw)
    # grid[r][c] = text; "FILLED" sentinel marks cells claimed by a span.
    grid: list[list[str | None]] = [[None] * width for _ in range(height)]
    # Track cells still "overflowing" downward from a rowspan above.
    pending: dict[int, tuple[str, int]] = {}  # col -> (text, rows_remaining)

    for r, row in enumerate(rows_raw):
        col = 0
        # First, fill any cells carried down from a rowspan in a prior row.
        for c in list(pending.keys()):
            if c < width:
                grid[r][c] = pending[c][0]
                remaining = pending[c][1] - 1
                if remaining > 0:
                    pending[c] = (pending[c][0], remaining)
                else:
                    del pending[c]
        # Then place this row's cells into the next free columns.
        for text, rowspan, colspan in row:
            while col < width and grid[r][col] is not None:
                col += 1
            if col >= width:
                break  # overflow — ignore extras (malformed HTML)
            for k in range(colspan):
                c = col + k
                if c >= width:
                    break
                grid[r][c] = text
            if rowspan > 1:
                for k in range(colspan):
                    c = col + k
                    if c < width:
                        pending[c] = (text, rowspan - 1)
            col += colspan

    # Replace any remaining None (unfilled) with empty string.
    return [[(cell if cell is not None else "") for cell in row] for row in grid]


def html_table_to_markdown(html: str) -> TableConversion:
    """Convert one HTML <table> to a GFM pipe table.

    rowspan/colspan are expanded by duplicating the cell text into every
    covered position — this loses the visual "merged" appearance but preserves
    all data, which is what a text LLM needs. Returns fell_back=True only when
    the table is structurally unparseable (no <tr>, not a table, too large).
    """
    if not html or "<table" not in html.lower():
        return TableConversion(markdown=None, fell_back=True)

    # Split into rows.
    tr_matches = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL)
    if not tr_matches:
        return TableConversion(markdown=None, fell_back=True)

    rows_raw: list[list[tuple[str, int, int]]] = []
    for tr_html in tr_matches:
        cells = _row_to_cells(tr_html)
        if cells:
            rows_raw.append(cells)
        if sum(len(r) for r in rows_raw) > _MAX_CELLS:
            logger.debug("Table exceeded %d cells, falling back", _MAX_CELLS)
            return TableConversion(markdown=None, fell_back=True)

    if not rows_raw:
        return TableConversion(markdown=None, fell_back=True)

    # Expand rowspan/colspan into a rectangular grid of plain strings.
    rows = _expand_spans(rows_raw)
    if not rows:
        return TableConversion(markdown=None, fell_back=True)

    # Skip degenerate single-column "tables" — these are usually code/text that
    # MinerU mislabeled as a table. A real data table needs ≥2 columns to be
    # worth a GFM rendering (a 1-col table is just a list).
    width = len(rows[0])
    if width < 2:
        return TableConversion(markdown=None, fell_back=True)

    # Escape pipe characters inside cells (they'd break the GFM syntax).
    def _esc(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", " ")

    header = rows[0]
    body = rows[1:]

    md_lines = ["| " + " | ".join(_esc(c) for c in header) + " |"]
    md_lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in body:
        md_lines.append("| " + " | ".join(_esc(c) for c in row) + " |")

    return TableConversion(markdown="\n".join(md_lines), fell_back=False)


def normalize_table_body(html: str | None, caption: str | None = None) -> tuple[str, bool]:
    """Convenience wrapper used by parse_persist.

    Returns (markdown_block, kept_original_html). When conversion fails we emit a
    placeholder pointing at the caption and ask the caller to keep the HTML on
    the anchor so the agent can still read the raw structure.
    """
    if not html:
        cap = caption or ""
        return f"*[Table: {cap}]*", False

    conv = html_table_to_markdown(html)
    if conv.markdown is not None:
        return conv.markdown, False

    # Fell back — keep HTML inline in a code block so text LLMs still see it.
    cap = f" ({caption})" if caption else ""
    return f"*[Complex table{cap} — raw HTML below]*\n\n```html\n{html}\n```", True
