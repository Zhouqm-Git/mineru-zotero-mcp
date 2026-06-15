"""End-to-end batch parse test (bypasses Zotero).

Tests the batch path on 3 real papers:
  1. First run: all 3 parse fresh.
  2. Second run: cache hits, all skipped (no MinerU calls).
  3. Single-failure isolation: inject a bad PDF path, others still succeed.

Zotero is bypassed by mocking resolve_identifier + get_pdf_path_for_item,
same as e2e_mineru.py.

Usage:
    MINERU_API_TOKEN=<token> python tests/e2e_batch.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

TOKEN = os.environ.get("MINERU_API_TOKEN", "")
if not TOKEN:
    print("ERROR: set MINERU_API_TOKEN in env")
    sys.exit(2)

# 3 small papers covering different layouts.
PAPERS = [
    ("/Users/zhouqm/Downloads/aigcode/paper/PE/PoSE.pdf", "pose2023"),
    ("/Users/zhouqm/Downloads/aigcode/paper/DeepSeek/mHC.pdf", "mhc2024"),
    ("/Users/zhouqm/Downloads/aigcode/paper/Megatron-LM/Megatron-LM.pdf", "megatronlm2022"),
]

VAULT = tempfile.mkdtemp(prefix="mineru_batch_")
print(f"vault: {VAULT}")
print(f"papers: {[p.split('/')[-1] for p, _ in PAPERS]}")
print()

from mineru_zotero_mcp.mineru_client import MineruClient
from mineru_zotero_mcp.parse_persist import parse_pdf

client = MineruClient(token=TOKEN)


def run_batch(papers, label, expect_cached=False):
    """Parse a list of (pdf_path, citekey); return per-paper outcomes."""
    print(f"=== {label} ===")
    results = []
    for pdf_path, citekey in papers:
        # Fresh per-paper mock: each gets its own fake item_key + pdf path.
        fake_key = citekey.upper()[:8].ljust(8, "X")
        with patch("mineru_zotero_mcp.parse_persist.resolve_identifier",
                   side_effect=lambda ik, ck: (fake_key, citekey)), \
             patch("mineru_zotero_mcp.parse_persist.get_item_identity",
                   return_value={"item_key": fake_key, "item_id": 1, "library_id": 1,
                                 "library_type": "user", "library_name": "My Library"}), \
             patch("mineru_zotero_mcp.parse_persist.get_pdf_path_for_item",
                   return_value=Path(pdf_path)):
            try:
                r = parse_pdf(
                    vault_root=VAULT, client=client,
                    item_key=fake_key, model_version="vlm",
                    enable_table=True, force=False, poll_timeout_s=600.0,
                )
                status = "cached" if r.cached else "parsed"
                print(f"  [{status}] {citekey}: {r.page_count}p, {r.table_count}t, "
                      f"{r.image_count}img, {r.char_count} chars")
                results.append((citekey, status, None))
            except Exception as e:  # noqa: BLE001
                print(f"  [FAILED] {citekey}: {e}")
                results.append((citekey, "failed", str(e)))
    print()
    return results


# ─── Run 1: fresh parse of all 3 ───────────────────────────────
r1 = run_batch(PAPERS, "RUN 1: fresh parse")
fresh = sum(1 for _, s, _ in r1 if s == "parsed")
failed = sum(1 for _, s, _ in r1 if s == "failed")
print(f"Run 1: {fresh} parsed, {failed} failed (expect 3 parsed, 0 failed)")
print()

# ─── Run 2: re-run, expect all cached (no MinerU calls) ────────
r2 = run_batch(PAPERS, "RUN 2: re-run (expect cache hits)")
cached = sum(1 for _, s, _ in r2 if s == "cached")
print(f"Run 2: {cached} cached (expect 3 cached)")
print()

# ─── Run 3: failure isolation — inject a bad PDF among 2 good ones ─
bad_papers = [
    ("/nonexistent/missing.pdf", "should_fail"),
    ("/Users/zhouqm/Downloads/aigcode/paper/PE/PoSE.pdf", "pose2023"),  # cached
]
r3 = run_batch(bad_papers, "RUN 3: failure isolation (1 bad + 1 good)")
isolated = any(s == "failed" and c == "should_fail" for c, s, _ in r3)
survived = any(s in ("cached", "parsed") and c == "pose2023" for c, s, _ in r3)
print(f"Run 3: bad paper failed = {isolated}, good paper survived = {survived}")
print()

# ─── Summary ───────────────────────────────────────────────────
print("=== SUMMARY ===")
print(f"Run 1 fresh parse:    {fresh}/3  {'PASS' if fresh == 3 else 'FAIL'}")
print(f"Run 2 cache hits:     {cached}/3 {'PASS' if cached == 3 else 'FAIL'}")
print(f"Run 3 failure isolation: bad={'PASS' if isolated else 'FAIL'}, "
      f"good={'PASS' if survived else 'FAIL'}")
print()

# Vault layout check
print("=== VAULT LAYOUT ===")
raw = Path(VAULT) / ".raw"
for d in sorted(raw.glob("*/*")) if raw.exists() else []:
    citekey = d.name.lower()
    md_candidates = list(d.glob("*.md"))
    anchors = d / "anchors.json"
    meta = d / "meta.json"
    assets = Path(VAULT) / "attachments" / "papers" / d.relative_to(raw)
    n_assets = len(list(assets.iterdir())) if assets.is_dir() else 0
    print(f"  {d.relative_to(raw)} ({citekey}): md={bool(md_candidates)}, anchors={anchors.is_file()}, "
          f"meta={meta.is_file()}, assets={n_assets}")

# Cleanup
shutil.rmtree(VAULT, ignore_errors=True)
print()
print("=== DONE ===")
