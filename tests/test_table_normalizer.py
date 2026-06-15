"""Tests for table_normalizer (HTML → GFM Markdown).

Validates the table-as-markdown policy central to reference-notes.md §③.
"""

from mineru_zotero_mcp.table_normalizer import html_table_to_markdown, normalize_table_body


def test_simple_table_to_gfm():
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None
    lines = conv.markdown.splitlines()
    assert lines[0] == "| A | B |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 1 | 2 |"
    assert conv.fell_back is False


def test_no_header_treated_as_data():
    # All <td> → first row becomes the header row in GFM (required).
    html = "<table><tr><td>x</td><td>y</td></tr><tr><td>1</td><td>2</td></tr></table>"
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None
    assert conv.markdown.splitlines()[0] == "| x | y |"


def test_rowspan_expanded():
    """rowspan should be expanded by duplicating the cell downward, not fall back."""
    html = (
        "<table>"
        "<tr><td rowspan='2'>a</td><td>b</td></tr>"
        "<tr><td>c</td></tr>"
        "</table>"
    )
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None, "rowspan should expand, not fall back"
    lines = conv.markdown.splitlines()
    # header + separator + 1 body row (grid row 1 is header, row 2 is body)
    assert len(lines) == 3
    # Body row should have 'a' carried down from the rowspan + 'c'.
    assert lines[2].startswith("| a |")
    assert "c" in lines[2]


def test_colspan_expanded():
    """colspan should fill consecutive columns with the same text."""
    html = (
        "<table>"
        "<tr><th>a</th><th colspan='2'>merged</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr>"
        "</table>"
    )
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None
    lines = conv.markdown.splitlines()
    # Header row has 3 columns: a | merged | merged
    assert lines[0].count("merged") == 2


def test_pseudo_table_single_cell_falls_back():
    """MinerU sometimes wraps a code block in a 1-cell <table>; skip those."""
    html = "<table><tr><td>some python code here</td></tr></table>"
    conv = html_table_to_markdown(html)
    assert conv.markdown is None
    assert conv.fell_back is True


def test_pipe_escaped_in_cells():
    html = "<table><tr><th>a|b</th><th>c</th></tr></table>"
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None
    assert "\\|" in conv.markdown  # pipe escaped


def test_normalize_body_fallback_keeps_html():
    # Non-table HTML / unparseable content falls back to keeping it inline.
    md, kept = normalize_table_body("<div>not a table</div>", caption="Tbl")
    assert kept is True
    assert "```html" in md


def test_normalize_body_no_html_placeholder():
    md, kept = normalize_table_body(None, caption="My Table")
    assert "[Table: My Table]" in md
    assert kept is False


def test_br_becomes_space_in_cell():
    html = "<table><tr><td>line1<br>line2</td><td>x</td></tr></table>"
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None
    # The two lines joined by a space inside one cell.
    assert "line1 line2" in conv.markdown


def test_entities_unescaped():
    html = "<table><tr><td>a &amp; b</td><td>c</td></tr></table>"
    conv = html_table_to_markdown(html)
    assert conv.markdown is not None
    assert "a & b" in conv.markdown
