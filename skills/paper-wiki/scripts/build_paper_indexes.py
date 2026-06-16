#!/usr/bin/env python3
"""Build Zotero-aware paper indexes from MinerU metadata and wiki source pages."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass
class Paper:
    doc_id: str
    citekey: str
    item_key: str
    library_id: str
    library_name: str
    title: str
    year: str
    venue: str
    status: str
    tags: list[str]
    collection_paths: list[str]
    source_path: str
    has_source_page: bool
    has_raw: bool


def main() -> int:
    parser = argparse.ArgumentParser(description="Build paper indexes under wiki/sources/zotero/.")
    parser.add_argument("--vault", default=os.environ.get("VAULT_ROOT"), help="Obsidian vault root")
    parser.add_argument("--library-id", help="Only build one library index")
    parser.add_argument("--dry-run", action="store_true", help="Print target files without writing")
    args = parser.parse_args()

    if not args.vault:
        raise SystemExit("VAULT_ROOT is not set; pass --vault")
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        raise SystemExit(f"Vault does not exist: {vault}")

    papers = load_papers(vault)
    if args.library_id:
        papers = [p for p in papers if p.library_id == str(args.library_id)]
    for target in build_indexes(vault, papers, dry_run=args.dry_run):
        print(target)
    return 0


def load_papers(vault: Path) -> list[Paper]:
    raw_docs = _load_raw_docs(vault)
    source_docs = _load_paper_source_pages(vault)
    doc_ids = sorted(set(raw_docs) | set(source_docs))
    papers = [_merge_paper(doc_id, raw_docs.get(doc_id, {}), source_docs.get(doc_id, {})) for doc_id in doc_ids]
    return sorted(papers, key=lambda p: (p.library_id, p.year, p.citekey), reverse=True)


def build_indexes(vault: Path, papers: list[Paper], *, dry_run: bool = False) -> list[str]:
    root = vault / "wiki" / "sources" / "zotero"
    targets: list[tuple[Path, str]] = [(root / "index.md", render_index("Zotero Paper Sources", "zotero", papers))]

    by_library: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        by_library[paper.library_id or "unknown"].append(paper)
    for library_id, library_papers in sorted(by_library.items()):
        library_name = next((p.library_name for p in library_papers if p.library_name), "")
        targets.append((
            root / f"lib-{library_id}" / "index.md",
            render_index(
                f"Zotero Library: {library_name or f'lib-{library_id}'}",
                f"library:lib-{library_id}",
                library_papers,
            ),
        ))
        for collection_path, collection_papers in _group_by_collection(library_papers).items():
            target = root / f"lib-{library_id}" / "collections" / _collection_index_path(collection_path)
            targets.append((
                target,
                render_index(
                    f"Zotero Collection: {collection_path}",
                    f"collection:lib-{library_id}:{collection_path}",
                    collection_papers,
                ),
            ))

    written = []
    for path, content in targets:
        written.append(path.relative_to(vault).as_posix())
        if dry_run:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return written


def render_index(title: str, scope: str, papers: list[Paper]) -> str:
    today = date.today().isoformat()
    lines = [
        "---",
        "type: meta",
        f'title: "{title}"',
        f'scope: "{scope}"',
        f"updated: {today}",
        "tags:",
        "  - paper-index",
        "---",
        "",
        f"# {title}",
        "",
        f"Updated: {today}",
        "",
        "## Papers",
        "",
        "| Paper | Year | Venue | Status | Collections | Tags | Zotero |",
        "|---|---:|---|---|---|---|---|",
    ]
    for paper in papers:
        tags = ", ".join(paper.tags)
        collections = "; ".join(paper.collection_paths)
        zotero = f"[Zotero](zotero://select/library/items/{paper.item_key})" if paper.item_key else ""
        lines.append(
            f"| [[{paper.source_path}|{_table(paper.citekey)}]] | {_table(paper.year)} | "
            f"{_table(paper.venue)} | {_table(paper.status)} | {_table(collections)} | "
            f"{_table(tags)} | {zotero} |"
        )

    missing_pages = [p for p in papers if not p.has_source_page]
    missing_raw = [p for p in papers if not p.has_raw]
    lines.extend(["", "## Gaps", ""])
    if not missing_pages and not missing_raw:
        lines.append("- None.")
    else:
        if missing_pages:
            lines.append("- Parsed but no paper source page:")
            lines.extend(f"  - `{p.doc_id}` citekey=`{p.citekey}`" for p in missing_pages)
        if missing_raw:
            lines.append("- Paper source page but no parse artifacts:")
            lines.extend(f"  - `{p.source_path}`" for p in missing_raw)
    lines.append("")
    return "\n".join(lines)


def _load_raw_docs(vault: Path) -> dict[str, dict[str, Any]]:
    raw = vault / ".raw"
    docs: dict[str, dict[str, Any]] = {}
    if not raw.is_dir():
        return docs
    for meta_file in raw.glob("**/meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        doc_id = str(meta.get("doc_id") or meta_file.parent.relative_to(raw).as_posix())
        docs[doc_id] = meta
    return docs


def _load_paper_source_pages(vault: Path) -> dict[str, dict[str, Any]]:
    wiki = vault / "wiki"
    docs: dict[str, dict[str, Any]] = {}
    if not wiki.is_dir():
        return docs
    for page in wiki.glob("**/*.md"):
        if page.name.startswith("_"):
            continue
        frontmatter = _read_frontmatter(page)
        if frontmatter.get("source_type") != "paper":
            continue
        doc_id = str(frontmatter.get("doc_id") or "")
        if not doc_id:
            continue
        frontmatter["source_path"] = page.relative_to(vault).as_posix()
        docs[doc_id] = frontmatter
    return docs


def _merge_paper(doc_id: str, raw: dict[str, Any], source: dict[str, Any]) -> Paper:
    citekey = str(source.get("citekey") or raw.get("citekey") or doc_id.rsplit("/", 1)[-1])
    item_key = str(source.get("item_key") or raw.get("item_key") or doc_id.rsplit("/", 1)[-1])
    library_id = str(source.get("library_id") or raw.get("library_id") or _library_from_doc_id(doc_id))
    library_name = str(source.get("library_name") or raw.get("library_name") or "")
    source_path = str(source.get("source_path") or _default_source_path(library_id, item_key))
    status = str(source.get("status") or ("missing-source-page" if not source else "seed"))
    return Paper(
        doc_id=doc_id,
        citekey=citekey,
        item_key=item_key,
        library_id=library_id,
        library_name=library_name,
        title=str(source.get("title") or raw.get("title") or citekey),
        year=str(source.get("year") or raw.get("year") or ""),
        venue=str(source.get("venue") or raw.get("venue") or ""),
        status=status,
        tags=_as_list(source.get("tags")),
        collection_paths=_collection_paths(source),
        source_path=source_path,
        has_source_page=bool(source),
        has_raw=bool(raw),
    )


def _read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    return _parse_simple_yaml(text[4:end])


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(_clean_scalar(line[4:]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        data[current_key] = [] if value == "" else _clean_scalar(value)
    return data


def _clean_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def _collection_paths(source: dict[str, Any]) -> list[str]:
    values = _as_list(source.get("collection_paths"))
    if not values and source.get("collection_path"):
        values = [str(source["collection_path"])]
    return [v.strip("/") for v in values if v.strip("/")]


def _group_by_collection(papers: list[Paper]) -> dict[str, list[Paper]]:
    grouped: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        for collection_path in paper.collection_paths:
            grouped[collection_path].append(paper)
    return dict(sorted(grouped.items()))


def _default_source_path(library_id: str, item_key: str) -> str:
    return f"wiki/sources/zotero/lib-{library_id}/items/{_slug_segment(item_key)}.md"


def _collection_index_path(collection_path: str) -> Path:
    parts = [_slug_segment(p) for p in collection_path.split("/") if p.strip()]
    return Path(*parts, "index.md") if parts else Path("index.md")


def _slug_segment(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_\-+.]+", "-", value.strip()).strip("-._")
    return value or "untitled"


def _library_from_doc_id(doc_id: str) -> str:
    first = doc_id.split("/", 1)[0]
    return first.removeprefix("lib-")


def _table(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
