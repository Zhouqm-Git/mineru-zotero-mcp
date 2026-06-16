---
name: paper-wiki
description: "Zotero-library-aware paper wiki bundle for mineru-zotero-mcp + claude-obsidian. Use when parsing, reading, annotating, ingesting, indexing, comparing, or querying Zotero papers in an Obsidian wiki while preserving Zotero library, item, and collection structure. Depends on mineru-zotero-mcp MCP tools for PDF parsing and anchors, and lets claude-obsidian wiki, retrieve, hooks, commands, and agents consume the Zotero-aware wiki tree rather than flattening papers into generic notes."
---

# paper-wiki: Zotero/MinerU Adapter for claude-obsidian

Use this skill as the paper-specific bridge between Zotero, MinerU, and claude-obsidian.

Do not create a parallel `notes/` knowledge base. User-facing knowledge goes into the existing `wiki/` layer. MinerU artifacts stay hidden in `.raw/<doc_id>/`, and figures/captures stay visible in `attachments/papers/<doc_id>/`.

Preserve Zotero as the paper knowledge backbone. `doc_id = lib-<libraryID>/<item_key>` is the canonical identity; collection folders are navigation/index views, not duplicated paper pages.

## Zotero-Aware Layout

Use this vault layout:

```text
.raw/<doc_id>/                                           MinerU parse/cache artifacts
attachments/papers/<doc_id>/                             visible figures/captures
wiki/sources/zotero/index.md                             all parsed/indexed papers
wiki/sources/zotero/lib-<libraryID>/index.md             one Zotero library index
wiki/sources/zotero/lib-<libraryID>/items/<item_key>.md   canonical paper source page
wiki/sources/zotero/lib-<libraryID>/collections/.../index.md
                                                          Zotero collection folder indexes
wiki/questions/<slug>.md                                 cross-paper answers
wiki/comparisons/<slug>.md                               comparison matrices
```

If one Zotero item appears in multiple collections, keep one paper page under `items/<item_key>.md` and link it from every relevant collection `index.md`. If the same paper exists in different Zotero libraries, keep separate pages because the library-scoped `doc_id` is different.

## Inherited claude-obsidian Contracts

Before writing wiki pages, follow the same contracts as `claude-obsidian/skills`:

- Read `wiki/hot.md` first when it exists.
- Use `wiki/index.md` and `wiki-retrieve` for existing synthesized knowledge.
- Use `scripts/wiki-mode.py route ...` for generic entity/concept/question pages.
- Use `scripts/wiki-lock.sh acquire/release` before writing wiki pages when available.
- Update `wiki/index.md`, `wiki/log.md`, and `wiki/hot.md` after meaningful changes.
- Leave `.raw/` immutable except for MinerU parse/cache artifacts written by MCP tools.

For paper source pages, prefer the Zotero-aware paths above over generic `wiki-mode.py route source` output. This is the part claude-obsidian adapts to this project, not the reverse.

- Paper source page: `wiki/sources/zotero/lib-<libraryID>/items/<item_key>.md`
- Paper domain index: `wiki/sources/zotero/index.md`
- Library index: `wiki/sources/zotero/lib-<libraryID>/index.md`
- Collection index: `wiki/sources/zotero/lib-<libraryID>/collections/<collection-path>/index.md`
- Question page: `wiki/questions/<slug>.md`
- Comparison page: `wiki/comparisons/<slug>.md`

## MCP Tools

Use the local `mineru-zotero-mcp` server:

- `mineru_doctor`
- `mineru_parse_pdf`
- `mineru_parse_batch`
- `mineru_check_quota`
- `mineru_list_documents`
- `mineru_search_evidence`
- `mineru_read_markdown`
- `mineru_list_anchors`
- `mineru_resolve_anchor`
- `mineru_list_visual_candidates`
- `mineru_capture_region`
- `mineru_create_evidence_annotation`

Use `zotero-mcp` for Zotero metadata, collection discovery, notes, search, tags, and low-level writes.

## Operations

### Preflight

Run `mineru_doctor` before batch ingest, indexing, or cross-paper synthesis. Stop on `FAIL`; continue past `WARN` only if the requested operation does not require the warned capability.

### Single Paper Ingest

1. Parse or reuse parsed artifacts:
   ```text
   mineru_parse_pdf(item_key="CFSHQZRJ", library_id=1)
   ```
2. Read targeted pages with `mineru_read_markdown`.
3. Collect evidence with anchors/candidates:
   - `mineru_list_anchors`
   - `mineru_resolve_anchor`
   - `mineru_list_visual_candidates`
4. Create a paper source page through the claude-obsidian wiki layer.
5. Add method/dataset/entity/concept pages only when they are reusable.
6. Update wiki index/log/hot cache.

Use paper source frontmatter:

```yaml
---
type: source
source_type: paper
title: "Full Paper Title"
doc_id: lib-1/ABCD1234
citekey: smith2024
item_key: ABCD1234
library_id: 1
library_name: "My Library"
collection_paths:
  - "Reading/Transformers"
authors:
  - "First Author"
year: 2024
venue: "NeurIPS"
status: seed
paper_type: empirical
tags:
  - paper
---
```

### Batch Ingest

1. Resolve Zotero scope with `zotero-mcp` collections/search tools, retaining `library_id`, `library_name`, and collection path membership.
2. Run `mineru_check_quota`.
3. Parse missing PDFs with `mineru_parse_batch`; retry edge cases with `mineru_parse_pdf`.
4. Create compact paper source pages under `wiki/sources/zotero/lib-<libraryID>/items/<item_key>.md` for parsed items that lack pages.
5. Refresh paper sub-indexes:
   ```bash
   python3 skills/paper-wiki/scripts/build_paper_indexes.py --vault "$VAULT_ROOT"
   ```
6. Refresh claude-obsidian retrieval when provisioned:
   ```bash
   python3 scripts/contextual-prefix.py --all
   python3 scripts/bm25-index.py build
   ```

Batch ingest should create reusable parsed artifacts and source pages. Do not deep-read every paper unless the user asks.

### Cross-Paper Query

Use `wiki-retrieve` first for synthesized wiki pages. Use `mineru_search_evidence` for exact PDF evidence:

```text
mineru_search_evidence(query="dense reranker", match="all", limit=20)
mineru_resolve_anchor(doc_id="lib-1/ABCD1234", anchor_id="a_table_p5_0000")
```

File durable answers under `wiki/questions/` and include paper source links plus page/anchor evidence.

### Comparison

For methods, datasets, baselines, metrics, limitations, or reproducibility comparisons:

1. Establish scope from Zotero, wiki index, or explicit `doc_ids`.
2. Search exact evidence with `match="all"` when possible.
3. Resolve strongest anchors.
4. Read paper source pages for interpretation.
5. Write `wiki/comparisons/<slug>.md` with a matrix.

Use `not found` for unsupported cells. Do not infer missing baselines from memory.

### Zotero Evidence Annotations

Create annotations only for evidence worth revisiting in Zotero:

```text
mineru_create_evidence_annotation(
  doc_id="lib-1/CFSHQZRJ",
  anchor_id="a_text_p3_0002",
  comment="core method evidence",
  mode="auto"
)
```

The tool resolves attachment key, page, text, and bbox.

## Maintenance Scripts

- `scripts/build_paper_indexes.py`: generated paper/library/collection indexes under `wiki/sources/zotero/`.
- `scripts/lint_paper_wiki.py`: read-only consistency check for paper source pages, `.raw/<doc_id>`, anchors, and embeds.

Run lint after batch ingest:

```bash
python3 skills/paper-wiki/scripts/lint_paper_wiki.py --vault "$VAULT_ROOT"
```

## Better Notes Boundary

Better Notes can mirror a paper source page back to Zotero, but external agents cannot reliably configure Auto-Sync from outside Zotero.

Report the sync path after writing a source page:

```text
Better Notes sync path: wiki/sources/zotero/lib-<libraryID>/items/<item_key>.md
Status: configured only if frontmatter contains $version; otherwise pending Zotero-side Set Auto-Sync.
```

## Do Not

- Do not create or maintain a standalone `notes/` paper architecture in claude-obsidian vaults.
- Do not flatten Zotero library structure into generic `wiki/sources/papers/<citekey>.md`.
- Do not duplicate canonical paper source pages into collection folders; collection folders contain `index.md` link views.
- Do not bypass claude-obsidian `wiki-index/log/hot` maintenance.
- Do not make PageIndex a default dependency; keep it optional under `.raw/<doc_id>/pageindex/` if later adopted.
- Do not answer paper-specific claims from model memory when parsed anchors or wiki pages exist.
