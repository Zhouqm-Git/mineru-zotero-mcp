"""PDF region renderer using PyMuPDF.

Replaces vspdf/src/pdf-renderer.ts (pdftoppm + sharp). PyMuPDF's `clip=` lets us
render only the requested region directly — no full-page render + crop round-trip.
pymupdf is already a dependency via zotero-mcp's [pdf] extra, so this adds nothing.

Ported logic:
  - render_region   ← pdf-renderer.ts renderPdfRegion (bbox normalized [0,1] → pixel clip)
  - render_merged_region ← pdf-renderer.ts renderMergedRegion (outer union + 1% padding)
"""

from __future__ import annotations

import logging
from pathlib import Path

from .types import Bbox, RegionCapture

logger = logging.getLogger(__name__)

DEFAULT_DPI = 200


def render_region(
    pdf_path: str | Path,
    page_1based: int,
    bbox_normalized: Bbox,
    output_path: str | Path,
    dpi: int = DEFAULT_DPI,
) -> RegionCapture:
    """Render a normalized-bbox region of one PDF page as PNG.

    Args:
        pdf_path: absolute path to the source PDF.
        page_1based: 1-based page number.
        bbox_normalized: [x1, y1, x2, y2] in [0, 1].
        output_path: where to save the PNG (parent dir auto-created).
        dpi: render resolution (default 200, matching CiteFlow).
    """
    import fitz  # pymupdf

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    try:
        page_index = page_1based - 1
        if not 0 <= page_index < doc.page_count:
            raise ValueError(
                f"page {page_1based} out of range (PDF has {doc.page_count} pages)"
            )
        page = doc[page_index]
        page_w = page.rect.width
        page_h = page.rect.height

        x1, y1, x2, y2 = bbox_normalized
        # Clamp to page bounds and guard against degenerate boxes.
        clip = fitz.Rect(
            max(0.0, x1) * page_w,
            max(0.0, y1) * page_h,
            min(1.0, x2) * page_w,
            min(1.0, y2) * page_h,
        )
        if clip.is_empty or clip.is_infinite:
            # Fall back to the full page if the bbox is degenerate.
            clip = page.rect

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, clip=clip)
        pix.save(str(output_path))
    finally:
        doc.close()

    return RegionCapture(
        image_path=str(output_path),
        image_width=pix.width,
        image_height=pix.height,
        render_scale=zoom,
    )


def render_merged_region(
    pdf_path: str | Path,
    page_1based: int,
    bboxes: list[Bbox],
    output_path: str | Path,
    dpi: int = DEFAULT_DPI,
) -> RegionCapture:
    """Merge several normalized bboxes into one outer bbox, then render.

    Mirrors pdf-renderer.ts renderMergedRegion: take the outermost bounds, add a
    1% padding (clamped to [0,1]), and render that single region. Used to stitch
    fragmented figure pieces back into one capture.
    """
    if not bboxes:
        raise ValueError("render_merged_region requires at least one bbox")

    min_x = min(b[0] for b in bboxes)
    min_y = min(b[1] for b in bboxes)
    max_x = max(b[2] for b in bboxes)
    max_y = max(b[3] for b in bboxes)

    pad = 0.01
    merged: Bbox = (
        max(0.0, min_x - pad),
        max(0.0, min_y - pad),
        min(1.0, max_x + pad),
        min(1.0, max_y + pad),
    )
    return render_region(pdf_path, page_1based, merged, output_path, dpi)
