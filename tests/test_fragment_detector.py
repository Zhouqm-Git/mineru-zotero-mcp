"""Tests for fragment_detector (ported from vspdf/src/fragment-detector.ts).

Validates the three-phase Union-Find + gap-breakpoint algorithm without any
third-party deps. The key behaviors to preserve:
  - Adjacent images on the same page merge into one FragmentGroup.
  - Images in different columns never merge.
  - A single isolated image reports isFragment=False.
"""

from mineru_zotero_mcp.fragment_detector import (
    _is_adjacent,
    _effective_gap,
    _merge_bboxes,
    detect_fragments,
    find_group_for_anchor,
)
from mineru_zotero_mcp.types import Anchor, AnchorManifest


def _anchor(aid: str, page: int, bbox: tuple[float, float, float, float], kind: str = "image") -> Anchor:
    return Anchor(
        anchorId=aid, kind=kind, page=page, bbox=bbox, bboxRaw=bbox, contentIndex=0
    )


def _manifest(page: int, anchors: list[Anchor]) -> AnchorManifest:
    return AnchorManifest(
        docId="k", sourcePdf="/x.pdf", markdownPath="k.md",
        contentListPath="c.json", assetsRoot="assets", anchors=anchors,
    )


def test_is_adjacent_vertical():
    # a sits directly above b with a tiny vertical gap → adjacent.
    a = (0.1, 0.1, 0.4, 0.4)
    b = (0.1, 0.42, 0.4, 0.7)  # v_gap = 0.02, horizontal overlap → adjacent
    assert _is_adjacent(a, b)


def test_not_adjacent_far_apart():
    a = (0.1, 0.1, 0.2, 0.2)
    b = (0.8, 0.8, 0.9, 0.9)
    assert not _is_adjacent(a, b)


def test_effective_gap_uses_axis_on_overlap():
    # When boxes overlap horizontally, gap is measured along vertical axis.
    a = (0.1, 0.1, 0.5, 0.3)
    b = (0.2, 0.5, 0.6, 0.7)  # h_overlap yes → gap = v_gap = 0.2
    assert abs(_effective_gap(a, b) - 0.2) < 1e-9


def test_merge_bboxes_takes_outer():
    merged = _merge_bboxes([(0.1, 0.1, 0.4, 0.4), (0.3, 0.3, 0.6, 0.7)])
    assert merged == (0.1, 0.1, 0.6, 0.7)


def test_single_image_not_fragment():
    m = _manifest(1, [_anchor("a_image_p1_0000", 1, (0.1, 0.1, 0.4, 0.4))])
    groups = detect_fragments(m, 1)
    assert len(groups) == 1
    assert groups[0].isFragment is False
    assert groups[0].anchorIds == ["a_image_p1_0000"]


def test_two_adjacent_images_merge():
    a = _anchor("a_image_p1_0000", 1, (0.1, 0.1, 0.4, 0.4))
    b = _anchor("a_image_p1_0001", 1, (0.1, 0.42, 0.4, 0.7))
    m = _manifest(1, [a, b])
    groups = detect_fragments(m, 1)
    assert len(groups) == 1, "adjacent fragments should merge into one group"
    assert groups[0].isFragment is True
    assert set(groups[0].anchorIds) == {"a_image_p1_0000", "a_image_p1_0001"}


def test_two_far_images_do_not_merge():
    a = _anchor("a_image_p1_0000", 1, (0.05, 0.05, 0.15, 0.15))
    b = _anchor("a_image_p1_0001", 1, (0.8, 0.8, 0.9, 0.9))
    m = _manifest(1, [a, b])
    groups = detect_fragments(m, 1)
    assert len(groups) == 2
    assert all(not g.isFragment for g in groups)


def test_find_group_for_anchor():
    a = _anchor("a_image_p1_0000", 1, (0.1, 0.1, 0.4, 0.4))
    b = _anchor("a_image_p1_0001", 1, (0.1, 0.42, 0.4, 0.7))
    m = _manifest(1, [a, b])
    groups = detect_fragments(m, 1)
    g = find_group_for_anchor(groups, "a_image_p1_0001")
    assert g is not None
    assert "a_image_p1_0001" in g.anchorIds


def test_no_image_anchors_empty():
    m = _manifest(1, [_anchor("a_text_p1_0000", 1, (0.1, 0.1, 0.4, 0.4), kind="text")])
    assert detect_fragments(m, 1) == []
