import os
import tempfile
from pathlib import Path

from mineru_zotero_mcp import store
from mineru_zotero_mcp.tools import doctor


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def test_format_report_counts_statuses():
    report = doctor._format_report([
        {"status": "OK", "name": "a", "detail": "ready"},
        {"status": "WARN", "name": "b", "detail": "maybe"},
        {"status": "FAIL", "name": "c", "detail": "bad"},
    ])

    assert "Summary: 1 OK, 1 WARN, 1 FAIL" in report
    assert "| FAIL | c | bad |" in report
    assert "Fix FAIL items" in report


def test_check_vault_reports_missing_env():
    old = os.environ.pop("VAULT_ROOT", None)
    checks = []
    try:
        vault = doctor._check_vault(checks)
    finally:
        if old is not None:
            os.environ["VAULT_ROOT"] = old

    assert vault is None
    assert checks == [{"status": "FAIL", "name": "VAULT_ROOT", "detail": "not set"}]


def test_check_parsed_artifacts_reports_complete_doc():
    vault = _tmp()
    doc_id = "lib-1/ABCD1234"
    citekey = "smith2024"
    pdf = vault / "source.pdf"
    pdf.write_bytes(b"%PDF")
    store.write_json(
        store.meta_path(vault, doc_id),
        {
            "doc_id": doc_id,
            "citekey": citekey,
            "source_path": str(pdf),
        },
    )
    store.write_json(
        store.anchors_path(vault, doc_id),
        {"docId": doc_id, "anchors": []},
    )
    store.write_json(store.content_path(vault, doc_id), [])
    store.write_text(store.md_path(vault, doc_id, citekey), "# Paper")

    checks = []
    doctor._check_parsed_artifacts(checks, vault)

    assert checks == [{"status": "OK", "name": "parsed artifacts", "detail": "1 parsed"}]


def test_check_wiki_counts_zotero_indexes_and_sync_markers():
    vault = _tmp()
    wiki = vault / "wiki"
    store.write_text(
        wiki / "sources" / "zotero" / "lib-1" / "items" / "ABCD1234.md",
        "---\ntype: source\nsource_type: paper\n$version: 1\n---\n# A",
    )
    store.write_text(wiki / "sources" / "zotero" / "index.md", "# All")
    store.write_text(wiki / "sources" / "zotero" / "lib-1" / "index.md", "# Library")
    store.write_text(
        wiki / "sources" / "zotero" / "lib-1" / "collections" / "Reading" / "index.md",
        "# Collection",
    )

    checks = []
    doctor._check_wiki(checks, vault)

    assert checks == [{
        "status": "OK",
        "name": "wiki",
        "detail": "1 Zotero item pages, 3 Zotero indexes, 1 Better Notes sync markers",
    }]


def test_escape_table():
    assert doctor._escape_table("a|b\nc") == "a\\|b c"
