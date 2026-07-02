# -*- coding: utf-8 -*-
"""Ядро оптимизации рёбер — без Qt (шаги 5–6, docs/PLAN_routing_patch.md).

Строит ортогональный маршрут ребра между точками прикрепления:
- ручные точки (_manual_route) сохраняются как есть;
- иначе точка прикрепления = центр стороны bbox (equipment)
  или центр узла (connector/перекрёсток — рёбра сходятся в одну точку);
- препятствия — виртуальные bbox всех остальных узлов;
- пересечения с другими рёбрами минимизируются скорингом edge_routing.

Модуль намеренно не зависит от Qt — тестируется pytest'ом напрямую
(tests/test_routing_metrics.py, секция шага 5).
"""
from __future__ import annotations

from ui.editors import edge_routing as er
from ui.editors.graph_geometry import (
    bbox_exit_side, bbox_side_midpoint, closest_bbox_side,
)

# Синхронно с BaseGraphEditor.CONNECTOR_MARKER_RADIUS
CONNECTOR_RADIUS = 8


def virtual_bbox(node: dict, conn_radius: float = CONNECTOR_RADIUS) -> list:
    """Как BaseGraphEditor._get_node_bbox: equipment → реальный bbox,
    иначе квадрат вокруг центра узла."""
    bb = node.get('bbox')
    if node.get('type') == 'equipment' and bb and len(bb) == 4:
        return bb
    cx, cy = node['centroid'][1], node['centroid'][0]
    r = conn_radius
    return [cx - r, cy - r, cx + r, cy + r]


def edge_polyline(e: dict) -> list | None:
    """Ломаная ребра [(x, y), ...] или None, если нет точек прикрепления."""
    sp, tp = e.get('source_point'), e.get('target_point')
    if not sp or not tp:
        return None
    return ([(sp[1], sp[0])]
            + [(w[1], w[0]) for w in e.get('waypoints') or []]
            + [(tp[1], tp[0])])


def _is_point_like(node: dict) -> bool:
    """Узел без собственного bbox (connector/перекрёсток) — рёбра к центру."""
    bb = node.get('bbox')
    return node.get('type') != 'equipment' or not (bb and len(bb) == 4)


def compute_optimized_route(
    nodes: dict,
    edges_data: list,
    edge_data: dict,
    conn_radius: float = CONNECTOR_RADIUS,
    existing_paths: list | None = None,
    override_src: tuple | None = None,
    override_tgt: tuple | None = None,
) -> dict:
    """Ортогональный маршрут для одного ребра (кнопка «Оптимизировать»).

    Args:
        nodes: {node_id: node} — модель редактора
        edges_data: все рёбра (для препятствий-путей и распределения)
        edge_data: оптимизируемое ребро
        conn_radius: радиус виртуального bbox коннектора
        existing_paths: переопределение путей других рёбер [(x,y),...]
            (используется в «Оптимизировать все» для аккумуляции свежих
            маршрутов); None → взять текущие пути из edges_data

    Returns:
        {'source_point': [y,x], 'target_point': [y,x],
         'waypoints': [[y,x],...], 'src_side': str, 'tgt_side': str}
    """
    src = nodes[edge_data['source']]
    tgt = nodes[edge_data['target']]
    src_bb = virtual_bbox(src, conn_radius)
    tgt_bb = virtual_bbox(tgt, conn_radius)
    scx, scy = src['centroid'][1], src['centroid'][0]
    tcx, tcy = tgt['centroid'][1], tgt['centroid'][0]

    manual = bool(edge_data.get('_manual_route'))
    man_sp = edge_data.get('source_point') if manual else None
    man_tp = edge_data.get('target_point') if manual else None

    # --- точка прикрепления и сторона выхода: source ---
    if man_sp:
        sx, sy = man_sp[1], man_sp[0]
        src_side = edge_data.get('_src_side') or closest_bbox_side(src_bb, sx, sy)
    else:
        src_side = bbox_exit_side(src_bb, scx, scy, tcx, tcy)
        if _is_point_like(src):
            sx, sy = scx, scy          # перекрёсток: ребро в центр узла
        elif override_src is not None:
            sx, sy = override_src      # слот распределения (шаг 6)
        else:
            sx, sy = bbox_side_midpoint(src_bb, src_side)

    # --- точка прикрепления и сторона выхода: target ---
    if man_tp:
        tx, ty = man_tp[1], man_tp[0]
        tgt_side = edge_data.get('_tgt_side') or closest_bbox_side(tgt_bb, tx, ty)
    else:
        tgt_side = bbox_exit_side(tgt_bb, tcx, tcy, scx, scy)
        if _is_point_like(tgt):
            tx, ty = tcx, tcy
        elif override_tgt is not None:
            tx, ty = override_tgt      # слот распределения (шаг 6)
        else:
            tx, ty = bbox_side_midpoint(tgt_bb, tgt_side)

    # --- препятствия и существующие пути ---
    obstacles = [
        virtual_bbox(n, conn_radius) for nid, n in nodes.items()
        if nid != edge_data['source'] and nid != edge_data['target']
    ]
    if existing_paths is None:
        existing_paths = [
            pl for e in edges_data if e is not edge_data
            for pl in [edge_polyline(e)] if pl
        ]

    waypoints = er.route_edge(
        (sx, sy), (tx, ty), src_side, tgt_side,
        src_bb, tgt_bb, obstacles, existing_paths,
    )

    return {
        'source_point': [sy, sx],
        'target_point': [ty, tx],
        'waypoints': waypoints,
        'src_side': src_side,
        'tgt_side': tgt_side,
    }


# ---------------------------------------------------------------------------
# Шаг 6: «Оптимизировать все» — все рёбра с учётом друг друга
# ---------------------------------------------------------------------------

def _paths_cross(pa: list, pb: list) -> bool:
    """Пересекаются ли две ортогональные ломаные (внутренними точками)."""
    for i in range(len(pa) - 1):
        for j in range(len(pb) - 1):
            if er._segments_cross(pa[i], pa[i + 1], pb[j], pb[j + 1]):
                return True
    return False


def optimize_all_routes(
    nodes: dict,
    edges_data: list,
    conn_radius: float = CONNECTOR_RADIUS,
) -> dict:
    """Кнопка «Оптимизировать все»: маршруты всех рёбер с учётом друг друга.

    Фазы:
      1. Стороны выхода всем рёбрам (ручные — по их точкам).
      2. Слоты точек прикрепления на сторонах equipment
         (distribute_connection_points; коннекторы цепляются за центр).
      3. Роутинг от коротких рёбер к длинным, свежие пути аккумулируются —
         каждое следующее ребро видит уже проложенные.
      4. Второй проход только для рёбер с оставшимися пересечениями.

    Мутирует edge-словари (вызывать под SnapshotCommand).
    Returns: {'routed': n, 'second_pass': n, 'crossings_left': n}
    """
    from ui.editors.edge_routing import distribute_connection_points

    valid = [e for e in edges_data
             if e.get('source') in nodes and e.get('target') in nodes]

    # --- Фаза 1: стороны ---
    for e in valid:
        src, tgt = nodes[e['source']], nodes[e['target']]
        sbb = virtual_bbox(src, conn_radius)
        tbb = virtual_bbox(tgt, conn_radius)
        scx, scy = src['centroid'][1], src['centroid'][0]
        tcx, tcy = tgt['centroid'][1], tgt['centroid'][0]
        if e.get('_manual_route'):
            sp, tp = e.get('source_point'), e.get('target_point')
            if sp:
                e['_src_side'] = closest_bbox_side(sbb, sp[1], sp[0])
            if tp:
                e['_tgt_side'] = closest_bbox_side(tbb, tp[1], tp[0])
        else:
            e['_src_side'] = bbox_exit_side(sbb, scx, scy, tcx, tcy)
            e['_tgt_side'] = bbox_exit_side(tbb, tcx, tcy, scx, scy)

    # --- Фаза 2: слоты на сторонах equipment ---
    slots: dict = {}   # (edge_id, 'src'|'tgt') -> (x, y)
    seen: set = set()
    for e in valid:
        for nid_key, side_key, role in (('source', '_src_side', 'src'),
                                        ('target', '_tgt_side', 'tgt')):
            nid = e[nid_key]
            node = nodes[nid]
            if _is_point_like(node):
                continue
            side = e.get(side_key)
            if not side or (nid, side) in seen:
                continue
            seen.add((nid, side))
            dist = distribute_connection_points(
                nid, side, virtual_bbox(node, conn_radius), valid, nodes)
            for ee in valid:
                eid = ee.get('id')
                if eid is None or eid not in dist:
                    continue
                if ee['source'] == nid and ee.get('_src_side') == side:
                    slots[(eid, 'src')] = dist[eid]
                if ee['target'] == nid and ee.get('_tgt_side') == side:
                    slots[(eid, 'tgt')] = dist[eid]

    # --- Фаза 3: роутинг от коротких к длинным ---
    paths: dict = {}
    order: list = []
    for idx, e in enumerate(valid):
        pl = edge_polyline(e)
        if pl:
            paths[idx] = pl
        s, t = nodes[e['source']], nodes[e['target']]
        d = (abs(s['centroid'][0] - t['centroid'][0])
             + abs(s['centroid'][1] - t['centroid'][1]))
        order.append((d, idx))
    order.sort()

    def _route_one(idx: int):
        e = valid[idx]
        eid = e.get('id')
        existing = [pl for k, pl in paths.items() if k != idx]
        r = compute_optimized_route(
            nodes, edges_data, e, conn_radius,
            existing_paths=existing,
            override_src=slots.get((eid, 'src')),
            override_tgt=slots.get((eid, 'tgt')),
        )
        e['source_point'] = r['source_point']
        e['target_point'] = r['target_point']
        e['waypoints'] = r['waypoints']
        e['_src_side'] = r['src_side']
        e['_tgt_side'] = r['tgt_side']
        paths[idx] = edge_polyline(e)

    for _, idx in order:
        _route_one(idx)

    # --- Фаза 4: второй проход по рёбрам с пересечениями ---
    def _crossing_idxs() -> set:
        res: set = set()
        idxs = sorted(paths)
        # bbox-префильтр пар: пересечение внутренних точек требует
        # перекрытия габаритов ломаных — отсекает ~всё на больших графах
        bounds = {}
        for k in idxs:
            xs = [p[0] for p in paths[k]]
            ys = [p[1] for p in paths[k]]
            bounds[k] = (min(xs), min(ys), max(xs), max(ys))
        for a_pos in range(len(idxs)):
            ia = idxs[a_pos]
            ea = valid[ia]
            ba = bounds[ia]
            for b_pos in range(a_pos + 1, len(idxs)):
                ib = idxs[b_pos]
                bb = bounds[ib]
                if ba[2] < bb[0] or bb[2] < ba[0] or \
                   ba[3] < bb[1] or bb[3] < ba[1]:
                    continue
                eb = valid[ib]
                if {ea['source'], ea['target']} & {eb['source'], eb['target']}:
                    continue
                if _paths_cross(paths[ia], paths[ib]):
                    res.add(ia)
                    res.add(ib)
        return res

    crossing = _crossing_idxs()
    for _, idx in order:
        if idx in crossing:
            _route_one(idx)

    return {
        'routed': len(order),
        'second_pass': len(crossing),
        'crossings_left': len(_crossing_idxs()),
    }
