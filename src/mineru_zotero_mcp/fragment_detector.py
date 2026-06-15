"""Fragment detector — detect when MinerU image fragments belong to one figure.

Direct port of vspdf/src/fragment-detector.ts (logic unchanged). The algorithm:

  Phase 0: Column detection — analyze text anchor x-distribution to find the page
           column structure (1-col / 2-col). Images in different columns are never
           merged even if spatially adjacent.
  Phase 1: Union-Find within each column — all-pairs spatial adjacency.
  Phase 2: Gap breakpoint — within each connected component, split at anomalously
           large gaps (max gap > 3× median gap).

Tunable constants are kept identical to the TS original so behavior matches.
"""

from __future__ import annotations

from .types import Anchor, AnchorManifest, Bbox, FragmentGroup

ADJACENCY_THRESHOLD = 0.06  # 6% page gap = adjacent
SPLIT_RATIO = 3  # split if max gap > 3× median gap
MIN_SPLIT_GAP = 0.03  # minimum 3% gap to consider splitting
FULL_WIDTH_THRESHOLD = 0.6  # image width > 60% page = full-width figure


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # Path compression.
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            self.parent[rx] = ry
        elif self.rank[rx] > self.rank[ry]:
            self.parent[ry] = rx
        else:
            self.parent[ry] = rx
            self.rank[rx] += 1


def _detect_column_boundaries(all_page_anchors: list[Anchor]) -> list[float]:
    """Return x-boundaries between columns ([] = single column)."""
    text_anchors = [a for a in all_page_anchors if a.kind == "text" and a.bbox]
    if len(text_anchors) < 4:
        return []

    narrow = [a for a in text_anchors if (a.bbox[2] - a.bbox[0]) < FULL_WIDTH_THRESHOLD]
    if len(narrow) < 4:
        return []

    x_centers = sorted((a.bbox[0] + a.bbox[2]) / 2 for a in narrow)

    max_gap = 0.0
    gap_idx = -1
    for i in range(1, len(x_centers)):
        gap = x_centers[i] - x_centers[i - 1]
        if gap > max_gap:
            max_gap = gap
            gap_idx = i

    if max_gap > 0.10 and gap_idx > 0:
        return [(x_centers[gap_idx - 1] + x_centers[gap_idx]) / 2]
    return []


def _partition_by_column(images: list[Anchor], boundaries: list[float]) -> list[list[Anchor]]:
    if not boundaries:
        return [images] if images else []

    slots: list[list[Anchor]] = [[] for _ in range(len(boundaries) + 1)]
    full_width: list[Anchor] = []

    for img in images:
        img_width = img.bbox[2] - img.bbox[0]
        if img_width > FULL_WIDTH_THRESHOLD:
            full_width.append(img)
            continue
        cx = (img.bbox[0] + img.bbox[2]) / 2
        slot = 0
        for b in boundaries:
            if cx > b:
                slot += 1
        slots[slot].append(img)

    result = [s for s in slots if s]
    for fw in full_width:
        result.append([fw])
    return result


def _is_adjacent(a: Bbox, b: Bbox) -> bool:
    h_gap = b[0] - a[2]
    v_gap = b[1] - a[3]
    h_overlap = a[0] < b[2] and b[0] < a[2]
    v_overlap = a[1] < b[3] and b[1] < a[3]

    if -ADJACENCY_THRESHOLD <= h_gap < ADJACENCY_THRESHOLD and v_overlap:
        return True
    if -ADJACENCY_THRESHOLD <= v_gap < ADJACENCY_THRESHOLD and h_overlap:
        return True
    if 0 <= h_gap < ADJACENCY_THRESHOLD and 0 <= v_gap < ADJACENCY_THRESHOLD:
        return True
    return False


def _effective_gap(a: Bbox, b: Bbox) -> float:
    h_gap = max(0.0, b[0] - a[2])
    v_gap = max(0.0, b[1] - a[3])
    h_overlap = a[0] < b[2] and b[0] < a[2]
    v_overlap = a[1] < b[3] and b[1] < a[3]

    if v_overlap:
        return h_gap
    if h_overlap:
        return v_gap
    return (h_gap**2 + v_gap**2) ** 0.5


def _merge_bboxes(bboxes: list[Bbox]) -> Bbox:
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _split_by_gap_breakpoints(anchors: list[Anchor]) -> list[list[Anchor]]:
    if len(anchors) <= 1:
        return [anchors]

    def sort_key(a: Anchor) -> tuple[float, float]:
        dy = a.bbox[1]
        if abs(dy) > ADJACENCY_THRESHOLD:
            return (dy, a.bbox[0])
        return (0.0, a.bbox[0])

    sorted_anchors = sorted(anchors, key=sort_key)
    if len(sorted_anchors) <= 1:
        return [sorted_anchors]

    gaps = [
        _effective_gap(sorted_anchors[i - 1].bbox, sorted_anchors[i].bbox)
        for i in range(1, len(sorted_anchors))
    ]
    if not gaps:
        return [sorted_anchors]

    med_gap = _median(gaps)
    threshold = max(med_gap * SPLIT_RATIO, MIN_SPLIT_GAP)
    max_gap = max(gaps)
    if max_gap <= threshold:
        return [sorted_anchors]

    split_idx = gaps.index(max_gap)
    left = sorted_anchors[: split_idx + 1]
    right = sorted_anchors[split_idx + 1:]
    return _split_by_gap_breakpoints(left) + _split_by_gap_breakpoints(right)


def _cluster_images(images: list[Anchor]) -> list[FragmentGroup]:
    if not images:
        return []
    if len(images) == 1:
        a = images[0]
        return [FragmentGroup(anchorIds=[a.anchorId], mergedBbox=a.bbox, isFragment=False, mineruImagePath=a.imagePath)]

    uf = _UnionFind(len(images))
    for i in range(len(images)):
        for j in range(i + 1, len(images)):
            if _is_adjacent(images[i].bbox, images[j].bbox):
                uf.union(i, j)

    component_map: dict[int, list[int]] = {}
    for i in range(len(images)):
        root = uf.find(i)
        component_map.setdefault(root, []).append(i)

    results: list[FragmentGroup] = []
    for indices in component_map.values():
        component = [images[i] for i in indices]
        if len(component) == 1:
            a = component[0]
            results.append(
                FragmentGroup(
                    anchorIds=[a.anchorId],
                    mergedBbox=a.bbox,
                    isFragment=False,
                    mineruImagePath=a.imagePath,
                )
            )
            continue

        for group in _split_by_gap_breakpoints(component):
            merged = _merge_bboxes([a.bbox for a in group])
            results.append(
                FragmentGroup(
                    anchorIds=[a.anchorId for a in group],
                    mergedBbox=merged,
                    isFragment=len(group) > 1,
                    # When fragmented we drop the per-piece MinerU image in favor of fresh capture.
                    mineruImagePath=None if len(group) > 1 else group[0].imagePath,
                )
            )
    return results


def detect_fragments(manifest: AnchorManifest, page: int) -> list[FragmentGroup]:
    """Detect figure-fragment groups on one page (1-based)."""
    page_anchors = [a for a in manifest.anchors if a.page == page]
    image_anchors = [a for a in page_anchors if a.kind == "image"]
    if not image_anchors:
        return []

    boundaries = _detect_column_boundaries(page_anchors)
    groups: list[FragmentGroup] = []
    for slot in _partition_by_column(image_anchors, boundaries):
        groups.extend(_cluster_images(slot))
    return groups


def find_group_for_anchor(
    groups: list[FragmentGroup], anchor_id: str
) -> FragmentGroup | None:
    for g in groups:
        if anchor_id in g.anchorIds:
            return g
    return None
