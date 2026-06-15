"""Tests for parse_persist's pure helpers (no network, no MinerU call).

These cover the markdown post-processing ported from vspdf/src/parse-and-persist.ts:
  - injectPageMarkers
  - rewrite image paths
  - inline HTML table conversion
"""

from mineru_zotero_mcp.parse_persist import (
    _convert_inline_html_tables,
    _inject_page_markers,
    _rewrite_image_paths,
)
from mineru_zotero_mcp.types import ContentItem


def test_rewrite_image_paths():
    md = "See ![fig](images/abc.jpg) and ![other](images/def.png)."
    out = _rewrite_image_paths(md, "raw/k1/assets")
    assert "![fig](raw/k1/assets/abc.jpg)" in out
    assert "![other](raw/k1/assets/def.png)" in out
    assert "images/" not in out


def test_rewrite_image_paths_idempotent_when_no_images():
    md = "no images here"
    assert _rewrite_image_paths(md, "assets") == md


def test_inject_page_markers_inserts_at_first_text():
    md = "Intro text that is long enough to qualify.\n\nSecond paragraph."
    items = [ContentItem(type="text", page_idx=0, text="Intro text that is long enough to qualify.")]
    out = _inject_page_markers(md, items)
    assert out.startswith("<!-- Page 1 -->")


def test_inject_page_markers_multi_page_descending_order():
    md = (
        "Page one content that is long enough here.\n\n"
        "Middle stuff.\n\n"
        "Page two content also long enough here."
    )
    items = [
        ContentItem(type="text", page_idx=0, text="Page one content that is long enough here."),
        ContentItem(type="text", page_idx=1, text="Page two content also long enough here."),
    ]
    out = _inject_page_markers(md, items)
    assert "<!-- Page 1 -->" in out
    assert "<!-- Page 2 -->" in out
    # Page 2 marker must appear after page 1's text (offset ordering).
    assert out.index("<!-- Page 1 -->") < out.index("<!-- Page 2 -->")


def test_inject_page_markers_skips_short_text():
    md = "hi\n\nLong enough content to be detected properly here."
    items = [
        ContentItem(type="text", page_idx=0, text="hi"),  # <20 chars, skipped
        ContentItem(type="text", page_idx=0, text="Long enough content to be detected properly here."),
    ]
    out = _inject_page_markers(md, items)
    assert "<!-- Page 1 -->" in out
    # Marker must come before the long text, not the short one.
    assert out.index("<!-- Page 1 -->") < out.index("Long enough")


def test_convert_inline_html_tables_replaces_table():
    md = "Before\n\n<table><tr><th>X</th><th>Y</th></tr><tr><td>1</td><td>2</td></tr></table>\n\nAfter"
    out = _convert_inline_html_tables(md)
    assert "<table>" not in out
    assert "| X |" in out
    assert "| 1 |" in out
    assert "Before" in out and "After" in out


def test_convert_inline_html_tables_complex_falls_back_to_html_block():
    md = "<table><tr><td rowspan='2'>merged</td></tr></table>"
    out = _convert_inline_html_tables(md)
    # Not convertible to GFM → kept in a fenced html block.
    assert "```html" in out
