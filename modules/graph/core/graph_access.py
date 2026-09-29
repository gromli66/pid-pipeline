# -*- coding: utf-8 -*-
"""graph_access.py — доступ к графу: общие утилиты без ввода-вывода.

Источник: стенд `_scratch/layout_align/harness/graph_io.py` (строки 32-119).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.
Чтение и запись файлов сюда НЕ переносятся: раскладка получает граф в памяти.

Соглашения координат (CODING_GUIDE §6):
    centroid            = [y, x]
    bbox                = [x1, y1, x2, y2]
    source/target_point = [y, x]
    path / waypoints    = [[y, x], ...]
    segmentation        = [x, y, x, y, ...]
"""
from __future__ import annotations

from copy import deepcopy

def edges(graph):
    """validated: 'links' + source/target; raw: 'edges' + from/to."""
    return graph.get("edges") if "edges" in graph else graph.get("links", [])


def edge_ends(e):
    return (e.get("source") or e.get("from"), e.get("target") or e.get("to"))


def degrees(graph):
    """Степень узлов ПО РЁБРАМ: {node_id: n} (поле `degree` узла врёт).

    Кратные рёбра считаются каждое: это честная степень (после нормализации
    кратных рёбер, блок 4, они остаются только на старых холстах)."""
    out = {}
    for e in edges(graph):
        for nid in edge_ends(e):
            if nid is not None:
                out[nid] = out.get(nid, 0) + 1
    return out


def nodes_by_id(graph):
    return {n["id"]: n for n in graph.get("nodes", [])}


def is_connector(node):
    return node.get("type") == "connector"


def node_wh(node):
    bb = node.get("bbox")
    if bb and len(bb) == 4:
        return float(bb[2] - bb[0]), float(bb[3] - bb[1])
    return 0.0, 0.0


def node_cxy(node):
    """(cx, cy) — ВНИМАНИЕ: centroid хранится как [y, x]."""
    c = node["centroid"]
    return float(c[1]), float(c[0])


def set_node_pos(node, cx, cy):
    """Сдвинуть узел в (cx, cy): centroid + bbox + segmentation согласованно."""
    ocx, ocy = node_cxy(node)
    dx, dy = cx - ocx, cy - ocy
    move_node(node, dx, dy)


def move_node(node, dx, dy):
    c = node["centroid"]
    node["centroid"] = [c[0] + dy, c[1] + dx]
    bb = node.get("bbox")
    if bb and len(bb) == 4:
        node["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
    seg = node.get("segmentation")
    if seg and isinstance(seg, list) and len(seg) >= 6:
        for i in range(0, len(seg), 2):
            seg[i] += dx
            seg[i + 1] += dy


def box_hits(graph_or_nodes, nid, bbox, margin=0.0):
    """Пересекает ли рамка `bbox` [x1, y1, x2, y2], раздутая на `margin`,
    рамку другого узла (площадь пересечения > 0; касание — нет).

    Один судья «упирается в соседа» для редактора и сервера. Узел `nid`
    и узлы без рамки (коннекторы) не считаются. `graph_or_nodes` — граф
    (`nodes`), словарь {id: node} или список узлов.
    """
    src = graph_or_nodes
    if isinstance(src, dict) and isinstance(src.get("nodes"), list):
        src = src["nodes"]
    items = src.items() if isinstance(src, dict) \
        else ((n.get("id"), n) for n in src)
    x1, y1 = bbox[0] - margin, bbox[1] - margin
    x2, y2 = bbox[2] + margin, bbox[3] + margin
    for oid, n in items:
        if str(oid) == str(nid):
            continue
        bb = n.get("bbox")
        if not bb or len(bb) != 4:
            continue
        if min(x2, bb[2]) - max(x1, bb[0]) > 0 \
                and min(y2, bb[3]) - max(y1, bb[1]) > 0:
            return True
    return False


def edge_polyline(e):
    """Полилиния ребра [(x, y), ...]: source_point -> path|waypoints -> target_point.

    path и waypoints хранятся [y, x]. Если оба есть — приоритет у waypoints
    (редактор считает их правкой маршрута); подтверждается contract-разведкой.
    """
    pts = []
    sp = e.get("source_point")
    tp = e.get("target_point")
    mid = e.get("waypoints") or e.get("path") or []
    if sp:
        pts.append((float(sp[1]), float(sp[0])))
    for p in mid:
        pts.append((float(p[1]), float(p[0])))
    if tp:
        pts.append((float(tp[1]), float(tp[0])))
    # схлопнуть дубли подряд
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out


def layout_snapshot(graph):
    """{node_id: (cx, cy, w, h, is_conn)} — для метрик."""
    snap = {}
    for n in graph.get("nodes", []):
        if "centroid" not in n:
            continue
        cx, cy = node_cxy(n)
        w, h = node_wh(n)
        snap[n["id"]] = (cx, cy, w, h, is_connector(n))
    return snap


def clone(graph):
    return deepcopy(graph)
