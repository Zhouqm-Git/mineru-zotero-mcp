"""Anchor generator — MinerU content_list → AnchorManifest.

Direct port of vspdf/src/anchor-generator.ts (logic unchanged). Each anchor maps
a content block (text/image/table/equation/list) to a PDF page + normalized bbox.

The one divergence from the TS original: image/table `imagePath` is written as a
vault-relative path `assets/<name>` instead of `.docnotes/parsed/<slug>.assets/<name>`,
because the new layout is <vault>/.raw/<citekey>/assets/.
"""

from __future__ import annotations

from collections import defaultdict

from .types import Anchor, AnchorKind, AnchorManifest, ContentItem, PageDimension

# Kinds that produce anchors (header / page_number are skipped).
_ANCHOR_KINDS = {"text", "image", "table", "equation", "list"}

# MinerU emits coordinates on a 1000x1000 virtual canvas regardless of the
# original PDF aspect ratio. Normalize against this base (matches TS:188).
MINERU_VIRTUAL_SIZE = 1000


def _normalize_bbox(
    bbox: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    return (
        bbox[0] / page_width,
        bbox[1] / page_height,
        bbox[2] / page_width,
        bbox[3] / page_height,
    )


def _make_anchor_id(kind: AnchorKind, page: int, index: int) -> str:
    return f"a_{kind}_p{page}_{index:04d}"


def _should_create_anchor(item: ContentItem) -> bool:
    if item.type not in _ANCHOR_KINDS:
        return False
    if item.bbox is None:
        return False
    if item.type == "text" and (not item.text or not item.text.strip()):
        return False
    return True


def _bbox_contained(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def _deduplicate_text_anchors(
    anchors: list[Anchor], content_list: list[ContentItem]
) -> list[Anchor]:
    """Drop inner text anchors when an outer one has identical text (keeps headings)."""
    text_anchors = [a for a in anchors if a.kind == "text"]
    to_remove: set[str] = set()

    for i, ai in enumerate(text_anchors):
        if ai.anchorId in to_remove:
            continue
        ci = content_list[ai.contentIndex] if ai.contentIndex < len(content_list) else None
        if ci and ci.text_level and ci.text_level >= 1:
            continue  # headings always kept

        for j in range(i + 1, len(text_anchors)):
            aj = text_anchors[j]
            if aj.anchorId in to_remove:
                continue
            if ai.page != aj.page:
                continue

            cj = content_list[aj.contentIndex] if aj.contentIndex < len(content_list) else None
            ti = (ci.text if ci and ci.text else "").strip()
            tj = (cj.text if cj and cj.text else "").strip()
            if ti != tj:
                continue
            if cj and cj.text_level and cj.text_level >= 1:
                continue

            if _bbox_contained(ai.bboxRaw, aj.bboxRaw):
                to_remove.add(ai.anchorId)
            elif _bbox_contained(aj.bboxRaw, ai.bboxRaw):
                to_remove.add(aj.anchorId)

    if not to_remove:
        return anchors
    return [a for a in anchors if a.anchorId not in to_remove]


def _build_anchor(
    item: ContentItem,
    kind: AnchorKind,
    page: int,
    bbox: tuple[float, float, float, float],
    bbox_raw: tuple[float, float, float, float],
    index: int,
    content_index: int,
) -> Anchor:
    anchor = Anchor(
        anchorId=_make_anchor_id(kind, page, index),
        kind=kind,
        page=page,
        bbox=bbox,
        bboxRaw=bbox_raw,
        contentIndex=content_index,
    )

    if kind == "text":
        anchor.textPreview = (item.text or "")[:100]
        anchor.textLevel = item.text_level
    elif kind == "image":
        if item.img_path:
            name = item.img_path.split("/", 1)[-1] if item.img_path.startswith("images/") else item.img_path
            anchor.imagePath = f"assets/{name}"
        anchor.caption = item.image_caption[0] if item.image_caption else None
    elif kind == "table":
        # NOTE: img_path is intentionally dropped for tables (table-as-markdown policy).
        anchor.caption = item.table_caption[0] if item.table_caption else None
        anchor.tableBodyHtml = item.table_body
    elif kind == "equation":
        anchor.textPreview = item.text
        anchor.textFormat = item.text_format or "latex"
    elif kind == "list":
        anchor.textPreview = " ".join(item.list_items)[:200]
        anchor.listItemCount = len(item.list_items)

    return anchor


def generate_anchors(
    citekey: str,
    source_pdf: str,
    markdown_path: str,
    content_list_path: str,
    assets_root: str,
    content_list: list[ContentItem],
) -> AnchorManifest:
    """Build a complete AnchorManifest from a MinerU content_list.

    Args:
        citekey: citation key (becomes manifest.docId in the new architecture).
        source_pdf: absolute PDF path.
        markdown_path / content_list_path / assets_root: vault-relative paths.
        content_list: parsed MinerU content_list.
    """
    # 1. Determine which pages exist (for pageDimensions).
    page_indices = sorted({item.page_idx for item in content_list})
    page_dimensions = [
        PageDimension(pageIdx=idx, width=MINERU_VIRTUAL_SIZE, height=MINERU_VIRTUAL_SIZE)
        for idx in page_indices
    ]

    # 2. Generate anchors with per-kind per-page counters.
    anchors: list[Anchor] = []
    counters: dict[str, int] = defaultdict(int)

    for i, item in enumerate(content_list):
        if not _should_create_anchor(item):
            continue
        assert item.bbox is not None  # guaranteed by _should_create_anchor
        page = item.page_idx + 1  # 1-based
        bbox = _normalize_bbox(item.bbox, MINERU_VIRTUAL_SIZE, MINERU_VIRTUAL_SIZE)

        kind = item.type  # type: ignore[assignment]
        key = f"{kind}_p{page}"
        idx = counters[key]
        counters[key] = idx + 1

        anchors.append(_build_anchor(item, kind, page, bbox, item.bbox, idx, i))

    # 3. Deduplicate nested text anchors.
    anchors = _deduplicate_text_anchors(anchors, content_list)

    return AnchorManifest(
        docId=citekey,
        sourcePdf=source_pdf,
        markdownPath=markdown_path,
        contentListPath=content_list_path,
        assetsRoot=assets_root,
        pageDimensions=page_dimensions,
        anchors=anchors,
    )
