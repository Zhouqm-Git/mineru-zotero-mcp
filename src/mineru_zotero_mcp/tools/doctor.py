"""Health checks for the Zotero -> MinerU -> Obsidian paper-wiki stack."""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Any

from .._app import mcp
from ..store import content_path, load_manifest, load_meta, md_path
from ..zotero_bridge import _local_reader_factory, _zotero_db_path
from .catalog import _iter_parsed_documents

_BBT_DEFAULT_PORT = 23119
_FRONTMATTER_VERSION_RE = re.compile(r"^\$version\s*:", re.MULTILINE)


@mcp.tool(
    name="mineru_doctor",
    description=(
        "Run a read-only health check for the paper-wiki stack: VAULT_ROOT, "
        "MinerU env, parsed .raw artifacts, Zotero local DB bridge, "
        "BetterBibTeX local port, note layout, and Better Notes sync markers. "
        "Call this before batch parsing, library indexing, or cross-paper QA."
    ),
)
def doctor_tool() -> str:
    checks: list[dict[str, str]] = []
    vault = _check_vault(checks)
    _check_mineru_env(checks)
    if vault is not None:
        _check_vault_layout(checks, vault)
        _check_parsed_artifacts(checks, vault)
        _check_notes(checks, vault)
    _check_zotero_bridge(checks)
    _check_better_bibtex(checks)

    return _format_report(checks)


def _check_vault(checks: list[dict[str, str]]) -> Path | None:
    raw = os.environ.get("VAULT_ROOT", "").strip()
    if not raw:
        _add(checks, "FAIL", "VAULT_ROOT", "not set")
        return None
    vault = Path(raw).expanduser()
    if not vault.is_dir():
        _add(checks, "FAIL", "VAULT_ROOT", f"not a directory: {vault}")
        return None
    if not os.access(vault, os.R_OK | os.W_OK):
        _add(checks, "FAIL", "VAULT_ROOT", f"not readable/writable: {vault}")
        return vault
    _add(checks, "OK", "VAULT_ROOT", str(vault))
    return vault


def _check_mineru_env(checks: list[dict[str, str]]) -> None:
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if token:
        _add(checks, "OK", "MINERU_API_TOKEN", "set")
    else:
        _add(checks, "FAIL", "MINERU_API_TOKEN", "not set")
    base = os.environ.get("MINERU_BASE_URL", "https://mineru.net").strip()
    _add(checks, "OK", "MINERU_BASE_URL", base or "https://mineru.net")


def _check_vault_layout(checks: list[dict[str, str]], vault: Path) -> None:
    expected = [
        (".raw", vault / ".raw"),
        ("attachments/papers", vault / "attachments" / "papers"),
        ("notes", vault / "notes"),
    ]
    for label, path in expected:
        if path.is_dir():
            _add(checks, "OK", label, "exists")
        else:
            level = "WARN" if label != ".raw" else "FAIL"
            _add(checks, level, label, "missing")


def _check_parsed_artifacts(checks: list[dict[str, str]], vault: Path) -> None:
    docs = _iter_parsed_documents(vault)
    if not docs:
        _add(checks, "WARN", "parsed documents", "none found under .raw/**/meta.json")
        return

    missing_anchors = 0
    missing_content = 0
    missing_markdown = 0
    missing_pdf = 0
    for doc in docs:
        doc_id = doc["doc_id"]
        meta = load_meta(vault, doc_id) or {}
        if load_manifest(vault, doc_id) is None:
            missing_anchors += 1
        if not content_path(vault, doc_id).is_file():
            missing_content += 1
        citekey = meta.get("citekey") or doc.get("citekey")
        if not md_path(vault, doc_id, citekey).is_file():
            missing_markdown += 1
        source_path = meta.get("source_path")
        if source_path and not Path(source_path).expanduser().is_file():
            missing_pdf += 1

    detail = f"{len(docs)} parsed"
    problems = []
    if missing_anchors:
        problems.append(f"{missing_anchors} missing anchors")
    if missing_content:
        problems.append(f"{missing_content} missing content")
    if missing_markdown:
        problems.append(f"{missing_markdown} missing markdown")
    if missing_pdf:
        problems.append(f"{missing_pdf} source PDFs unavailable")
    if problems:
        _add(checks, "WARN", "parsed artifacts", detail + "; " + ", ".join(problems))
    else:
        _add(checks, "OK", "parsed artifacts", detail)


def _check_notes(checks: list[dict[str, str]], vault: Path) -> None:
    notes_dir = vault / "notes"
    if not notes_dir.is_dir():
        return
    notes = list(notes_dir.glob("**/*.md"))
    canonical = [p for p in notes if _looks_like_canonical_note(notes_dir, p)]
    indexes = [p for p in notes if p.name == "index.md" or p.name == "_index.md"]
    synced = 0
    for p in notes:
        try:
            if _FRONTMATTER_VERSION_RE.search(p.read_text(encoding="utf-8", errors="ignore")):
                synced += 1
        except OSError:
            continue
    _add(
        checks,
        "OK" if notes else "WARN",
        "notes",
        f"{len(canonical)} canonical, {len(indexes)} indexes, {synced} Better Notes sync markers",
    )


def _looks_like_canonical_note(notes_dir: Path, path: Path) -> bool:
    rel = path.relative_to(notes_dir)
    return len(rel.parts) >= 3 and rel.parts[0].startswith("lib-") and path.name != "index.md"


def _check_zotero_bridge(checks: list[dict[str, str]]) -> None:
    try:
        db_path = _zotero_db_path()
    except Exception as e:  # noqa: BLE001
        _add(checks, "FAIL", "zotero-mcp config", str(e))
        return
    if not db_path:
        _add(checks, "WARN", "zotero-mcp config", "zotero_db_path not configured")
        return
    db = Path(db_path).expanduser()
    if not db.is_file():
        _add(checks, "FAIL", "Zotero SQLite", f"not found: {db}")
        return
    try:
        Reader = _local_reader_factory()
        with Reader(db_path=str(db)) as reader:
            conn = reader._get_connection()
            count = conn.execute(
                "SELECT COUNT(*) FROM items WHERE itemID NOT IN (SELECT itemID FROM deletedItems)"
            ).fetchone()[0]
        _add(checks, "OK", "Zotero SQLite", f"readable, {count} active items")
    except Exception as e:  # noqa: BLE001
        _add(checks, "FAIL", "Zotero SQLite", f"cannot read: {e}")


def _check_better_bibtex(checks: list[dict[str, str]]) -> None:
    raw_port = os.environ.get("ZOTERO_BBT_PORT", str(_BBT_DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        _add(checks, "WARN", "BetterBibTeX JSON-RPC", f"invalid ZOTERO_BBT_PORT: {raw_port}")
        return
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            pass
        _add(checks, "OK", "BetterBibTeX JSON-RPC", f"127.0.0.1:{port} reachable")
    except OSError:
        _add(
            checks,
            "WARN",
            "BetterBibTeX JSON-RPC",
            f"127.0.0.1:{port} unreachable; citekey lookup may need Zotero desktop running",
        )


def _add(checks: list[dict[str, str]], status: str, name: str, detail: str) -> None:
    checks.append({"status": status, "name": name, "detail": detail})


def _format_report(checks: list[dict[str, str]]) -> str:
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("OK", "WARN", "FAIL")}
    lines = [
        "# paper-wiki doctor",
        "",
        f"Summary: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL",
        "",
        "| Status | Check | Detail |",
        "|---|---|---|",
    ]
    for c in checks:
        lines.append(f"| {c['status']} | {c['name']} | {_escape_table(c['detail'])} |")

    lines.extend(["", "## Next steps"])
    if counts["FAIL"]:
        lines.append("- Fix FAIL items before running batch parse or index generation.")
    if counts["WARN"]:
        lines.append("- Review WARN items; they are often acceptable for read-only or first-run workflows.")
    if not counts["FAIL"] and not counts["WARN"]:
        lines.append("- Stack is ready for parsing, indexing, and cross-paper evidence search.")
    return "\n".join(lines)


def _escape_table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
