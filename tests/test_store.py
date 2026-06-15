"""Tests for store: paths, atomic writes, content-hash cache, manifest round-trip.

Exercises the .raw/<doc_id>/ internal layout, attachments/papers/<doc_id>/
embed layout, and cache-key policy without any third-party deps.
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
    # Path traversal must be neutralized before names are used under .raw/.
    assert ".." not in store.sanitize_citekey("..")
    assert ".." not in store.sanitize_citekey("../etc/passwd")
    assert "/" not in store.sanitize_citekey("a/../b")
    # A single dot (legal in some BBT keys) survives.
    assert store.sanitize_citekey("smith.v1") == "smith.v1"


def test_make_doc_id_uses_library_and_item_key():
    assert store.make_doc_id(1, "ABCD1234") == "lib-1/ABCD1234"
    assert store.make_doc_id(None, "ABCD1234") == "lib-unknown/ABCD1234"
    assert store.make_doc_id("7", "A/../B") == "lib-7/A___B"


def test_raw_dir_layout():
    d = store.raw_dir(_tmp(), "lib-1/ABCD1234")
    assert d.name == "ABCD1234"
    assert d.parent.name == "lib-1"
    assert d.parent.parent.name == ".raw"


def test_file_paths():
    t = _tmp()
    doc_id = "lib-1/ABCD1234"
    assert store.md_path(t, doc_id, "smith2024") == t / ".raw" / "lib-1" / "ABCD1234" / "smith2024.md"
    assert store.anchors_path(t, doc_id).name == "anchors.json"
    assert store.content_path(t, doc_id).name == "content.json"
    assert store.meta_path(t, doc_id).name == "meta.json"
    assert store.assets_dir(t, doc_id) == t / "attachments" / "papers" / "lib-1" / "ABCD1234"


def test_atomic_write_text():
    p = _tmp() / ".raw" / "k1" / "k1.md"
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
    doc_id = "lib-1/ABCD1234"
    store.write_json(store.meta_path(t, doc_id), {"source_hash": h, "page_count": 7})
    cached = store.is_cached(t, doc_id, pdf)
    assert cached is not None
    assert cached["page_count"] == 7


def test_is_cached_misses_on_change():
    t = _tmp()
    pdf = t / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 original")
    doc_id = "lib-1/ABCD1234"
    store.write_json(store.meta_path(t, doc_id), {"source_hash": "0" * 32})
    assert store.is_cached(t, doc_id, pdf) is None


def test_manifest_round_trip():
    t = _tmp()
    doc_id = "lib-1/ABCD1234"
    manifest = AnchorManifest(
        docId=doc_id,
        sourcePdf="/abs/x.pdf",
        markdownPath=".raw/lib-1/ABCD1234/smith2024.md",
        contentListPath=".raw/lib-1/ABCD1234/content.json",
        assetsRoot="attachments/papers/lib-1/ABCD1234",
        pageDimensions=[PageDimension(pageIdx=0, width=1000, height=1000)],
        anchors=[
            Anchor(
                anchorId="a_text_p1_0000", kind="text", page=1,
                bbox=(0.1, 0.1, 0.5, 0.5), bboxRaw=(100.0, 100.0, 500.0, 500.0),
                contentIndex=0, textPreview="hello",
            )
        ],
    )
    p = store.anchors_path(t, doc_id)
    store.write_json(p, manifest.to_dict())

    loaded = store.load_manifest(t, doc_id)
    assert loaded is not None
    rebuilt = store.manifest_from_dict(loaded)
    assert rebuilt.docId == doc_id
    assert len(rebuilt.anchors) == 1
    assert rebuilt.anchors[0].anchorId == "a_text_p1_0000"
    assert rebuilt.anchors[0].bbox == (0.1, 0.1, 0.5, 0.5)
    assert rebuilt.pageDimensions[0].pageIdx == 0
