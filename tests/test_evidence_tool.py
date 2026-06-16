"""Tests for high-level evidence annotation helpers.

These tests avoid real Zotero writes; they validate the deterministic mapping
from MinerU anchors to annotation modes, rectangles, text snippets, and tags.
"""

import os
import tempfile
from pathlib import Path

from mineru_zotero_mcp import store
from mineru_zotero_mcp.tools import evidence


def test_choose_mode_auto_text_for_text_anchor():
    assert evidence._choose_mode({"kind": "text"}, "auto") == "text"
    assert evidence._choose_mode({"kind": "list"}, "auto") == "text"


def test_choose_mode_auto_area_for_visual_anchors():
    assert evidence._choose_mode({"kind": "image"}, "auto") == "area"
    assert evidence._choose_mode({"kind": "table"}, "auto") == "area"
    assert evidence._choose_mode({"kind": "equation"}, "auto") == "area"


def test_bbox_to_rect_converts_x2_y2_to_width_height():
    assert evidence._bbox_to_rect([0.1, 0.2, 0.4, 0.8]) == (
        0.1,
        0.2,
        0.30000000000000004,
        0.6000000000000001,
    )


def test_bbox_to_rect_rejects_invalid_box():
    try:
        evidence._bbox_to_rect([0.4, 0.2, 0.1, 0.8])
    except ValueError as e:
        assert "invalid anchor bbox" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_trim_annotation_text_normalizes_and_limits_at_word_boundary():
    text = "  This   is a long\npiece of text that should be trimmed cleanly. "
    assert evidence._trim_annotation_text(text, 25) == "This is a long piece of"


def test_merge_tags_adds_defaults_and_dedupes():
    assert evidence._merge_tags(["paper-wiki", "method"]) == [
        "paper-wiki",
        "evidence",
        "method",
    ]
    assert evidence._merge_tags("risk, method") == [
        "paper-wiki",
        "evidence",
        "risk",
        "method",
    ]


def test_extract_annotation_key():
    result = "Successfully created highlight annotation\n\n**Annotation Key:** ABC12345"
    assert evidence._extract_annotation_key(result) == "ABC12345"


def test_is_failure_result():
    assert evidence._is_failure_result("Error: missing credentials")
    assert evidence._is_failure_result("Failed to create annotation: {}")
    assert not evidence._is_failure_result("Successfully created highlight annotation")


def test_create_evidence_annotation_tool_text_anchor_without_real_zotero():
    vault = Path(tempfile.mkdtemp())
    doc_id = "lib-1/ABCD1234"
    _write_doc(vault, doc_id)

    calls = {}
    old_env = os.environ.get("VAULT_ROOT")
    old_attachment = evidence.get_pdf_attachment_key_for_item
    old_text_call = evidence._call_zotero_create_annotation
    try:
        os.environ["VAULT_ROOT"] = str(vault)
        evidence.get_pdf_attachment_key_for_item = lambda *args, **kwargs: "ATTACH01"

        def fake_create_annotation(**kwargs):
            calls.update(kwargs)
            return "Successfully created highlight annotation\n\n**Annotation Key:** ANNO0001"

        evidence._call_zotero_create_annotation = fake_create_annotation
        out = evidence.create_evidence_annotation_tool(
            doc_id=doc_id,
            anchor_id="a_text_p1_0000",
            comment="core claim",
        )
    finally:
        _restore_env(old_env)
        evidence.get_pdf_attachment_key_for_item = old_attachment
        evidence._call_zotero_create_annotation = old_text_call

    assert "annotation_key: `ANNO0001`" in out
    assert calls["attachment_key"] == "ATTACH01"
    assert calls["page"] == 1
    assert calls["text"] == "This is the full text from content.json used for highlighting."
    assert calls["color"] == "#a28ae5"
    assert calls["tags"] == ["paper-wiki", "evidence"]


def test_create_evidence_annotation_tool_area_anchor_without_real_zotero():
    vault = Path(tempfile.mkdtemp())
    doc_id = "lib-1/ABCD1234"
    _write_doc(vault, doc_id)

    calls = {}
    old_env = os.environ.get("VAULT_ROOT")
    old_attachment = evidence.get_pdf_attachment_key_for_item
    old_area_call = evidence._call_zotero_create_area_annotation
    try:
        os.environ["VAULT_ROOT"] = str(vault)
        evidence.get_pdf_attachment_key_for_item = lambda *args, **kwargs: "ATTACH01"

        def fake_create_area_annotation(**kwargs):
            calls.update(kwargs)
            return "Successfully created area annotation\n\n**Annotation Key:** ANNO0002"

        evidence._call_zotero_create_area_annotation = fake_create_area_annotation
        out = evidence.create_evidence_annotation_tool(
            doc_id=doc_id,
            anchor_id="a_table_p2_0000",
            mode="auto",
            tags=["result"],
        )
    finally:
        _restore_env(old_env)
        evidence.get_pdf_attachment_key_for_item = old_attachment
        evidence._call_zotero_create_area_annotation = old_area_call

    assert "mode: `area`" in out
    assert "annotation_key: `ANNO0002`" in out
    assert calls["attachment_key"] == "ATTACH01"
    assert calls["page"] == 2
    assert calls["x"] == 0.2
    assert calls["y"] == 0.3
    assert calls["width"] == 0.39999999999999997
    assert calls["height"] == 0.3
    assert calls["tags"] == ["paper-wiki", "evidence", "result"]


def _write_doc(vault: Path, doc_id: str) -> None:
    store.write_json(
        store.meta_path(vault, doc_id),
        {
            "doc_id": doc_id,
            "item_key": "ABCD1234",
            "library_id": 1,
            "source_path": "/tmp/source.pdf",
        },
    )
    store.write_json(
        store.anchors_path(vault, doc_id),
        {
            "docId": doc_id,
            "sourcePdf": "/tmp/source.pdf",
            "anchors": [
                {
                    "anchorId": "a_text_p1_0000",
                    "kind": "text",
                    "page": 1,
                    "bbox": [0.1, 0.1, 0.5, 0.2],
                    "contentIndex": 0,
                    "textPreview": "preview",
                },
                {
                    "anchorId": "a_table_p2_0000",
                    "kind": "table",
                    "page": 2,
                    "bbox": [0.2, 0.3, 0.6, 0.6],
                    "contentIndex": 1,
                    "caption": "Main results",
                },
            ],
        },
    )
    store.write_json(
        store.content_path(vault, doc_id),
        [
            {"type": "text", "text": "This is the full text from content.json used for highlighting."},
            {"type": "table", "table_caption": ["Main results"]},
        ],
    )


def _restore_env(old_value: str | None) -> None:
    if old_value is None:
        os.environ.pop("VAULT_ROOT", None)
    else:
        os.environ["VAULT_ROOT"] = old_value
