"""Кратные рёбра (решение Р7): между двумя узлами остаётся одно ребро.

Правило перенесено из конвертера `.prtx` (`converter-box/py/json2xml.py`,
`normalize_graph`, 664-760): группа рёбер одной пары узлов; дубль — ребро, у
которого точки прикрепления ближе `DUP_PX` к точкам уже оставленного ребра с
ОБЕИХ сторон, — удаляется; настоящая вторая труба рассекается посередине
узлом-connector `parconn_N` степени 2, все поля ребра (в том числе Ду)
копируются в обе половины.

Отличия от конвертера:
  1. целым остаётся ребро с меньшим числовым хвостом id (`edge_5` раньше
     `edge_12`), а не первое по порядку файла, — детерминизм;
  2. если один из узлов пары класса `unknow`, дубль не удаляется никогда,
     лишнее ребро только рассекается (оператор видит обе трубы);
  3. вторая половина получает id `edge_<max+1>`, а не `<id>b`; исходный id
     остаётся у половины со стороны source (на него ссылаются метки Ду).

Зовут builder (`keys=('from', 'to')`, рёбра с `path`) и `canvas_input.to_canvas`
(`keys=('source', 'target')`, рёбра без пути — режем по середине waypoints или
хорды). На графе без кратных пар — ничего не меняет. Степени не пересчитывает:
это делает вызывающий. Координаты точек — [y, x].
"""
import logging
import math
import re
from typing import Dict, List, Tuple

from .direction_nodes import _CONNECTOR_AREA, _make_connector, _max_suffix, _split_edge_at

logger = logging.getLogger(__name__)

DUP_PX = 8.0
# = nodes.UNKNOWN_CLASS_NAME; nodes.py тянет numpy, а модуль идёт и в UI-путь
_UNKNOWN = "unknow"


def _id_key(e):
    m = re.search(r"(\d+)$", str(e.get("id", "")))
    return (0, int(m.group(1))) if m else (1, str(e.get("id", "")))


def _point_at(e, nid, src_key):
    """Точка прикрепления ребра у узла nid; фолбэк — конец path."""
    at_src = e.get(src_key) == nid
    p = e.get("source_point") if at_src else e.get("target_point")
    if p:
        return p
    path = e.get("path")
    if path:
        return path[0] if at_src else path[-1]
    return None


def _is_dup(e, kept, pair, src_key):
    for k in kept:
        for nid in pair:
            p, q = _point_at(e, nid, src_key), _point_at(k, nid, src_key)
            if not (p and q) or math.hypot(p[0] - q[0], p[1] - q[1]) >= DUP_PX:
                break
        else:
            return True
    return False


def _halves_by_chord(e, cid, mp, new_id, keys):
    """Рассечь ребро без пути (холст) в точке mp: -> (первая, вторая)."""
    src_key, tgt_key = keys
    wps = e.get("waypoints") or []
    h = len(wps) // 2
    w1, w2 = (wps[:h], wps[h + 1:]) if wps else ([], [])
    pa, pb = e.get("source_point"), e.get("target_point")
    first = dict(e, **{tgt_key: cid, "target_point": list(mp)})
    second = dict(e, **{"id": new_id, src_key: cid, "source_point": list(mp),
                        tgt_key: e.get(tgt_key), "target_point": pb})
    if "waypoints" in e:
        first["waypoints"], second["waypoints"] = w1, w2
    for half, a, b in ((first, pa, mp), (second, mp, pb)):
        pts = [a] + list(half.get("waypoints") or []) + [b]
        d = sum(math.hypot(u[0] - v[0], u[1] - v[1]) for u, v in zip(pts, pts[1:]))
        if "path" in e:
            half["path"] = [list(p) for p in pts]
        if "length" in e:
            half["length"] = int(round(d))
        if "straight_line_distance" in e:
            half["straight_line_distance"] = float(math.hypot(a[0] - b[0], a[1] - b[1]))
    return first, second


def normalize_parallel_edges(nodes: List[Dict], edges: List[Dict], *,
                             keys: Tuple[str, str]) -> Dict:
    """Одно ребро на пару узлов (кроме пар с unknow — там только рассечение). На месте.

    -> stats: pairs, deleted, split, split_unknow_dup, skipped + списки
    deleted_ids, split_ids, new_nodes, new_edges, nodes_touched.
    """
    src_key, tgt_key = keys
    stats = {"pairs": 0, "deleted": 0, "split": 0, "split_unknow_dup": 0, "skipped": 0,
             "deleted_ids": [], "split_ids": [], "new_nodes": [], "new_edges": [],
             "nodes_touched": []}
    groups: Dict[frozenset, List[Dict]] = {}
    for e in edges:
        a, b = e.get(src_key), e.get(tgt_key)
        if a is None or b is None or a == b:
            continue
        groups.setdefault(frozenset((a, b)), []).append(e)
    groups = {p: es for p, es in groups.items() if len(es) > 1}
    if not groups:
        return stats

    cls = {n.get("id"): n.get("class_name") for n in nodes}
    drop, cut = set(), []          # id() удаляемых рёбер; (ребро, дубль ли) под рассечение
    touched = set()
    for pair, es in groups.items():
        stats["pairs"] += 1
        unknow = any(cls.get(nid) == _UNKNOWN for nid in pair)
        es = sorted(es, key=_id_key)
        kept = [es[0]]
        for e in es[1:]:
            dup = _is_dup(e, kept, pair, src_key)
            if dup and not unknow:
                drop.add(id(e))
                touched.update(pair)
                stats["deleted_ids"].append(e.get("id"))
                continue
            kept.append(e)
            cut.append((e, dup))

    if drop:
        edges[:] = [e for e in edges if id(e) not in drop]
    stats["deleted"] = len(stats["deleted_ids"])

    used = {e.get("id") for e in edges}
    conn_n = max(_max_suffix(nodes, "parconn_"), 0) + 1
    edge_n = _max_suffix(edges, "edge_") + 1
    for e, dup in cut:
        path = e.get("path") or []
        if keys == ("from", "to") and len(path) >= 3:
            k = len(path) // 2
            split_pt = path[k]
        else:
            pa, pb = e.get("source_point"), e.get("target_point")
            if not (pa and pb):
                stats["skipped"] += 1
                continue
            wps = e.get("waypoints") or []
            split_pt = (wps[len(wps) // 2] if wps
                        else [(pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0])
        while f"edge_{edge_n}" in used:
            edge_n += 1
        new_id = f"edge_{edge_n}"
        used.add(new_id)
        conn = _make_connector(f"parconn_{conn_n}", split_pt, area=_CONNECTOR_AREA)
        conn_n += 1
        if keys == ("from", "to") and len(path) >= 3:
            pieces, _ = _split_edge_at(e, [k], [conn["id"]], 0)
            pieces[0]["id"], pieces[1]["id"] = e.get("id"), new_id
        else:
            pieces = list(_halves_by_chord(e, conn["id"], conn["centroid"], new_id, keys))
        i = next(j for j, x in enumerate(edges) if x is e)
        edges[i:i + 1] = pieces
        nodes.append(conn)
        stats["split"] += 1
        stats["split_unknow_dup"] += int(dup)
        stats["split_ids"].append(e.get("id"))
        stats["new_nodes"].append(conn["id"])
        stats["new_edges"].append(new_id)
        touched.update((e.get(src_key), e.get(tgt_key), conn["id"]))

    stats["nodes_touched"] = sorted(touched)
    logger.info("кратные рёбра: пар %d, удалено %d, рассечено %d (дублей у unknow %d), "
                "не рассечено без точек %d", stats["pairs"], stats["deleted"], stats["split"],
                stats["split_unknow_dup"], stats["skipped"])
    return stats
