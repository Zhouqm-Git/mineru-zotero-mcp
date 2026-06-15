"""Core data structures.

Ported from vspdf/src/types.ts. Only the anchor/parse/content layer is kept —
Annotation and note types are intentionally absent (those belong to zotero-mcp).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# A normalized bbox: [x1, y1, x2, y2] in [0, 1].
Bbox = tuple[float, float, float, float]

# A raw pixel bbox as MinerU emits it: [x1, y1, x2, y2] in pixels.
BboxRaw = tuple[float, float, float, float]

AnchorKind = Literal["text", "image", "table", "equation", "list"]


@dataclass
class ContentItem:
    """One entry of MinerU's content_list. Matches vspdf/src/types.ts ContentItem.

    Only fields we actually consume are typed; MinerU may emit others.
    """

    type: str  # text|image|table|equation|list|header|page_number
    page_idx: int  # 0-based
    bbox: BboxRaw | None = None
    # text / header / page_number / equation
    text: str | None = None
    text_level: int | None = None  # 1-6 for headings
    text_format: str | None = None  # "latex" for equations
    # image
    img_path: str | None = None
    image_caption: list[str] = field(default_factory=list)
    image_footnote: list[str] = field(default_factory=list)
    # table
    table_caption: list[str] = field(default_factory=list)
    table_footnote: list[str] = field(default_factory=list)
    table_body: str | None = None  # HTML
    # list
    sub_type: str | None = None
    list_items: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> ContentItem:
        """Build a ContentItem from a MinerU content_list entry, tolerating missing keys."""
        return cls(
            type=raw.get("type", "text"),
            page_idx=raw.get("page_idx", 0),
            bbox=tuple(raw["bbox"]) if raw.get("bbox") else None,  # type: ignore[arg-type]
            text=raw.get("text"),
            text_level=raw.get("text_level"),
            text_format=raw.get("text_format"),
            img_path=raw.get("img_path"),
            image_caption=list(raw.get("image_caption") or []),
            image_footnote=list(raw.get("image_footnote") or []),
            table_caption=list(raw.get("table_caption") or []),
            table_footnote=list(raw.get("table_footnote") or []),
            table_body=raw.get("table_body"),
            sub_type=raw.get("sub_type"),
            list_items=list(raw.get("list_items") or []),
        )


@dataclass
class PageDimension:
    pageIdx: int  # 0-based
    width: float
    height: float


@dataclass
class Anchor:
    """Maps one content block to a PDF page + normalized bbox."""

    anchorId: str  # a_{kind}_p{page}_{0000}
    kind: AnchorKind
    page: int  # 1-based (matches PDF convention)
    bbox: Bbox  # normalized [0,1]
    bboxRaw: BboxRaw  # original pixels
    contentIndex: int  # index into content_list
    # text
    textPreview: str | None = None
    textLevel: int | None = None
    # image
    imagePath: str | None = None  # relative to vault root
    caption: str | None = None
    # table
    tableBodyHtml: str | None = None
    markdownTable: str | None = None  # M6: normalized GFM table
    # equation
    textFormat: str | None = None
    # list
    listItemCount: int | None = None


@dataclass
class AnchorManifest:
    docId: str  # canonical parse identity: lib-<libraryID>/<item_key>
    sourcePdf: str  # absolute PDF path
    markdownPath: str  # relative to vault root
    contentListPath: str  # relative to vault root
    assetsRoot: str  # relative to vault root
    pageDimensions: list[PageDimension] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class ParseResult:
    """Returned by parse_pdf."""

    citekey: str
    item_key: str | None
    doc_id: str
    pdf_path: str  # absolute
    markdown_path: str  # relative to vault
    anchors_path: str
    assets_dir: str
    meta_path: str
    page_count: int
    image_count: int
    table_count: int
    char_count: int
    cached: bool
    library_id: int | None = None
    library_name: str | None = None


@dataclass
class ParseMeta:
    """Persisted to <doc_id>/meta.json — drives the content-hash cache."""

    citekey: str
    item_key: str | None
    doc_id: str
    source_path: str  # absolute PDF path
    source_hash: str  # md5 of first 1MB (content-hash cache key)
    library_id: int | None = None
    library_name: str | None = None
    strategy: str = "mineru"
    model_version: str = "vlm"
    parse_time_ms: int = 0
    char_count: int = 0
    page_count: int = 0
    image_count: int = 0
    table_count: int = 0
    cached_at: float = 0.0
    mineru_batch_id: str | None = None
    content_list_path: str = ""
    assets_root: str = ""


@dataclass
class FragmentGroup:
    """Output of fragment_detector.detect_fragments for one group."""

    anchorIds: list[str]
    mergedBbox: Bbox
    isFragment: bool
    mineruImagePath: str | None = None


@dataclass
class RegionCapture:
    """Returned by pdf_renderer.render_region."""

    image_path: str  # absolute
    image_width: int
    image_height: int
    render_scale: float
