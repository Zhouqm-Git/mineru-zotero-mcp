"""mineru_capture_region tool.

Renders an arbitrary region of the paper's PDF as a PNG. After parse-time
figure merging, this is NOT needed for figures — those are already whole and
high-quality in attachments/papers/<citekey>/. This tool serves NON-figure
content that has no image on disk but that you want to show visually in a note:

  - a formula block          (equation anchors have text but no image)
  - a table's visual layout  (tables are stored as GFM text, not images)
  - a text passage as evidence
  - a custom bbox            (e.g. a region spanning columns)

Resolution and source: it re-renders from the ORIGINAL PDF via PyMuPDF, so the
output is always crisp regardless of what MinerU produced. You can also use it
to up-res a figure beyond the default parse-time DPI by passing dpi=300/400.

Boundary: this tool does NOT create a Zotero annotation. It returns the image
path + page + bbox so the caller can invoke zotero_create_area_annotation
(zotero-mcp) with those coordinates — keeping the annotation source-of-truth
in Zotero.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from .._app import mcp
from .._ctx import get_vault_root
from ..pdf_renderer import render_region
from ..store import load_manifest, paper_attachments_dir, sanitize_citekey, to_vault_relative
from ..types import Bbox
from ..zotero_bridge import get_pdf_path_for_item

logger = logging.getLogger(__name__)


@mcp.tool(
    name="mineru_capture_region",
    description=(
        "Render an arbitrary region of the paper's PDF as a PNG and save it under "
        "the vault's attachments/papers/<citekey>/. Use this for NON-figure content that has no "
        "image on disk: a formula block, a table's visual layout, a text passage "
        "as evidence, or a custom bbox. For figures, use the image path returned "
        "by mineru_list_visual_candidates directly — figures are already rendered "
        "during parsing.\n\n"
        "Returns the image path (vault-relative), page, normalized bbox, and "
        "dimensions. This tool does NOT create a Zotero annotation — to create "
        "one, call `zotero_create_area_annotation` (zotero-mcp) with the "
        "returned page+bbox."
    ),
)
def capture_region_tool(
    citekey: str,
    anchor_id: str | None = None,
    page: int | None = None,
    bbox: list[float] | None = None,
    dpi: int = 200,
) -> str:
    vault = get_vault_root()

    manifest = load_manifest(vault, citekey)
    if manifest is None:
        return (
            f"No parsed data for `{citekey}`. "
            f"Run `mineru_parse_pdf(citekey=\"{citekey}\")` first."
        )

    # Resolve target page + bbox from anchor_id OR explicit page+bbox.
    if anchor_id is not None:
        anchor = _find_anchor(manifest, anchor_id)
        if anchor is None:
            return f"Anchor `{anchor_id}` not found in `{citekey}`."
        target_page = anchor["page"]
        target_bbox: Bbox = tuple(anchor["bbox"])  # type: ignore[assignment]
        kind = anchor.get("kind")
        # Friendly nudge: figures are already on disk — point the caller there.
        if kind == "image" and anchor.get("imagePath"):
            return (
                f"Anchor `{anchor_id}` is a figure already rendered at "
                f"`{anchor['imagePath']}`. Use that path directly in your note — "
                f"no capture needed. (Call this tool with a higher `dpi` only if "
                f"you want to up-res it.)"
            )
    elif page is not None and bbox is not None and len(bbox) == 4:
        target_page = page
        target_bbox = (bbox[0], bbox[1], bbox[2], bbox[3])
    else:
        return "Provide either anchor_id, or both page and bbox (4 floats)."

    # Resolve the source PDF (recorded path first, then zotero_bridge).
    pdf_path = _resolve_pdf(manifest)
    if pdf_path is None or not Path(pdf_path).is_file():
        return (
            f"PDF unavailable for `{citekey}` — cannot render. "
            f"Ensure the Zotero attachment is downloaded locally."
        )

    out_path, out_relative = _build_capture_path(vault, citekey)
    try:
        cap = render_region(pdf_path, target_page, target_bbox, out_path, dpi)
    except Exception as e:  # noqa: BLE001
        logger.warning("Capture failed for %s: %s", citekey, e)
        return f"Capture failed for `{citekey}` page {target_page}: {e}"

    return (
        f"Captured `{citekey}` page {target_page} → `{out_relative}`.\n\n"
        f"- page: {target_page}\n"
        f"- bbox (normalized): {[round(c, 3) for c in target_bbox]}\n"
        f"- image: {cap.image_width}×{cap.image_height} px @ {dpi} DPI\n"
        f"- embed in a note: `![[{out_relative}]]`\n\n"
        f"To create a Zotero annotation, call "
        f"`zotero_create_area_annotation(attachment_key=..., page={target_page}, "
        f"x={target_bbox[0]:.4f}, y={target_bbox[1]:.4f}, "
        f"width={target_bbox[2]-target_bbox[0]:.4f}, "
        f"height={target_bbox[3]-target_bbox[1]:.4f}, comment=...)`."
    )


# ─── helpers ────────────────────────────────────────────────────


def _find_anchor(manifest: dict[str, Any], anchor_id: str) -> dict[str, Any] | None:
    return next(
        (a for a in manifest.get("anchors", []) if a.get("anchorId") == anchor_id),
        None,
    )


def _resolve_pdf(manifest: dict[str, Any]) -> str | None:
    src = manifest.get("sourcePdf")
    if src and Path(src).is_file():
        return src
    item_key = manifest.get("item_key")
    if item_key:
        p = get_pdf_path_for_item(item_key)
        if p:
            return str(p)
    return None


def _build_capture_path(vault: Path, citekey: str) -> tuple[Path, str]:
    out_dir = paper_attachments_dir(vault, citekey)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap_id = f"{sanitize_citekey(citekey)}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    out_path = out_dir / f"cap_{cap_id}.png"
    return out_path, to_vault_relative(vault, out_path)
