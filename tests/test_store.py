"""Tests for store: paths, atomic writes, content-hash cache, manifest round-trip.

Exercises the .raw/<citekey>/ layout and the cache-key policy without any
third-party deps (only stdlib tempfile). pytest-style fixtures are avoided so
this also runs under a plain unittest runner.
"""

import json
import tempfile
from pathlib import Path

from mineru_zotero_mcp import store
from mineru_zotero_mcp.types import Anchor, AnchorManifest, PageDimension


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def test_sanitize_citekey_strips_unsafe():
    assert store.sanitize_citekey("smith_2024") == "smith_2024"
    assert store.sanitize_citekey("sm/..\\it") == "sm___it"
    assert store.sanitize_citekey("") == "unknown"


def test_sanitize_citekey_blocks_traversal():
    # Path traversal must be neutralized — citekeys feed directly into .raw/<key>/.
    assert ".." not in store.sanitize_citekey("..")
    assert ".." not in store.sanitize_citekey("../etc/passwd")
    assert "/" not in store.sanitize_citekey("a/../b")
    # A single dot (legal in some BBT keys) survives.
    assert store.sanitize_citekey("smith.v1") == "smith.v1"


def test_raw_dir_layout():
    d = store.raw_dir(_tmp(), "smith2024")
    assert d.name == "smith2024"
    assert d.parent.name == "raw"  # no leading dot/underscore (Obsidian visibility)


def test_file_paths():
    t = _tmp()
    assert store.md_path(t, "k1") == t / "raw" / "k1" / "k1.md"
    assert store.anchors_path(t, "k1").name == "anchors.json"
    assert store.content_path(t, "k1").name == "content.json"
    assert store.meta_path(t, "k1").name == "meta.json"
    assert store.assets_dir(t, "k1").name == "assets"


def test_atomic_write_text():
    p = _tmp() / "raw" / "k1" / "k1.md"
    store.write_text(p, "hello")
    assert p.read_text() == "hello"
    assert not list(p.parent.glob(".*.tmp"))  # no leftover temps


def test_atomic_write_json_roundtrip():
    p = _tmp() / "x.json"
    store.write_json(p, {"a": 1, "b": [2, 3]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [2, 3]}


def test_pdf_content_hash_stable():
    pdf = _tmp() / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 hello" * 100)
    assert store.pdf_content_hash(pdf) == store.pdf_content_hash(pdf)


def test_content_hash_differs_on_change():
    pdf = _tmp() / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 v1")
    h1 = store.pdf_content_hash(pdf)
    pdf.write_bytes(b"%PDF-1.4 v2 completely different content here")
    assert h1 != store.pdf_content_hash(pdf)


def test_is_cached_matches_hash():
    t = _tmp()
    pdf = t / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 original")
    h = store.pdf_content_hash(pdf)
    store.write_json(store.meta_path(t, "k1"), {"source_hash": h, "page_count": 7})
    cached = store.is_cached(t, "k1", pdf)
    assert cached is not None
    assert cached["page_count"] == 7


def test_is_cached_misses_on_change():
    t = _tmp()
    pdf = t / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 original")
    store.write_json(store.meta_path(t, "k1"), {"source_hash": "0" * 32})
    assert store.is_cached(t, "k1", pdf) is None


def test_manifest_round_trip():
    t = _tmp()
    manifest = AnchorManifest(
        docId="k1",
        sourcePdf="/abs/x.pdf",
        markdownPath="raw/k1/k1.md",
        contentListPath="raw/k1/content.json",
        assetsRoot="raw/k1/assets",
        pageDimensions=[PageDimension(pageIdx=0, width=1000, height=1000)],
        anchors=[
            Anchor(
                anchorId="a_text_p1_0000", kind="text", page=1,
                bbox=(0.1, 0.1, 0.5, 0.5), bboxRaw=(100.0, 100.0, 500.0, 500.0),
                contentIndex=0, textPreview="hello",
            )
        ],
    )
    p = store.anchors_path(t, "k1")
    store.write_json(p, manifest.to_dict())

    loaded = store.load_manifest(t, "k1")
    assert loaded is not None
    rebuilt = store.manifest_from_dict(loaded)
    assert rebuilt.docId == "k1"
    assert len(rebuilt.anchors) == 1
    assert rebuilt.anchors[0].anchorId == "a_text_p1_0000"
    assert rebuilt.anchors[0].bbox == (0.1, 0.1, 0.5, 0.5)
    assert rebuilt.pageDimensions[0].pageIdx == 0

