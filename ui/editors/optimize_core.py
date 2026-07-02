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
