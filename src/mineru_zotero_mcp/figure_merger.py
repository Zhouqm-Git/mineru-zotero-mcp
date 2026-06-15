"""Auto-merge fragmented figures during parsing.

After MinerU extraction, the markdown + paper attachment directory contain image
fragments: one real figure is often split into N adjacent image anchors with N
separate jpgs.
This module runs right after anchor generation to:

  1. Detect fragment groups (reuses fragment_detector).
  2. For each group with > 1 fragment, render ONE complete figure from the
     original PDF via PyMuPDF (high-DPI, full union bbox).
  3. Rewrite the markdown: replace the N fragmented image references with a
     single ![](attachments/papers/<citekey>/fig_<id>.png).
  4. Delete the now-orphaned fragment jpgs from the attachment directory.
  5. Update the anchors so the group becomes a single anchor pointing at the
     merged figure (the surviving anchor keeps its id; siblings are removed).

The result: by the time the user/agent reads the markdown, figures are already
whole and high-quality. No separate "capture" step is needed for figures.

PDF rendering is best-effort: if PyMuPDF fails (e.g. PDF unavailable during
parse), we leave the original fragments in place and log a warning — the
markdown stays valid, just with fragmented images.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .fragment_detector import detect_fragments, find_group_for_anchor
from .pdf_renderer import render_region
from .store import write_bytes
from .types import Anchor, AnchorManifest

logger = logging.getLogger(__name__)

# Matches a markdown image reference: ![alt](path). We rewrite these.
_IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# DPI for merged-figure re-render. 200 matches the capture default.
MERGE_DPI = 200


def merge_fragmented_figures(
    *,
    manifest: AnchorManifest,
    markdown: str,
    pdf_path: str | Path,
    assets_directory: Path,
    assets_relative: str,
) -> tuple[str, int]:
    """Merge fragmented figure anchors in-place and rewrite the markdown.

    Args:
        manifest: the AnchorManifest (mutated: fragment anchors collapsed).
        markdown: the current markdown body (table-converted, image-paths rewritten).
        pdf_path: original PDF, used to re-render complete figures.
        assets_directory: absolute path to the paper attachment dir — orphaned
            fragments deleted here.
        assets_relative: vault-relative path to assets_directory.

    Returns:
        (rewritten_markdown, merged_count) — the new markdown and how many
        fragment groups were merged into single figures.
    """
    if not Path(pdf_path).is_file():
        logger.warning(
            "PDF %s unavailable during figure merge; leaving fragments as-is.",
            pdf_path,
        )
        return markdown, 0

    # Collect all pages that have image anchors.
    image_pages = sorted({a.page for a in manifest.anchors if a.kind == "image"})
    if not image_pages:
        return markdown, 0

    merged_count = 0
    # Track which anchors to delete (fragment siblings absorbed into a survivor).
    absorbed_ids: set[str] = set()
    # Rewrite map: old asset path → new path, applied to the markdown.
    # One fragment group can reference several asset files; we map each to the
    # single merged figure path so every reference collapses to one image.
    path_rewrites: dict[str, str] = {}

    for page in image_pages:
        groups = detect_fragments(manifest, page)
        for group in groups:
            if not group.isFragment or len(group.anchorIds) <= 1:
                continue  # standalone image — nothing to merge

            survivor_id = group.anchorIds[0]
            survivor = _find_anchor(manifest, survivor_id)
            if survivor is None:
                continue

            # Render the complete figure from the original PDF.
            fig_name = f"fig_{survivor_id}.png"
            fig_path = assets_directory / fig_name
            try:
                cap = render_region(
                    pdf_path, page, group.mergedBbox, fig_path, dpi=MERGE_DPI
                )
            except Exception as e:  # noqa: BLE001 — degrade gracefully
                logger.warning(
                    "Figure merge render failed for %s p%d (%s): %s",
                    manifest.docId, page, survivor_id, e,
                )
                continue

            # Record every fragment's asset path → merged figure path.
            for aid in group.anchorIds:
                a = _find_anchor(manifest, aid)
                if a and a.imagePath:
                    # imagePath is vault-relative; resolve the asset basename.
                    old_basename = Path(a.imagePath).name
                    path_rewrites[old_basename] = fig_name
                    path_rewrites[a.imagePath] = fig_name

            # Delete orphaned fragment files (but never the freshly-rendered fig).
            for aid in group.anchorIds:
                a = _find_anchor(manifest, aid)
                if a and a.imagePath:
                    frag_file = assets_directory / Path(a.imagePath).name
                    if frag_file.name != fig_name and frag_file.is_file():
                        try:
                            frag_file.unlink()
                        except OSError as e:
                            logger.debug("Could not delete fragment %s: %s", frag_file, e)

            # Collapse the anchor group: survivor absorbs the union bbox +
            # merged image; siblings are marked for removal.
            survivor.bbox = group.mergedBbox
            survivor.imagePath = f"{assets_relative}/{fig_name}"
            # Blend captions from all fragments (drop empties / dups).
            captions = []
            for aid in group.anchorIds:
                a = _find_anchor(manifest, aid)
                if a and a.caption and a.caption not in captions:
                    captions.append(a.caption)
            if captions:
                survivor.caption = " / ".join(captions)
            for aid in group.anchorIds[1:]:
                absorbed_ids.add(aid)

            merged_count += 1
            logger.info(
                "Merged %d figure fragments on p%d → %s",
                len(group.anchorIds), page, fig_name,
            )

    if merged_count == 0:
        return markdown, 0

    # Drop absorbed anchors from the manifest.
    manifest.anchors = [a for a in manifest.anchors if a.anchorId not in absorbed_ids]

    # Rewrite markdown image references.
    rewritten = _rewrite_fragment_refs(markdown, path_rewrites, assets_relative)
    return rewritten, merged_count


def _find_anchor(manifest: AnchorManifest, anchor_id: str) -> Anchor | None:
    return next((a for a in manifest.anchors if a.anchorId == anchor_id), None)


def _rewrite_fragment_refs(
    markdown: str,
    path_rewrites: dict[str, str],
    assets_relative: str,
) -> str:
    """Collapse N fragment image refs into one merged-figure ref.

    Within a local markdown region, consecutive image refs that all map to the
    same merged figure are replaced by a single ref to that figure. Isolated
    refs are rewritten in place. This keeps the figure appearing once where it
    was fragmented, rather than N times.
    """
    if not path_rewrites:
        return markdown

    lines = markdown.split("\n")
    out: list[str] = []
    i = 0
    seen_merged: set[str] = set()  # merged figs already emitted, to dedup

    while i < len(lines):
        line = lines[i]
        m = _IMG_REF_RE.search(line)
        if not m or m.group(2).split("/")[-1] not in path_rewrites:
            out.append(line)
            i += 1
            continue

        # Found a fragment ref. Greedily collect consecutive fragment-ref lines
        # that resolve to the SAME merged figure, then emit it once.
        current_basename = m.group(2).split("/")[-1]
        target = path_rewrites.get(current_basename)
        if target is None:
            out.append(line)
            i += 1
            continue

        # Absorb following lines that also map to `target`.
        j = i
        alt_texts: list[str] = [m.group(1)]
        while j + 1 < len(lines):
            nxt = lines[j + 1].strip()
            nxt_m = _IMG_REF_RE.search(nxt)
            if not nxt_m:
                break
            nxt_base = nxt_m.group(2).split("/")[-1]
            if path_rewrites.get(nxt_base) != target:
                break
            alt_texts.append(nxt_m.group(1))
            j += 1

        # Emit the merged figure once (dedup across the whole document so a
        # figure referenced in two places doesn't duplicate).
        if target not in seen_merged:
            seen_merged.add(target)
            combined_alt = " / ".join(a for a in alt_texts if a) or "figure"
            out.append(f"![{combined_alt}]({assets_relative}/{target})")
        # else: skip — the figure was already emitted elsewhere.
        i = j + 1

    return "\n".join(out)
