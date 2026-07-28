# -*- coding: utf-8 -*-
"""_overlaps.py — строгий shapely-арбитр наложений боксов.

Источник: стенд `_scratch/layout_align/verify_overlaps.py` (строки 71-145).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.
Из отчёта стенда взято ровно то, что читает гейт: число строгих пар
block-block. Остальные поля отчёта (сверка с харнесом, зазоры, худшие пары)
— диагностика стенда, в прод не идут.
"""
from __future__ import annotations

from shapely import STRtree
from shapely.geometry import Point, box as shp_box

def collect_items(graph):
    """[(nid, is_conn, rect|None, geom)] — по всем узлам графа.

    rect = (x1, y1, x2, y2) из bbox, если он невырожден; иначе None и узел
    представлен точкой центроида (коннекторы, узлы без bbox).
    """
    items = []
    degenerate = []
    for n in graph.get("nodes", []):
        nid = n.get("id")
        is_conn = n.get("type") == "connector"
        bb = n.get("bbox")
        rect = None
        if bb and len(bb) == 4:
            x1, y1, x2, y2 = (float(v) for v in bb)
            if x2 - x1 > 0.0 and y2 - y1 > 0.0:
                rect = (x1, y1, x2, y2)
        if rect is not None:
            geom = shp_box(*rect)
        else:
            c = n.get("centroid")
            if not c or len(c) < 2:
                continue
            geom = Point(float(c[1]), float(c[0]))   # centroid = [y, x]
            degenerate.append((nid, is_conn))
        items.append((nid, is_conn, rect, geom))
    return items, degenerate


def _kind(a_conn, b_conn):
    if a_conn and b_conn:
        return "conn-conn"
    if a_conn or b_conn:
        return "block-conn"
    return "block-block"


def _rect_overlap(ra, rb):
    """(ox, oy) — перекрытие по осям; отрицательное = зазор."""
    if ra is None or rb is None:
        return None
    ox = min(ra[2], rb[2]) - max(ra[0], rb[0])
    oy = min(ra[3], rb[3]) - max(ra[1], rb[1])
    return ox, oy


# ───────────────────────── ядро проверки ─────────────────────────

def strict_pairs(items):
    """Пары геометрий с ненулевой площадью пересечения + касания."""
    geoms = [g for _, _, _, g in items]
    tree = STRtree(geoms)
    pos, touch = [], []
    seen = set()
    for i, (nid, is_conn, rect, g) in enumerate(items):
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if j == i:
                continue
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            other = items[j]
            inter = g.intersection(other[3])
            area = float(inter.area)
            ov = _rect_overlap(rect, other[2])
            rec = {"a": nid, "b": other[0],
                   "kind": _kind(is_conn, other[1]),
                   "area_px2": round(area, 4),
                   "ox": round(ov[0], 4) if ov else None,
                   "oy": round(ov[1], 4) if ov else None,
                   "depth_px": round(min(ov), 4) if ov else 0.0}
            (pos if area > 0.0 else touch).append(rec)
    return pos, touch


def strict_block_pairs(graph):
    """Число строго пересекающихся пар block-block.

    Эквивалент `verify_overlaps.verify(graph)["strict"]["by_kind"]
    ["block-block"]` стенда: там это счётчик по тем же записям strict_pairs.
    """
    items, _degenerate = collect_items(graph)
    pos, _touch = strict_pairs(items)
    return sum(1 for r in pos if r["kind"] == "block-block")
