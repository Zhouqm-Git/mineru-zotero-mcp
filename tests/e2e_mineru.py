"""End-to-end MinerU integration test (bypasses Zotero).

Tests the real MinerU cloud API + parse_persist pipeline on a local PDF:
  submit → poll → download zip → table normalize → figure merge → anchors → vault files

Zotero is bypassed by mocking resolve_identifier + get_pdf_path_for_item so we
isolate "does MinerU work?" from "is Zotero set up?".

Usage:
    MINERU_API_TOKEN=<token> python tests/e2e_mineru.py <pdf-path> <citekey> <vault-root>
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Make sure we import the installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PDF = sys.argv[1] if len(sys.argv) > 1 else "/Users/zhouqm/Downloads/aigcode/paper/PE/PoSE.pdf"
CITEKEY = sys.argv[2] if len(sys.argv) > 2 else "pose2023"
# If the caller passes an explicit vault path (argv[3]) we keep it; otherwise we
# use a temp dir and clean it up at the end so tests never litter the filesystem.
_EXPLICIT_VAULT = len(sys.argv) > 3
VAULT = sys.argv[3] if _EXPLICIT_VAULT else tempfile.mkdtemp(prefix="mineru_e2e_")

TOKEN = os.environ.get("MINERU_API_TOKEN", "")
if not TOKEN:
    print("ERROR: set MINERU_API_TOKEN in env")
    sys.exit(2)

print(f"PDF:      {PDF}")
print(f"citekey:  {CITEKEY}")
print(f"vault:    {VAULT}")
print(f"token:    {TOKEN[:20]}...")
print()

# Fresh vault for a clean test.
raw = Path(VAULT) / "raw" / CITEKEY
if raw.exists():
    shutil.rmtree(raw)
Path(VAULT).mkdir(parents=True, exist_ok=True)

from mineru_zotero_mcp.mineru_client import MineruClient
from mineru_zotero_mcp.parse_persist import parse_pdf

ITEM_KEY_FAKE = "TESTTEST"

client = MineruClient(token=TOKEN)

# Bypass Zotero: pretend resolve_identifier + get_pdf_path_for_item succeed.
with patch("mineru_zotero_mcp.parse_persist.resolve_identifier", side_effect=lambda ik, ck: (ITEM_KEY_FAKE, CITEKEY)), \
     patch("mineru_zotero_mcp.parse_persist.get_pdf_path_for_item", return_value=Path(PDF)):

    print("→ Calling parse_pdf (submitting to MinerU, polling, persisting)...")
    result = parse_pdf(
        vault_root=VAULT,
        client=client,
        item_key=ITEM_KEY_FAKE,
        model_version="vlm",
        enable_table=True,
        force=True,
        poll_timeout_s=600.0,
    )

print()
print("=== RESULT ===")
print(f"  citekey:       {result.citekey}")
print(f"  cached:        {result.cached}")
print(f"  page_count:    {result.page_count}")
print(f"  image_count:   {result.image_count}")
print(f"  table_count:   {result.table_count}")
print(f"  char_count:    {result.char_count}")
print(f"  markdown_path: {result.markdown_path}")
print(f"  anchors_path:  {result.anchors_path}")
print(f"  assets_dir:    {result.assets_dir}")
print()

# Inspect the produced files.
md_p = Path(VAULT) / result.markdown_path
anchors_p = Path(VAULT) / result.anchors_path
assets_d = Path(VAULT) / result.assets_dir

print("=== FILES ===")
print(f"  md exists:      {md_p.is_file()} ({md_p.stat().st_size if md_p.is_file() else 0} bytes)")
print(f"  anchors exist:  {anchors_p.is_file()} ({anchors_p.stat().st_size if anchors_p.is_file() else 0} bytes)")
if assets_d.is_dir():
    files = sorted(assets_d.iterdir())
    pngs = [f for f in files if f.suffix == ".png"]
    jpgs = [f for f in files if f.suffix in (".jpg", ".jpeg")]
    print(f"  assets:         {len(files)} files ({len(pngs)} png [merged figs], {len(jpgs)} jpg [originals])")
    for f in pngs[:5]:
        print(f"    - {f.name}  ({f.stat().st_size} bytes)")
else:
    print(f"  assets dir missing: {assets_d}")

# Inspect anchors.
import json
if anchors_p.is_file():
    manifest = json.loads(anchors_p.read_text())
    anchors = manifest.get("anchors", [])
    kinds = {}
    for a in anchors:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print()
    print("=== ANCHORS ===")
    print(f"  total: {len(anchors)}")
    for k, v in sorted(kinds.items()):
        print(f"    {k}: {v}")
    # Show any table anchors that got a markdown table attached.
    tables_with_md = [a for a in anchors if a.get("kind") == "table" and a.get("markdownTable")]
    print(f"  tables with GFM markdown: {len(tables_with_md)}")
    # Show merged figure anchors (image anchors whose imagePath starts with fig_).
    merged_figs = [a for a in anchors if a.get("kind") == "image" and a.get("imagePath", "").startswith("assets/fig_")]
    print(f"  merged figures (fig_*.png): {len(merged_figs)}")

# Show first ~40 lines of markdown so we can eyeball table-as-markdown + page markers.
if md_p.is_file():
    md = md_p.read_text()
    print()
    print("=== MARKDOWN HEAD (first 60 lines) ===")
    for line in md.splitlines()[:60]:
        print(f"  {line}")
    # Check key invariants.
    print()
    print("=== INVARIANTS ===")
    print(f"  has <!-- Page 1 --> marker: {'<!-- Page 1 -->' in md}")
    page_markers = md.count("<!-- Page ")
    print(f"  page markers count:         {page_markers}")
    has_gfm_table = "| --- |" in md or "| --- |" in md.replace("- -", "---")
    print(f"  has GFM table separator:    {'| ---' in md or '|---|' in md}")
    has_html_table = "<table" in md.lower()
    print(f"  has leftover HTML <table>:  {has_html_table}")

print()
print("=== DONE ===")

# Clean up the temp vault unless the caller passed an explicit path.
if not _EXPLICIT_VAULT:
    shutil.rmtree(VAULT, ignore_errors=True)
    print(f"(cleaned up temp vault: {VAULT})")
