# -*- coding: utf-8 -*-
"""_triggers.py — детекция трёх триггеров (схлопнутое ребро / бокс на
магистрали / коннектор впритык).

Источник: стенд `_scratch/layout_align/triggers.py` (строки 33-162).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.
Гейт использует только box_on_magi, но детектор перенесён целиком: три
триггера считаются одним проходом и разделять их значило бы менять логику.
"""
from __future__ import annotations

import math

from ..graph_access import (edge_ends, edge_polyline, edges, is_connector,
                            node_cxy, nodes_by_id)

FLOOR = 6.0    # px: порог «схлопнуто / впритык»
MAGI = 60.0    # px: порог «магистраль»
_SHRINK = 2.0  # px: сжатие bbox в seg_rect (как в прототипе)


def _edge_len(e):
    """Нарисованная длина полилинии ребра."""
    pl = edge_polyline(e)
    if len(pl) < 2:
        return 0.0
    return sum(math.hypot(pl[i + 1][0] - pl[i][0], pl[i + 1][1] - pl[i][1])
               for i in range(len(pl) - 1))


def _seg_rect(p1, p2, r, sh=_SHRINK):
    """Пересекает ли сегмент p1-p2 прямоугольник r (сжатый на sh)? (Liang-Barsky)."""
    x1, y1, x2, y2 = r[0] + sh, r[1] + sh, r[2] - sh, r[3] - sh
    if x2 <= x1 or y2 <= y1:
        return False
    xa, ya = p1
    xb, yb = p2
    dx, dy = xb - xa, yb - ya
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, xa - x1), (dx, x2 - xa), (-dy, ya - y1), (dy, y2 - ya)):
        if abs(p) < 1e-9:
            if q < 0:
                return False
        else:
            rr = q / p
            if p < 0:
                t0 = max(t0, rr)
            else:
                t1 = min(t1, rr)
            if t0 > t1:
                return False
    return True


def _pt_seg(px, py, ax, ay, bx, by):
    """Расстояние от точки (px,py) до отрезка (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0,
                                         ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def detect(graph, floor=FLOOR, magi=MAGI):
    """Найти все три типа триггеров в graph.

    Возвращает dict:
        'collapsed':   [(s, t, x, y), ...]   x,y — середина между узлами
        'box_on_magi': [(box_id, cx, cy), ...]  cx,cy — центр bbox
        'conn_stuck':  [(conn_id, cx, cy), ...] cx,cy — центроид коннектора
    """
    byid = nodes_by_id(graph)
    EL = list(edges(graph))

    inc = {}
    for e in EL:
        s, t = edge_ends(e)
        inc.setdefault(s, []).append(e)
        inc.setdefault(t, []).append(e)

    # оборудование с валидным bbox + длинные рёбра (магистрали)
    EQ = [(n["id"], tuple(n["bbox"])) for n in graph.get("nodes", [])
          if not is_connector(n) and n.get("bbox")]
    LONG = [(edge_ends(e), edge_polyline(e)) for e in EL
            if _edge_len(e) > magi]

    # (a) схлопнутое ребро оборудования < floor
    collapsed = []
    for e in EL:
        s, t = edge_ends(e)
        a, b = byid.get(s), byid.get(t)
        if not a or not b or (is_connector(a) and is_connector(b)):
            continue
        if _edge_len(e) < floor:
            ax, ay = node_cxy(a)
            bx, by = node_cxy(b)
            collapsed.append((s, t, (ax + bx) / 2, (ay + by) / 2))

    # (b) бокс оборудования на ЧУЖОЙ магистрали
    box_on_magi = []
    for bid, bb in EQ:
        for (s, t), pl in LONG:
            if s == bid or t == bid:
                continue
            if any(_seg_rect(pl[i], pl[i + 1], bb)
                   for i in range(len(pl) - 1)):
                cx = (bb[0] + bb[2]) / 2
                cy = (bb[1] + bb[3]) / 2
                box_on_magi.append((bid, cx, cy))
                break

    # (c) коннектор впритык (< floor) к ЧУЖОЙ трубе
    conn_stuck = []
    for n in graph.get("nodes", []):
        if not is_connector(n):
            continue
        cid = n["id"]
        cx, cy = node_cxy(n)
        own = {id(e) for e in inc.get(cid, [])}
        best = 1e9
        for e in EL:
            if id(e) in own:
                continue
            s, t = edge_ends(e)
            if s == cid or t == cid:
                continue
            pl = edge_polyline(e)
            for i in range(len(pl) - 1):
                d = _pt_seg(cx, cy, pl[i][0], pl[i][1],
                            pl[i + 1][0], pl[i + 1][1])
                if d < best:
                    best = d
            if best < floor:
                break
        if best < floor:
            conn_stuck.append((cid, cx, cy))

    return {"collapsed": collapsed,
            "box_on_magi": box_on_magi,
            "conn_stuck": conn_stuck}


def count(graph, floor=FLOOR, magi=MAGI):
    """(na, nb, nc) — число триггеров каждого типа."""
    d = detect(graph, floor, magi)
    return len(d["collapsed"]), len(d["box_on_magi"]), len(d["conn_stuck"])
