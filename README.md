# mineru-zotero-mcp

An MCP server that bridges **Zotero** PDFs and **MinerU** extraction for evidence-grounded paper reading in **Obsidian**.

It is the companion to [`zotero-mcp`](https://github.com/54yyyu/zotero-mcp): where zotero-mcp manages your Zotero library (metadata, annotations, notes, search), this server fills the one gap zotero-mcp does not cover — turning a Zotero PDF into structured Markdown (with page-anchored blocks, tables-as-markdown, and high-quality figure captures) for an Obsidian vault.

## What it does

`Zotero item → citation key → MinerU parse → Obsidian vault .raw/<citekey>/`

Produces, per paper:

- `<citekey>.md` — full MinerU Markdown (tables are GFM pipe tables, not images)
- `anchors.json` — each text/image/table/equation/list block mapped back to a PDF page + bbox
- `content.json` — raw MinerU content_list
- `assets/` — extracted figures
- `meta.json` — parse metadata + content-hash cache

## Tools (7)

| Tool | Purpose |
|---|---|
| `mineru_parse_pdf` | Parse a single Zotero PDF via MinerU |
| `mineru_parse_batch` | Batch-parse (≤50) with polling/callback |
| `mineru_list_anchors` / `mineru_resolve_anchor` | Query the block→bbox mapping |
| `mineru_read_markdown` | Read parsed md, optionally sliced by page |
| `mineru_capture_region` | Fresh PDF region capture (PyMuPDF), with auto-merge of fragmented figures |
| `mineru_list_visual_candidates` | Detect figure-fragment groups to merge |

annotation/note/search are deliberately **not** implemented — those belong to zotero-mcp.

## Configuration

```
MINERU_API_TOKEN=xxx         # mineru.net API token
VAULT_ROOT=/path/to/vault    # Obsidian vault root (.raw/ lands here)
ZOTERO_LOCAL=true            # reuse zotero-mcp local mode (reads ~/Zotero/zotero.sqlite)
```

zotero-mcp's own config (`~/.config/zotero-mcp/config.json`) is reused for the Zotero DB path and BetterBibTeX port.

## Installation

```bash
cd mineru-zotero-mcp
pip install -e .
mineru-zotero-mcp
```
