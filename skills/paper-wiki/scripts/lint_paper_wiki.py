#!/usr/bin/env python3
"""Lint claude-obsidian paper source pages against MinerU artifacts."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from build_paper_indexes import _read_frontmatter  # noqa: E402

_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
_REQUIRED = ("type", "source_type", "title", "doc_id", "citekey", "item_key", "status")


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint paper-wiki source page consistency.")
    parser.add_argument("--vault", default=os.environ.get("VAULT_ROOT"), help="Obsidian vault root")
    args = parser.parse_args()
    if not args.vault:
        raise SystemExit("VAULT_ROOT is not set; pass --vault")
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        raise SystemExit(f"Vault does not exist: {vault}")
    issues = lint(vault)
    print(render_report(issues))
    return 1 if any(i[0] == "FAIL" for i in issues) else 0


def lint(vault: Path) -> list[tuple[str, str, str]]:
    issues: list[tuple[str, str, str]] = []
    pages = _paper_source_pages(vault)
    if not pages:
        issues.append(("WARN", "wiki source pages", "no source_type=paper pages under wiki/"))
    for page in pages:
        _lint_page(vault, page, issues)
    if not (vault / "wiki" / "sources" / "zotero" / "index.md").is_file():
        issues.append(("WARN", "wiki/sources/zotero/index.md", "missing; run build_paper_indexes.py"))
    return issues


def render_report(issues: list[tuple[str, str, str]]) -> str:
    counts = {s: sum(1 for i in issues if i[0] == s) for s in ("OK", "WARN", "FAIL")}
    lines = [
        "# paper-wiki lint",
        "",
        f"Summary: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL",
        "",
        "| Status | Check | Detail |",
        "|---|---|---|",
    ]
    for status, check, detail in issues:
        lines.append(f"| {status} | {_table(check)} | {_table(detail)} |")
    return "\n".join(lines)


def _paper_source_pages(vault: Path) -> list[Path]:
    wiki = vault / "wiki"
    if not wiki.is_dir():
        return []
    pages = []
    for page in wiki.glob("**/*.md"):
        fm = _read_frontmatter(page)
        if fm.get("source_type") == "paper":
            pages.append(page)
    return sorted(pages)


def _lint_page(vault: Path, page: Path, issues: list[tuple[str, str, str]]) -> None:
    rel = page.relative_to(vault).as_posix()
    fm = _read_frontmatter(page)
    missing = [key for key in _REQUIRED if not fm.get(key)]
    if missing:
        issues.append(("FAIL", rel, "missing frontmatter: " + ", ".join(missing)))
    else:
        issues.append(("OK", rel, "frontmatter complete"))

    doc_id = str(fm.get("doc_id") or "")
    raw_dir = vault / ".raw" / doc_id
    if not doc_id:
        return
    if not raw_dir.is_dir():
        issues.append(("FAIL", rel, f"missing raw artifacts: .raw/{doc_id}"))
    elif not (raw_dir / "anchors.json").is_file():
        issues.append(("FAIL", rel, f"missing anchors: .raw/{doc_id}/anchors.json"))
    else:
        issues.append(("OK", rel, f"raw artifacts present: .raw/{doc_id}"))

    try:
        text = page.read_text(encoding="utf-8")
    except OSError as e:
        issues.append(("FAIL", rel, f"cannot read page: {e}"))
        return
    for embed in _EMBED_RE.findall(text):
        if not (vault / embed).exists():
            issues.append(("FAIL", rel, f"missing embed: {embed}"))


def _table(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
