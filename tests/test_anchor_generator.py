"""Tests for anchor_generator (ported from vspdf/src/anchor-generator.ts).

Validates the content_list → AnchorManifest mapping without any third-party deps.
"""

from mineru_zotero_mcp.anchor_generator import generate_anchors
from mineru_zotero_mcp.types import ContentItem


def _text(idx: int, text: str, level: int | None = None) -> ContentItem:
    return ContentItem(
        type="text",
        page_idx=idx,
        bbox=(10.0, 10.0, 500.0, 50.0),
        text=text,
        text_level=level,
    )


def _image(idx: int, img: str = "images/fig1.jpg", caption: list[str] | None = None) -> ContentItem:
    return ContentItem(
        type="image",
        page_idx=idx,
        bbox=(100.0, 100.0, 400.0, 400.0),
        img_path=img,
        image_caption=caption or [],
    )


def _table(idx: int, body: str = "<table><tr><td>a</td></tr></table>") -> ContentItem:
    return ContentItem(
        type="table",
        page_idx=idx,
        bbox=(50.0, 50.0, 950.0, 200.0),
        table_body=body,
        table_caption=["T1"],
    )


def _equation(idx: int) -> ContentItem:
    return ContentItem(
        type="equation",
        page_idx=idx,
        bbox=(100.0, 100.0, 300.0, 150.0),
        text="E=mc^2",
        text_format="latex",
    )


def test_generate_basic_kinds():
    items = [_text(0, "intro paragraph text"), _image(0), _table(0), _equation(0)]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", items)

    kinds = sorted(a.kind for a in m.anchors)
    assert kinds == ["equation", "image", "table", "text"], kinds
    # Page dimension inferred as 1000x1000 virtual canvas.
    assert m.pageDimensions[0].width == 1000
    assert m.pageDimensions[0].height == 1000


def test_anchor_id_format():
    items = [_text(0, "first"), _text(0, "second")]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", items)
    ids = [a.anchorId for a in m.anchors if a.kind == "text"]
    assert ids == ["a_text_p1_0000", "a_text_p1_0001"], ids


def test_bbox_normalized_against_1000():
    # bbox (200,400,800,900) on a 1000×1000 canvas → (0.2,0.4,0.8,0.9)
    items = [ContentItem(type="text", page_idx=0, bbox=(200.0, 400.0, 800.0, 900.0), text="x" * 25)]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", items)
    a = m.anchors[0]
    assert a.bbox == (0.2, 0.4, 0.8, 0.9)
    assert a.bboxRaw == (200.0, 400.0, 800.0, 900.0)


def test_image_path_rewritten_to_assets():
    items = [_image(0, img="images/fig1.jpg", caption=["Fig 1"])]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "attachments/papers/k1", items)
    a = m.anchors[0]
    assert a.imagePath == "attachments/papers/k1/fig1.jpg", a.imagePath
    assert a.caption == "Fig 1"


def test_table_drops_img_path_keeps_html():
    items = [ContentItem(
        type="table", page_idx=0, bbox=(1.0, 1.0, 2.0, 2.0),
        table_body="<table></table>", img_path="images/t.jpg",
    )]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", items)
    a = m.anchors[0]
    assert a.imagePath is None  # policy: tables don't carry images
    assert a.tableBodyHtml == "<table></table>"


def test_empty_text_skipped():
    items = [
        ContentItem(type="text", page_idx=0, bbox=(1.0, 1.0, 2.0, 2.0), text="   "),
        _text(0, "real content"),
    ]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", items)
    assert len(m.anchors) == 1
    assert m.anchors[0].textPreview == "real content"


def test_header_and_page_number_not_anchored():
    items = [
        ContentItem(type="header", page_idx=0, bbox=(1.0, 1.0, 2.0, 2.0), text="hdr"),
        ContentItem(type="page_number", page_idx=0, bbox=(1.0, 1.0, 2.0, 2.0), text="3"),
    ]
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", items)
    assert m.anchors == []


def test_dedup_nested_text_keeps_outer():
    # Two text items with identical text, inner bbox nested in outer → keep outer only.
    outer = ContentItem(type="text", page_idx=0, bbox=(10.0, 10.0, 500.0, 500.0), text="same text here it is long enough")
    inner = ContentItem(type="text", page_idx=0, bbox=(20.0, 20.0, 100.0, 100.0), text="same text here it is long enough")
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", [outer, inner])
    # The outer (larger bbox) should survive.
    assert len(m.anchors) == 1
    assert m.anchors[0].bboxRaw == (10.0, 10.0, 500.0, 500.0)


def test_heading_never_deduped():
    base = "heading text long enough to qualify"
    h1 = ContentItem(type="text", page_idx=0, bbox=(10.0, 10.0, 500.0, 500.0), text=base, text_level=1)
    nested = ContentItem(type="text", page_idx=0, bbox=(20.0, 20.0, 100.0, 100.0), text=base)
    m = generate_anchors("k1", "/x.pdf", "k1.md", "content.json", "assets", [h1, nested])
    # Both survive because the outer is a heading.
    assert len(m.anchors) == 2
