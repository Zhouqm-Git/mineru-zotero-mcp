import os
import tempfile
from pathlib import Path

from mineru_zotero_mcp import store
from mineru_zotero_mcp.tools import catalog


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _write_doc(vault: Path, doc_id: str, citekey: str, text: str, library_id: int = 1) -> None:
    store.write_json(
        store.meta_path(vault, doc_id),
        {
            "doc_id": doc_id,
            "citekey": citekey,
            "item_key": doc_id.rsplit("/", 1)[-1],
            "library_id": library_id,
            "library_name": f"Library {library_id}",
            "page_count": 7,
            "table_count": 1,
            "image_count": 2,
            "cached_at": 1000 + library_id,
            "source_path": f"/papers/{citekey}.pdf",
        },
    )
    store.write_json(
        store.anchors_path(vault, doc_id),
        {
            "docId": doc_id,
            "anchors": [
                {
                    "anchorId": "a_text_p2_0000",
                    "kind": "text",
                    "page": 2,
                    "bbox": [0.1, 0.1, 0.5, 0.2],
                    "contentIndex": 0,
                    "textPreview": text[:60],
                },
                {
                    "anchorId": "a_table_p5_0000",
                    "kind": "table",
                    "page": 5,
                    "bbox": [0.1, 0.3, 0.8, 0.7],
                    "contentIndex": 1,
                    "markdownTable": "| Method | Score |\n|---|---|\n| BM25 | 71 |",
                },
            ],
        },
    )
    store.write_json(
        store.content_path(vault, doc_id),
        [
            {"type": "text", "text": text},
            {"type": "table", "table_body": "BM25 dense reranker comparison"},
        ],
    )


def test_iter_parsed_documents_reads_meta_tree():
    vault = _tmp()
    _write_doc(vault, "lib-1/ABCD1234", "smith2024", "contrastive learning")

    docs = catalog._iter_parsed_documents(vault)

    assert len(docs) == 1
    assert docs[0]["doc_id"] == "lib-1/ABCD1234"
    assert docs[0]["citekey"] == "smith2024"
    assert docs[0]["page_count"] == 7


def test_filter_documents_by_library_and_query():
    docs = [
        {"doc_id": "lib-1/A", "citekey": "alpha", "library_id": 1, "source_path": "/x/a.pdf"},
        {"doc_id": "lib-2/B", "citekey": "beta", "library_id": 2, "source_path": "/x/b.pdf"},
    ]

    filtered = catalog._filter_documents(docs, library_id=2, query="beta")

    assert [d["doc_id"] for d in filtered] == ["lib-2/B"]


def test_search_evidence_tool_returns_cross_doc_anchor_matches():
    vault = _tmp()
    _write_doc(
        vault,
        "lib-1/ABCD1234",
        "smith2024",
        "The experiments compare BM25 with a dense reranker on retrieval tasks.",
    )
    old = os.environ.get("VAULT_ROOT")
    os.environ["VAULT_ROOT"] = str(vault)
    try:
        result = catalog.search_evidence_tool("dense reranker", limit=5)
    finally:
        if old is None:
            os.environ.pop("VAULT_ROOT", None)
        else:
            os.environ["VAULT_ROOT"] = old

    assert "`lib-1/ABCD1234`" in result
    assert "`a_text_p2_0000`" in result
    assert "dense reranker" in result
    assert "mineru_resolve_anchor" in result


def test_search_evidence_tool_all_match_filters_partial_hits():
    vault = _tmp()
    _write_doc(vault, "lib-1/ABCD1234", "smith2024", "dense reranker")
    _write_doc(vault, "lib-1/EFGH5678", "jones2025", "dense model only")
    old = os.environ.get("VAULT_ROOT")
    os.environ["VAULT_ROOT"] = str(vault)
    try:
        result = catalog.search_evidence_tool("dense reranker", kind="text", match="all", limit=5)
    finally:
        if old is None:
            os.environ.pop("VAULT_ROOT", None)
        else:
            os.environ["VAULT_ROOT"] = old

    assert "`lib-1/ABCD1234`" in result
    assert "`lib-1/EFGH5678`" not in result
    assert "Match mode: all" in result


def test_list_documents_tool_shows_metadata():
    vault = _tmp()
    _write_doc(vault, "lib-1/ABCD1234", "smith2024", "hello")
    old = os.environ.get("VAULT_ROOT")
    os.environ["VAULT_ROOT"] = str(vault)
    try:
        result = catalog.list_documents_tool()
    finally:
        if old is None:
            os.environ.pop("VAULT_ROOT", None)
        else:
            os.environ["VAULT_ROOT"] = old

    assert "# Parsed MinerU documents (1)" in result
    assert "citekey=`smith2024`" in result
    assert "7p" in result
