"""Tests for figure_merger — the parse-time auto-merge of fragmented figures.

Validates the markdown rewrite + anchor collapse logic without rendering a real
PDF (render_region is monkeypatched to just touch a file).
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from mineru_zotero_mcp.figure_merger import merge_fragmented_figures
from mineru_zotero_mcp.types import Anchor, AnchorManifest, RegionCapture


def _manifest(anchors: list[Anchor]) -> AnchorManifest:
    return AnchorManifest(
        docId="k1", sourcePdf="/abs/x.pdf", markdownPath="k1.md",
        contentListPath="c.json", assetsRoot="attachments/papers/k1",
        pageDimensions=[], anchors=anchors,
    )


def _img(aid: str, page: int, bbox: tuple[float, float, float, float], img: str, caption: str | None = None) -> Anchor:
    return Anchor(
        anchorId=aid, kind="image", page=page, bbox=bbox, bboxRaw=bbox,
        contentIndex=0, imagePath=img, caption=caption,
    )


def _fake_pdf() -> Path:
    """A real (empty) file standing in for the PDF so is_file() passes."""
    p = Path(tempfile.mktemp(suffix=".pdf"))
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def _fake_render(pdf_path, page, bbox, out_path, dpi=200):
    """Stand-in for PyMuPDF: just write a stub PNG so the file exists."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(b"FAKEPNG")
    return RegionCapture(image_path=str(out_path), image_width=100, image_height=100, render_scale=dpi / 72)


def test_no_images_no_change():
    manifest = _manifest([])
    with patch("mineru_zotero_mcp.figure_merger.render_region", side_effect=_fake_render):
        out, n = merge_fragmented_figures(
            manifest=manifest, markdown="no images", pdf_path="/abs/x.pdf",
            assets_directory=Path(tempfile.mkdtemp()) / "assets",
            assets_relative="attachments/papers/k1",
        )
    assert n == 0
    assert out == "no images"


def test_pdf_missing_leaves_fragments():
    """When the PDF is unavailable, fragments stay as-is (graceful degrade)."""
    a = _img("a_image_p1_0000", 1, (0.1, 0.1, 0.4, 0.4), "attachments/papers/k1/f1.jpg")
    b = _img("a_image_p1_0001", 1, (0.1, 0.42, 0.4, 0.7), "attachments/papers/k1/f2.jpg")
    manifest = _manifest([a, b])
    md = "![a](attachments/papers/k1/f1.jpg)\n![b](attachments/papers/k1/f2.jpg)"
    out, n = merge_fragmented_figures(
        manifest=manifest, markdown=md, pdf_path="/nonexistent.pdf",
        assets_directory=Path(tempfile.mkdtemp()) / "assets",
        assets_relative="attachments/papers/k1",
    )
    assert n == 0
    assert "f1.jpg" in out and "f2.jpg" in out  # unchanged
    assert len(manifest.anchors) == 2  # no collapse


def test_two_adjacent_fragments_merge():
    """Two adjacent image anchors → one merged figure, md collapses to one ref."""
    a = _img("a_image_p1_0000", 1, (0.1, 0.1, 0.4, 0.4), "attachments/papers/k1/f1.jpg", caption="Fig 1a")
    b = _img("a_image_p1_0001", 1, (0.1, 0.42, 0.4, 0.7), "attachments/papers/k1/f2.jpg", caption="Fig 1b")
    manifest = _manifest([a, b])
    md = "![Fig 1a](attachments/papers/k1/f1.jpg)\n![Fig 1b](attachments/papers/k1/f2.jpg)"

    assets = Path(tempfile.mkdtemp()) / "assets"
    # Create the fake fragment files so deletion logic can run.
    (assets).mkdir(parents=True)
    (assets / "f1.jpg").write_bytes(b"x")
    (assets / "f2.jpg").write_bytes(b"x")

    pdf = _fake_pdf()
    try:
        with patch("mineru_zotero_mcp.figure_merger.render_region", side_effect=_fake_render):
            out, n = merge_fragmented_figures(
                manifest=manifest, markdown=md, pdf_path=pdf,
                assets_directory=assets,
                assets_relative="attachments/papers/k1",
            )
    finally:
        pdf.unlink(missing_ok=True)

    assert n == 1
    # Only one image reference remains, pointing at the merged figure.
    assert "fig_a_image_p1_0000.png" in out
    assert "f1.jpg" not in out and "f2.jpg" not in out
    # Anchor set collapsed to one.
    assert len(manifest.anchors) == 1
    survivor = manifest.anchors[0]
    assert survivor.anchorId == "a_image_p1_0000"
    assert survivor.imagePath == "attachments/papers/k1/fig_a_image_p1_0000.png"
    # Captions combined.
    assert "Fig 1a" in survivor.caption and "Fig 1b" in survivor.caption
    # Merged bbox is the union.
    assert survivor.bbox == (0.1, 0.1, 0.4, 0.7)
    # Fragment files deleted.
    assert not (assets / "f1.jpg").exists()
    assert not (assets / "f2.jpg").exists()
    # Merged figure file exists.
    assert (assets / "fig_a_image_p1_0000.png").exists()


def test_standalone_image_not_merged():
    """A single isolated image anchor is left alone."""
    a = _img("a_image_p1_0000", 1, (0.1, 0.1, 0.2, 0.2), "attachments/papers/k1/iso.jpg")
    manifest = _manifest([a])
    md = "![iso](attachments/papers/k1/iso.jpg)"
    with patch("mineru_zotero_mcp.figure_merger.render_region", side_effect=_fake_render):
        out, n = merge_fragmented_figures(
            manifest=manifest, markdown=md, pdf_path="/abs/x.pdf",
            assets_directory=Path(tempfile.mkdtemp()) / "assets",
            assets_relative="attachments/papers/k1",
        )
    assert n == 0
    assert "iso.jpg" in out
    assert len(manifest.anchors) == 1


def test_dedup_across_document():
    """If a merged figure is referenced twice in the md, it appears once."""
    a = _img("a_image_p1_0000", 1, (0.1, 0.1, 0.4, 0.4), "attachments/papers/k1/f1.jpg")
    b = _img("a_image_p1_0001", 1, (0.1, 0.42, 0.4, 0.7), "attachments/papers/k1/f2.jpg")
    manifest = _manifest([a, b])
    # Same two refs appearing in two places.
    md = "![a](attachments/papers/k1/f1.jpg)\n![b](attachments/papers/k1/f2.jpg)\n\ntext\n\n![a](attachments/papers/k1/f1.jpg)\n![b](attachments/papers/k1/f2.jpg)"
    assets = Path(tempfile.mkdtemp()) / "assets"
    assets.mkdir(parents=True)
    (assets / "f1.jpg").write_bytes(b"x")
    (assets / "f2.jpg").write_bytes(b"x")
    pdf = _fake_pdf()
    try:
        with patch("mineru_zotero_mcp.figure_merger.render_region", side_effect=_fake_render):
            out, n = merge_fragmented_figures(
                manifest=manifest, markdown=md, pdf_path=pdf,
                assets_directory=assets,
                assets_relative="attachments/papers/k1",
            )
    finally:
        pdf.unlink(missing_ok=True)
    assert n == 1
    # The merged figure appears exactly once in the output.
    assert out.count("fig_a_image_p1_0000.png") == 1
