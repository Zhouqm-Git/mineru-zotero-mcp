# mineru-zotero-mcp

An MCP server that bridges **Zotero** PDFs and **MinerU** extraction for evidence-grounded paper reading in **Obsidian**.

It is the companion to [`zotero-mcp`](https://github.com/54yyyu/zotero-mcp): where zotero-mcp manages your Zotero library (metadata, annotations, notes, search), this server fills the one gap zotero-mcp does not cover — turning a Zotero PDF into structured Markdown (with page-anchored blocks, tables-as-markdown, and high-quality figure captures) for an Obsidian vault.

## What it does

`Zotero item → doc_id (library + item key) → MinerU parse → Obsidian vault`

`citekey` is bibliographic metadata only. Storage identity is always `doc_id`
(`lib-<libraryID>/<item_key>`) so the same paper or citation key can exist in
multiple Zotero libraries without colliding.

Produces, per paper:

- `.raw/<doc_id>/<citekey>.md` — full MinerU Markdown (tables are GFM pipe tables, not images)
- `.raw/<doc_id>/anchors.json` — each text/image/table/equation/list block mapped back to a PDF page + bbox
- `.raw/<doc_id>/content.json` — raw MinerU content_list
- `.raw/<doc_id>/meta.json` — parse metadata + content-hash cache
- `attachments/papers/<doc_id>/` — extracted and re-rendered figures/captures for Obsidian embeds

## Tools (7)

| Tool | Purpose |
|---|---|
| `mineru_parse_pdf` | Parse a single Zotero PDF via MinerU; returns `doc_id` |
| `mineru_parse_batch` | Batch-parse (≤50) with polling/callback |
| `mineru_list_anchors` / `mineru_resolve_anchor` | Query the block→bbox mapping by `doc_id` |
| `mineru_read_markdown` | Read parsed md by `doc_id`, optionally sliced by page |
| `mineru_capture_region` | Fresh PDF region capture (PyMuPDF) saved under `attachments/papers/<doc_id>/` |
| `mineru_list_visual_candidates` | List parse-time merged figure candidates by `doc_id` |

annotation/note/search are deliberately **not** implemented — those belong to zotero-mcp.

## Configuration

```
MINERU_API_TOKEN=xxx         # mineru.net API token
VAULT_ROOT=/path/to/vault    # .raw/ + attachments/papers/ land here
ZOTERO_LOCAL=true            # reuse zotero-mcp local mode (reads ~/Zotero/zotero.sqlite)
```

zotero-mcp's own config (`~/.config/zotero-mcp/config.json`) is reused for the Zotero DB path and BetterBibTeX port.

## Installation

```bash
cd mineru-zotero-mcp
pip install -e .
mineru-zotero-mcp
```
