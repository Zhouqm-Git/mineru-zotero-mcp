---
description: Run the Zotero-library-aware MinerU paper workflow inside a claude-obsidian vault.
---

Read `skills/paper-wiki/SKILL.md`.

Then:

1. Run `mineru_doctor`.
2. If the user asked to ingest papers, resolve Zotero library/collection scope, check quota, parse missing PDFs, and file source pages into `wiki/sources/zotero/<collection_path>/<citekey>.md` (route via `scripts/wiki-mode.py route paper "<citekey>" --collection-path "<path>"`).
3. If the user asked a question/comparison, use `wiki-retrieve` first when available, then `mineru_search_evidence` for exact PDF evidence.
4. After writing meaningful wiki pages, update `wiki/index.md`, `wiki/log.md`, and `wiki/hot.md`.
5. For generated Zotero collection paper indexes, run:
   ```bash
   python3 skills/paper-wiki/scripts/build_indexes.py --vault "$VAULT_ROOT"
   ```
