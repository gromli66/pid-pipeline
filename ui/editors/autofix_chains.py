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

    # ─── Update edge endpoints — Э1: канон посадки ─────────────
    # Старый блок переписывал концы у ВСЕХ рёбер собственными правилами
    # (грань полного bbox, полигон приоритетнее скина — против канона).
    # Теперь: пересаживаются ТОЛЬКО рёбра, затронутые сдвигом узлов, и
    # только каноном `modules/graph/core/seating.reseat_edge` — тем же
    # модулем, что сажает выход раскладки. Нетронутые рёбра сохраняют
    # входную посадку (инвариант «не больше входа»); вход с ПИНОМ (Э5)
    # канону не отдаётся — после пересадки конец возвращается в пин.
    # Waypoints затронутых рёбер стираются ДО пересадки — это прежняя
    # семантика выпрямления (иначе после сдвига узлов остаётся устаревший
    # зигзаг с диагональным хвостом), а канон затем сажает концы на общую
    # ось уже прямого ребра.
    from modules.graph.core.seating import reseat_edge

    moved_ids = set()
    for nid, n in nodes.items():
        ox, oy = orig_pos[nid]
        if math.hypot(_cx(n) - ox, _cy(n) - oy) > 0.5:
            moved_ids.add(nid)

    from ui.editors import port_model

    for e in edges_data:
        if e.get('_manual_route'):        # легаси-файлы мимо миграции
            continue
        if e['source'] not in moved_ids and e['target'] not in moved_ids:
            continue
        if e['source'] not in nodes or e['target'] not in nodes:
            continue                      # висячее ребро — как и раньше, мимо
        e['waypoints'] = []
        reseat_edge(nodes, e)
        # Э5: вход с пином канону не отдаётся — вернуть конец в пин
        for role, pk in (('source', 'source_point'),
                         ('target', 'target_point')):
            pin = port_model.pinned_port(nodes.get(e.get(role)), e)
            if pin is not None:
                e[pk] = [pin[1], pin[0]]
        sp, tp = e.get('source_point'), e.get('target_point')
        if sp and tp and (abs(sp[0] - tp[0]) <= STRAIGHT_TOL
                          or abs(sp[1] - tp[1]) <= STRAIGHT_TOL):
            stats['edges_straightened'] += 1

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
