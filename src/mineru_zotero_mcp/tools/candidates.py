"""mineru_list_visual_candidates tool.

After parse-time figure merging (figure_merger.py), each image anchor in
anchors.json is already a complete, high-quality figure. This tool is no longer
about "fragment grouping" — it's a **figure catalog** for the paper.

It gives a pure-text LLM enough context to decide, without viewing any image,
which figures are worth embedding in notes and what each figure represents:

  - location (page + bbox)  → where in the paper, to cite alongside text
  - caption                 → what the figure is (Fig 1: Architecture...)
  - image path              → vault-relative path to drop into a note as ![[...]]
  - surrounding text hint   → nearby text anchors, so the LLM can infer context

No base64 is embedded — that would dump every figure into the LLM context
unnecessarily. The LLM reads captions + locations to judge relevance; when it
wants a specific figure in a note, it uses the path directly (Obsidian resolves
the wikilink). If a human or multimodal model needs to actually view one figure,
they open that one path.
"""

from __future__ import annotations

import logging

from .._app import mcp
from .._ctx import get_vault_root
from ..store import load_manifest

logger = logging.getLogger(__name__)

# How many nearby text anchors to show as context around each figure.
_CONTEXT_NEIGHBORS = 2


@mcp.tool(
    name="mineru_list_visual_candidates",
    description=(
        "Catalog every figure in a parsed paper so a text-only LLM can judge which "
        "are worth embedding in notes — WITHOUT loading any image bytes. "
        "Returns, per figure: page, bbox (normalized), caption, the vault-relative "
        "image path (ready to drop into a note as ![[path]] or ![alt](path)), and "
        "a short text-context snippet (nearby text anchors).\n\n"
        "Figures are already merged/clean from parsing; this is a discovery/decision "
        "tool, not a capture tool. Use mineru_capture_region only when you need a "
        "rendered image of a NON-figure region (a formula block, a table's visual "
        "layout, a text passage as evidence)."
    ),
)
def list_visual_candidates_tool(
    citekey: str,
    page: int | None = None,
) -> str:
    vault = get_vault_root()
    manifest = load_manifest(vault, citekey)
    if manifest is None:
        return (
            f"No parsed data for `{citekey}`. "
            f"Run `mineru_parse_pdf(citekey=\"{citekey}\")` first."
        )

    anchors = manifest.get("anchors", [])
    image_anchors = [a for a in anchors if a.get("kind") == "image"]
    if page is not None:
        image_anchors = [a for a in image_anchors if a.get("page") == page]

    if not image_anchors:
        where = f" on page {page}" if page else ""
        return f"No figures found for `{citekey}`{where}."

    # Index text anchors by page for neighbor lookup.
    text_by_page: dict[int, list[dict]] = {}
    for a in anchors:
        if a.get("kind") == "text":
            text_by_page.setdefault(a.get("page"), []).append(a)

    lines: list[str] = [f"# {len(image_anchors)} figure(s) in `{citekey}`", ""]
    for i, img in enumerate(image_anchors, 1):
        p = img.get("page")
        bbox = img.get("bbox") or []
        bbox_str = f"[{bbox[0]:.2f},{bbox[1]:.2f}→{bbox[2]:.2f},{bbox[3]:.2f}]" if len(bbox) == 4 else "?"

        caption = img.get("caption") or "(no caption)"
        path = img.get("imagePath") or "(no image file)"
        ctx = _context_for(text_by_page.get(p, []), img)

        lines.append(f"## Figure {i} — page {p}")
        lines.append(f"- caption: {caption}")
        lines.append(f"- location: page {p}, bbox {bbox_str}")
        lines.append(f"- image: `{path}`  ← use this in a note: `!{path}` or `![[{path.split('/')[-1]}]]`")
        if ctx:
            lines.append(f"- nearby text: {ctx}")
        lines.append("")

    lines.append(
        "Tip: pick figures whose caption matches your note's point and embed the "
        "path above. No need to call any other tool to get the image — it's already "
        "rendered and on disk."
    )
    return "\n".join(lines)


def _context_for(text_anchors: list[dict], img: dict, n: int = _CONTEXT_NEIGHBORS) -> str:
    """Return up to `n` short text previews near the figure on the same page.

    Ordered by vertical proximity to the figure's bbox. Helps a text-only LLM
    infer what the figure illustrates without viewing it.
    """
    if not text_anchors or not img.get("bbox"):
        return ""
    img_cy = (img["bbox"][1] + img["bbox"][3]) / 2

    def distance(t: dict) -> float:
        tb = t.get("bbox") or [0, 0, 0, 0]
        return abs(((tb[1] + tb[3]) / 2) - img_cy)

    nearest = sorted(text_anchors, key=distance)[:n]
    snippets = []
    for t in nearest:
        preview = (t.get("textPreview") or "").strip().replace("\n", " ")
        if preview:
            snippets.append(preview[:80])
    return " … ".join(snippets) if snippets else ""
