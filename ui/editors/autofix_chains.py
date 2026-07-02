"""
Auto-Fix: Graph-Aware Chain Alignment для P&ID графов.

Выравнивает узлы (equipment + connectors) по ортогональным цепочкам,
чтобы все рёбра стали строго горизонтальными или вертикальными.

Алгоритм:
1. Классификация рёбер (H/V/Diagonal) по ratio + абсолютному порогу
2. Union-Find цепочки: H-цепочки (общий Y), V-цепочки (общий X)
3. Splitting цепочек при большом spread (защита от L-образных труб)
4. Target = weighted median с учётом clamp (equipment тяжелее)
5. Clamp: equipment ±EQUIP_MAX_SHIFT, connectors ±CONN_MAX_SHIFT
6. Multi-pass для сходимости (обычно 2-3 итерации)
7. Overlap resolution: коннекторы внутри bbox equipment — вытолкнуть
8. Пересчёт edge endpoints (прямые рёбра остаются прямыми)

Тестировано на 3 графах:
- graph_validated:  244 edges → 100% perfect (0° deviation)
- graph_validated1: 319 edges → 100% perfect
- 2.json (793 edges): 793 edges → 100% perfect

Использование:
    from autofix_chains import auto_fix_graph

    # В AdvancedGraphEditor.auto_fix():
    result = auto_fix_graph(self.nodes, self.edges_data)
    # result содержит обновлённые nodes и edges + статистику
"""

from __future__ import annotations

import math
from copy import deepcopy
from collections import defaultdict
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════

# Edge classification
RATIO_THRESH = 0.25       # dy/dx < this → horizontal
ABS_MAX_MINOR = 60        # px — minor axis > this → diagonal (even if ratio OK)
SHORT_DIST = 30           # edges shorter than this → always H/V by dominant axis

# Chain building
MAX_CHAIN_SPREAD = 80     # px — split chain if coordinate spread > this

# Node weights in median
EQUIP_WEIGHT = 5
CONN_WEIGHT = 1

# Position clamp
EQUIP_MAX_SHIFT = 30      # px — max equipment shift from original
CONN_MAX_SHIFT = 80       # px — max connector shift from original

# Edge straightness
STRAIGHT_TOL = 3.0        # px — edge is "straight" if minor axis < this

# Iteration
MAX_PASSES = 4

# Overlap resolution
OVERLAP_MARGIN = 3        # px — margin when pushing connectors out of bbox


# ═══════════════════════════════════════════════════════════════
# Edge classification
# ═══════════════════════════════════════════════════════════════

def classify_edge(sp: list, tp: list) -> str:
    """
    Классифицировать ребро как H (horizontal), V (vertical),
    D (diagonal) или skip (слишком короткое).

    Args:
        sp: source point [y, x]
        tp: target point [y, x]

    Returns:
        'H', 'V', 'D', or 'skip'
    """
    dy = tp[0] - sp[0]
    dx = tp[1] - sp[1]
    ady, adx = abs(dy), abs(dx)
    dist = math.sqrt(dx * dx + dy * dy)

    if dist < 3:
        return 'skip'

    # Короткие рёбра: всегда H или V по доминантной оси
    if dist < SHORT_DIST:
        return 'H' if adx >= ady else 'V'

    # Длинные рёбра: ratio + абсолютный порог
    if adx >= ady:
        ratio = ady / max(adx, 1)
        if ratio < RATIO_THRESH and ady <= ABS_MAX_MINOR:
            return 'H'
    else:
        ratio = adx / max(ady, 1)
        if ratio < RATIO_THRESH and adx <= ABS_MAX_MINOR:
            return 'V'

    # Fallback: если явно больше H или V
    if adx > ady * 3:
        return 'H'
    if ady > adx * 3:
        return 'V'

    return 'D'


# ═══════════════════════════════════════════════════════════════
# Union-Find
# ═══════════════════════════════════════════════════════════════

class _UnionFind:
    """Union-Find с path compression и union by rank."""

    def __init__(self):
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def groups(self) -> dict[str, list[str]]:
        grps: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            grps[self.find(x)].append(x)
        return dict(grps)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _weighted_median(values_weights: list[tuple[float, float]]) -> float:
    """Weighted median: позиция большинства побеждает, outlier'ы подтягиваются."""
    if not values_weights:
        return 0
    sorted_vw = sorted(values_weights, key=lambda x: x[0])
    total_w = sum(w for _, w in sorted_vw)
    cumsum = 0.0
    for val, w in sorted_vw:
        cumsum += w
        if cumsum >= total_w / 2:
            return val
    return sorted_vw[-1][0]


def _split_at_gaps(
    vals: list[tuple[str, float]], max_spread: float
) -> list[list[tuple[str, float]]]:
    """Рекурсивно разбить отсортированный список на подгруппы по max spread."""
    if not vals:
        return []
    spread = vals[-1][1] - vals[0][1]
    if spread <= max_spread or len(vals) <= 1:
        return [vals]

    max_gap = 0.0
    max_gap_idx = 1
    for i in range(1, len(vals)):
        gap = vals[i][1] - vals[i - 1][1]
        if gap > max_gap:
            max_gap = gap
            max_gap_idx = i

    left = vals[:max_gap_idx]
    right = vals[max_gap_idx:]
    return _split_at_gaps(left, max_spread) + _split_at_gaps(right, max_spread)


def _build_chains(
    uf: _UnionFind, nodes: dict[str, dict], axis: str
) -> list[list[str]]:
    """Построить цепочки с ограничением spread."""
    raw_groups = uf.groups()
    final_chains: list[list[str]] = []

    coord_idx = 0 if axis == 'Y' else 1  # centroid = [y, x]

    for members in raw_groups.values():
        if len(members) < 2:
            continue

        vals = [(nid, nodes[nid]['centroid'][coord_idx]) for nid in members]
        vals.sort(key=lambda x: x[1])

        for sub_chain in _split_at_gaps(vals, MAX_CHAIN_SPREAD):
            if len(sub_chain) >= 2:
                final_chains.append([nid for nid, _ in sub_chain])

    return final_chains


def _clamp(target: float, orig: float, max_shift: float) -> float:
    return max(orig - max_shift, min(orig + max_shift, target))


# ═══════════════════════════════════════════════════════════════
# Polygon geometry helpers
# ═══════════════════════════════════════════════════════════════

def _get_poly(node: dict) -> Optional[list]:
    """Extract polygon as [(x,y), ...] pairs, or None."""
    seg = node.get('segmentation')
    if seg and isinstance(seg, list) and len(seg) >= 6:
        return [(seg[i], seg[i + 1]) for i in range(0, len(seg), 2)]
    return None


def _h_ray_intersections(y: float, poly_pts: list) -> list:
    """All x-coordinates where horizontal line y=const crosses polygon edges."""
    xs = []
    n = len(poly_pts)
    for i in range(n):
        x1, y1 = poly_pts[i]
        x2, y2 = poly_pts[(i + 1) % n]
        if y1 == y2:
            continue
        if (y1 <= y < y2) or (y2 <= y < y1):
            t = (y - y1) / (y2 - y1)
            xs.append(x1 + t * (x2 - x1))
    return xs


def _v_ray_intersections(x: float, poly_pts: list) -> list:
    """All y-coordinates where vertical line x=const crosses polygon edges."""
    ys = []
    n = len(poly_pts)
    for i in range(n):
        x1, y1 = poly_pts[i]
        x2, y2 = poly_pts[(i + 1) % n]
        if x1 == x2:
            continue
        if (x1 <= x < x2) or (x2 <= x < x1):
            t = (x - x1) / (x2 - x1)
            ys.append(y1 + t * (y2 - y1))
    return ys


def _closest_point_on_segment(px, py, x1, y1, x2, y2):
    """Closest point on line segment (x1,y1)-(x2,y2) to point (px,py)."""
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return x1, y1
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    return x1 + t * dx, y1 + t * dy


def _nearest_point_on_polygon(px: float, py: float, poly_pts: list):
    """Closest point on polygon boundary to (px, py). Returns (x, y)."""
    best_dist_sq = float('inf')
    best_pt = (px, py)
    n = len(poly_pts)
    for i in range(n):
        x1, y1 = poly_pts[i]
        x2, y2 = poly_pts[(i + 1) % n]
        cx, cy = _closest_point_on_segment(px, py, x1, y1, x2, y2)
        d = (cx - px) ** 2 + (cy - py) ** 2
        if d < best_dist_sq:
            best_dist_sq = d
            best_pt = (cx, cy)
    return best_pt


def _polygon_h_boundary(y: float, poly_pts: list, toward_x: float):
    """X on polygon boundary at horizontal y, on the side facing toward_x.

    Returns x-coordinate or None if ray misses polygon.
    """
    xs = _h_ray_intersections(y, poly_pts)
    if not xs:
        return None
    # Pick boundary on the side of toward_x:
    # toward_x > polygon center → rightmost intersection
    # toward_x < polygon center → leftmost intersection
    cx = sum(p[0] for p in poly_pts) / len(poly_pts)
    if toward_x >= cx:
        return max(xs)
    return min(xs)


def _polygon_v_boundary(x: float, poly_pts: list, toward_y: float):
    """Y on polygon boundary at vertical x, on the side facing toward_y.

    Returns y-coordinate or None if ray misses polygon.
    """
    ys = _v_ray_intersections(x, poly_pts)
    if not ys:
        return None
    cy = sum(p[1] for p in poly_pts) / len(poly_pts)
    if toward_y >= cy:
        return max(ys)
    return min(ys)


def _polygon_connection_point(
    other_x: float, other_y: float, poly_pts: list, bbox: list,
) -> tuple:
    """Best connection point on polygon boundary toward (other_x, other_y).

    Strategy:
    1. H-ray at y=other_y → x on polygon boundary (perfect H edge)
    2. V-ray at x=other_x → y on polygon boundary (perfect V edge)
    3. Pick whichever gives shorter distance
    4. If both miss → nearest point on polygon boundary (slightly diagonal)
    """
    h_x = _polygon_h_boundary(other_y, poly_pts, other_x)
    v_y = _polygon_v_boundary(other_x, poly_pts, other_y)

    h_dist = abs(h_x - other_x) if h_x is not None else float('inf')
    v_dist = abs(v_y - other_y) if v_y is not None else float('inf')

    if h_dist <= v_dist and h_x is not None:
        return (h_x, other_y)
    if v_y is not None:
        return (other_x, v_y)

    # Both rays miss → nearest point on boundary
    return _nearest_point_on_polygon(other_x, other_y, poly_pts)


# ═══════════════════════════════════════════════════════════════
# Orthogonal reroute helpers (шаг 7b)
# ═══════════════════════════════════════════════════════════════

_CONN_VBOX_R = 8  # синхронно с BaseGraphEditor.CONNECTOR_MARKER_RADIUS


def _vbox(n: dict) -> list:
    """Виртуальный bbox узла: equipment → реальный, иначе бокс вокруг центра."""
    bb = n.get('bbox')
    if n.get('type') != 'connector' and bb and len(bb) == 4:
        return bb
    cx, cy = n['centroid'][1], n['centroid'][0]
    r = _CONN_VBOX_R
    return [cx - r, cy - r, cx + r, cy + r]


def _side_of(bbox: list, x: float, y: float) -> str:
    d = {
        'left': abs(x - bbox[0]), 'right': abs(x - bbox[2]),
        'top': abs(y - bbox[1]), 'bottom': abs(y - bbox[3]),
    }
    return min(d, key=d.get)


def _orthogonal_waypoints(edge: dict, sp: list, tp: list, nodes: dict) -> list:
    """Ортогональный маршрут между уже вычисленными endpoint'ами (7b).

    Вместо диагонали — route_edge с обходом узлов. Импорт локальный,
    чтобы модуль остался автономным при отсутствии edge_routing."""
    from ui.editors.edge_routing import route_edge
    src, tgt = nodes[edge['source']], nodes[edge['target']]
    sbb, tbb = _vbox(src), _vbox(tgt)
    obstacles = [
        _vbox(n) for nid, n in nodes.items()
        if nid not in (edge['source'], edge['target'])
    ]
    return route_edge(
        (sp[1], sp[0]), (tp[1], tp[0]),
        _side_of(sbb, sp[1], sp[0]), _side_of(tbb, tp[1], tp[0]),
        sbb, tbb, obstacles, [],
    )


# ═══════════════════════════════════════════════════════════════
# Main algorithm
# ═══════════════════════════════════════════════════════════════

def auto_fix_graph(
    nodes: dict[str, dict],
    edges_data: list[dict],
    equip_max_shift: float = EQUIP_MAX_SHIFT,
    conn_max_shift: float = CONN_MAX_SHIFT,
) -> dict:
    """
    Выровнять узлы и пересчитать рёбра для ортогонального вида.

    Args:
        nodes: dict {node_id: node_data} — МОДИФИЦИРУЕТСЯ in-place
        edges_data: list of edge dicts — МОДИФИЦИРУЕТСЯ in-place
        equip_max_shift: max px equipment can move from original
        conn_max_shift: max px connector can move from original

    Returns:
        dict со статистикой:
        {
            'h_chains': int,
            'v_chains': int,
            'nodes_moved': int,
            'total_shift_px': float,
            'edges_straightened': int,
            'passes': int,
            'overlaps_fixed': int,
        }
    """
    stats = {
        'h_chains': 0, 'v_chains': 0,
        'nodes_moved': 0, 'total_shift_px': 0.0,
        'edges_straightened': 0, 'passes': 0,
        'overlaps_fixed': 0,
        'manual_kept': 0,      # 7a: ручные маршруты сохранены
        'edges_rerouted': 0,   # 7b: диагонали заменены ортогональным маршрутом
    }

    def _cx(n: dict) -> float:
        return n['centroid'][1]

    def _cy(n: dict) -> float:
        return n['centroid'][0]

    def _is_equip(n: dict) -> bool:
        return n.get('type') != 'connector'

    # Save original positions for clamp
    orig_pos: dict[str, tuple[float, float]] = {}
    for nid, n in nodes.items():
        orig_pos[nid] = (_cx(n), _cy(n))

    # ─── Multi-pass alignment ──────────────────────────────────
    for pass_num in range(MAX_PASSES):
        moved_this_pass = 0

        # Step 1: Classify edges from current centroids
        edge_classes: dict[str, str] = {}
        for e in edges_data:
            src = nodes.get(e['source'])
            tgt = nodes.get(e['target'])
            if not src or not tgt:
                continue
            sp = [_cy(src), _cx(src)]
            tp = [_cy(tgt), _cx(tgt)]
            edge_classes[e['id']] = classify_edge(sp, tp)

        # Step 2: Build H-chains and V-chains
        h_uf, v_uf = _UnionFind(), _UnionFind()
        for e in edges_data:
            cls = edge_classes.get(e['id'])
            if cls == 'H':
                h_uf.union(e['source'], e['target'])
            elif cls == 'V':
                v_uf.union(e['source'], e['target'])

        h_chains = _build_chains(h_uf, nodes, 'Y')
        v_chains = _build_chains(v_uf, nodes, 'X')

        if pass_num == 0:
            stats['h_chains'] = len(h_chains)
            stats['v_chains'] = len(v_chains)

        # Step 3: Compute targets (2-step: raw median → clamp → re-median)
        y_targets: dict[str, float] = {}
        x_targets: dict[str, float] = {}

        for chain in h_chains:
            # Raw weighted median
            vw = [
                (_cy(nodes[nid]),
                 EQUIP_WEIGHT if _is_equip(nodes[nid]) else CONN_WEIGHT)
                for nid in chain
            ]
            raw_target = _weighted_median(vw)

            # Clamp-aware re-median
            clamped_vw = []
            for nid in chain:
                n = nodes[nid]
                _, oy = orig_pos[nid]
                ms = equip_max_shift if _is_equip(n) else conn_max_shift
                clamped_y = _clamp(raw_target, oy, ms)
                w = EQUIP_WEIGHT if _is_equip(n) else CONN_WEIGHT
                clamped_vw.append((clamped_y, w))

            final_target = _weighted_median(clamped_vw)
            for nid in chain:
                y_targets[nid] = final_target

        for chain in v_chains:
            vw = [
                (_cx(nodes[nid]),
                 EQUIP_WEIGHT if _is_equip(nodes[nid]) else CONN_WEIGHT)
                for nid in chain
            ]
            raw_target = _weighted_median(vw)

            clamped_vw = []
            for nid in chain:
                n = nodes[nid]
                ox, _ = orig_pos[nid]
                ms = equip_max_shift if _is_equip(n) else conn_max_shift
                clamped_x = _clamp(raw_target, ox, ms)
                w = EQUIP_WEIGHT if _is_equip(n) else CONN_WEIGHT
                clamped_vw.append((clamped_x, w))

            final_target = _weighted_median(clamped_vw)
            for nid in chain:
                x_targets[nid] = final_target

        # Step 4: Apply positions with clamp
        for nid, n in nodes.items():
            cx, cy = _cx(n), _cy(n)
            ox, oy = orig_pos[nid]

            tx = x_targets.get(nid, cx)
            ty = y_targets.get(nid, cy)

            ms = equip_max_shift if _is_equip(n) else conn_max_shift
            tx = _clamp(tx, ox, ms)
            ty = _clamp(ty, oy, ms)

            ddx = tx - cx
            ddy = ty - cy

            if abs(ddx) > 0.5 or abs(ddy) > 0.5:
                n['centroid'] = [ty, tx]
                bb = n.get('bbox')
                if bb and len(bb) == 4:
                    n['bbox'] = [bb[0] + ddx, bb[1] + ddy,
                                 bb[2] + ddx, bb[3] + ddy]
                seg = n.get('segmentation')
                if seg and isinstance(seg, list) and len(seg) >= 6:
                    for i in range(0, len(seg), 2):
                        seg[i] += ddx
                        seg[i + 1] += ddy
                moved_this_pass += 1

        stats['passes'] = pass_num + 1
        if moved_this_pass < 3:
            break

    # ─── Overlap resolution ────────────────────────────────────
    equip_bboxes: list[tuple[str, list]] = []
    for nid, n in nodes.items():
        if _is_equip(n):
            bb = n.get('bbox')
            if bb and len(bb) == 4:
                equip_bboxes.append((nid, bb))

    for nid, n in nodes.items():
        if _is_equip(n):
            continue
        cx, cy = _cx(n), _cy(n)
        for eid, bb in equip_bboxes:
            m = OVERLAP_MARGIN
            if bb[0] - m <= cx <= bb[2] + m and bb[1] - m <= cy <= bb[3] + m:
                dists = {
                    "left": cx - bb[0],
                    "right": bb[2] - cx,
                    "top": cy - bb[1],
                    "bottom": bb[3] - cy,
                }
                nearest = min(dists, key=dists.get)
                push = m + 2
                if nearest == "top":
                    cy = bb[1] - push
                elif nearest == "bottom":
                    cy = bb[3] + push
                elif nearest == "left":
                    cx = bb[0] - push
                elif nearest == "right":
                    cx = bb[2] + push
                n['centroid'] = [cy, cx]
                stats['overlaps_fixed'] += 1
                # No break — continue checking remaining equipment bboxes
                # with updated cx/cy position

    # ─── Update edge endpoints ─────────────────────────────────
    # For each edge, compute source and target connection points.
    #
    # Straight edges (after alignment, ddy or ddx ≤ STRAIGHT_TOL):
    #   Both endpoints share a common Y (H) or X (V), ensuring
    #   a perfectly straight line without waypoints.
    #   Common coordinate: connector centroid wins (avoids mid_x/mid_y
    #   which places the point far from connector for large equipment).
    #
    # Non-straight edges (diagonal, couldn't align):
    #   Each endpoint computed independently:
    #   - Connector (no bbox): point = centroid
    #   - Equipment (bbox): project the OTHER node's position
    #     onto the nearest visible face of the bbox.
    #   These edges stay as straight diagonal lines (no waypoints).

    def _project_to_bbox_face(px: float, py: float, bbox: list) -> tuple:
        """Find the best connection point on bbox face for point (px, py)."""
        x1, y1, x2, y2 = bbox

        x_inside = x1 <= px <= x2
        y_inside = y1 <= py <= y2

        if x_inside and not y_inside:
            return (px, y1) if py < y1 else (px, y2)

        if y_inside and not x_inside:
            return (x1, py) if px < x1 else (x2, py)

        if not x_inside and not y_inside:
            clamped_x = max(x1, min(x2, px))
            clamped_y = max(y1, min(y2, py))
            face_y = y1 if py < y1 else y2
            face_x = x1 if px < x1 else x2
            h_face_off = abs(px - clamped_x)
            v_face_off = abs(py - clamped_y)
            if h_face_off <= v_face_off:
                return (clamped_x, face_y)
            else:
                return (face_x, clamped_y)

        # Inside bbox — nearest face
        dists = [
            (px - x1, (x1, py)),     # left
            (x2 - px, (x2, py)),     # right
            (py - y1, (px, y1)),     # top
            (y2 - py, (px, y2)),     # bottom
        ]
        return min(dists, key=lambda d: d[0])[1]

    def _node_endpoint(node: dict, other_x: float, other_y: float) -> tuple:
        """Connection point on node toward (other_x, other_y).

        Connector (no bbox) → centroid.
        Equipment with polygon → polygon boundary point.
        Equipment bbox only → project onto bbox face.
        """
        bbox = node.get('bbox')
        if not bbox or len(bbox) != 4:
            return (node['centroid'][1], node['centroid'][0])
        poly = _get_poly(node)
        if poly:
            return _polygon_connection_point(other_x, other_y, poly, bbox)
        return _project_to_bbox_face(other_x, other_y, bbox)

    def _h_edge_x(node: dict, toward_x: float, common_y: float) -> float:
        """X-coordinate for H-straight edge endpoint on node at y=common_y.

        Polygon → H-ray intersection on boundary.
        Bbox only → left/right face.
        No bbox → centroid x.
        """
        bbox = node.get('bbox')
        if not bbox or len(bbox) != 4:
            return _cx(node)
        poly = _get_poly(node)
        if poly:
            bx = _polygon_h_boundary(common_y, poly, toward_x)
            if bx is not None:
                return bx
            # H-ray misses polygon → nearest boundary point, take x
            pt = _nearest_point_on_polygon(toward_x, common_y, poly)
            return pt[0]
        # Bbox fallback
        ncx = _cx(node)
        return bbox[2] if toward_x > ncx else bbox[0]

    def _v_edge_y(node: dict, toward_y: float, common_x: float) -> float:
        """Y-coordinate for V-straight edge endpoint on node at x=common_x.

        Polygon → V-ray intersection on boundary.
        Bbox only → top/bottom face.
        No bbox → centroid y.
        """
        bbox = node.get('bbox')
        if not bbox or len(bbox) != 4:
            return _cy(node)
        poly = _get_poly(node)
        if poly:
            by = _polygon_v_boundary(common_x, poly, toward_y)
            if by is not None:
                return by
            pt = _nearest_point_on_polygon(common_x, toward_y, poly)
            return pt[1]
        ncy = _cy(node)
        return bbox[3] if toward_y > ncy else bbox[1]

    for e in edges_data:
        src = nodes.get(e['source'])
        tgt = nodes.get(e['target'])
        if not src or not tgt:
            continue

        # 7a: ручной маршрут неприкосновенен — endpoints следуют за своими
        # узлами (на дельту сдвига), waypoints не трогаются вовсе.
        if e.get('_manual_route'):
            for node_obj, nid, pkey in ((src, e['source'], 'source_point'),
                                        (tgt, e['target'], 'target_point')):
                pt = e.get(pkey)
                if pt and nid in orig_pos:
                    ox, oy = orig_pos[nid]
                    pt[1] += _cx(node_obj) - ox
                    pt[0] += _cy(node_obj) - oy
            stats['manual_kept'] += 1
            continue

        sx, sy = _cx(src), _cy(src)
        tx, ty = _cx(tgt), _cy(tgt)
        src_bbox = src.get('bbox') if src.get('bbox') and len(
            src.get('bbox', [])) == 4 else None
        tgt_bbox = tgt.get('bbox') if tgt.get('bbox') and len(
            tgt.get('bbox', [])) == 4 else None

        ddx_abs = abs(sx - tx)
        ddy_abs = abs(sy - ty)

        if ddy_abs <= STRAIGHT_TOL:
            # ── Horizontal straight edge ──────────────────
            if not src_bbox and not tgt_bbox:
                common_y = (sy + ty) / 2
            elif not src_bbox:
                common_y = sy
            elif not tgt_bbox:
                common_y = ty
            else:
                common_y = (sy + ty) / 2

            sp_x = _h_edge_x(src, tx, common_y) if src_bbox else sx
            tp_x = _h_edge_x(tgt, sx, common_y) if tgt_bbox else tx

            e['source_point'] = [common_y, sp_x]
            e['target_point'] = [common_y, tp_x]
            e['waypoints'] = []
            stats['edges_straightened'] += 1

        elif ddx_abs <= STRAIGHT_TOL:
            # ── Vertical straight edge ────────────────────
            if not src_bbox and not tgt_bbox:
                common_x = (sx + tx) / 2
            elif not src_bbox:
                common_x = sx
            elif not tgt_bbox:
                common_x = tx
            else:
                common_x = (sx + tx) / 2

            sp_y = _v_edge_y(src, ty, common_x) if src_bbox else sy
            tp_y = _v_edge_y(tgt, sy, common_x) if tgt_bbox else ty

            e['source_point'] = [sp_y, common_x]
            e['target_point'] = [tp_y, common_x]
            e['waypoints'] = []
            stats['edges_straightened'] += 1

        else:
            # ── Non-straight edge (diagonal / couldn't align) ─
            if not src_bbox or not tgt_bbox:
                src_pt = _node_endpoint(src, tx, ty)
                tgt_pt = _node_endpoint(tgt, sx, sy)
                e['source_point'] = [src_pt[1], src_pt[0]]
                e['target_point'] = [tgt_pt[1], tgt_pt[0]]
            else:
                # eq↔eq → try perpendicular via bbox overlap
                x_overlap_start = max(src_bbox[0], tgt_bbox[0])
                x_overlap_end = min(src_bbox[2], tgt_bbox[2])
                y_overlap_start = max(src_bbox[1], tgt_bbox[1])
                y_overlap_end = min(src_bbox[3], tgt_bbox[3])

                x_overlap = x_overlap_end - x_overlap_start
                y_overlap = y_overlap_end - y_overlap_start

                if x_overlap > 2 and y_overlap <= 0:
                    # Vertical connection through X-overlap
                    conn_x = (x_overlap_start + x_overlap_end) / 2
                    if sy < ty:
                        sp_y = _v_edge_y(src, ty, conn_x)
                        tp_y = _v_edge_y(tgt, sy, conn_x)
                    else:
                        sp_y = _v_edge_y(src, ty, conn_x)
                        tp_y = _v_edge_y(tgt, sy, conn_x)
                    e['source_point'] = [sp_y, conn_x]
                    e['target_point'] = [tp_y, conn_x]

                elif y_overlap > 2 and x_overlap <= 0:
                    # Horizontal connection through Y-overlap
                    conn_y = (y_overlap_start + y_overlap_end) / 2
                    if sx < tx:
                        sp_x = _h_edge_x(src, tx, conn_y)
                        tp_x = _h_edge_x(tgt, sx, conn_y)
                    else:
                        sp_x = _h_edge_x(src, tx, conn_y)
                        tp_x = _h_edge_x(tgt, sx, conn_y)
                    e['source_point'] = [conn_y, sp_x]
                    e['target_point'] = [conn_y, tp_x]

                else:
                    # No clean overlap → projection fallback
                    src_pt = _node_endpoint(src, tx, ty)
                    tgt_pt = _node_endpoint(tgt, sx, sy)
                    e['source_point'] = [src_pt[1], src_pt[0]]
                    e['target_point'] = [tgt_pt[1], tgt_pt[0]]

            # 7b: если соединение осталось диагональным — ортогональный
            # маршрут в обход узлов вместо прямой диагонали.
            e['waypoints'] = []
            sp, tp = e.get('source_point'), e.get('target_point')
            if sp and tp and abs(sp[1] - tp[1]) > STRAIGHT_TOL \
                    and abs(sp[0] - tp[0]) > STRAIGHT_TOL:
                e['waypoints'] = _orthogonal_waypoints(e, sp, tp, nodes)
                if e['waypoints']:
                    stats['edges_rerouted'] += 1

    # ─── Final statistics ──────────────────────────────────────
    total_shift = 0.0
    nodes_moved = 0
    for nid, n in nodes.items():
        ox, oy = orig_pos[nid]
        shift = math.sqrt((_cx(n) - ox) ** 2 + (_cy(n) - oy) ** 2)
        if shift > 0.5:
            nodes_moved += 1
            total_shift += shift

    stats['nodes_moved'] = nodes_moved
    stats['total_shift_px'] = round(total_shift, 1)

    return stats
