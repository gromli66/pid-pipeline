# -*- coding: utf-8 -*-
"""Э4 — движок адресного сглаживания (`modules/graph/core/edit_smooth`).

Проверяется «со всех сторон» (требование заказчика 2026-08-02): все виды
посадки (рамка/слот, контурный участок, коннектор-центроид, ЯКОРЬ оператора),
все типы рёбер (прямая диагональ, зигзаг с одной ступенькой, многоступенчатый,
короткое ребро-датчик), и то, что неприкосновенно — не двигается.

Чистый stdlib: движок без Qt, роутинг подаётся колбэком.
"""
import copy

import pytest

from modules.graph.core import edit_smooth as es
from modules.graph.core import ports


def _g(nodes, links):
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [1080, 1920]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _box(nid, cx, cy, w=40.0, h=40.0, **kw):
    n = {"id": nid, "type": "equipment", "class_id": 99,
         "class_name": "unknow", "centroid": [cy, cx],
         "bbox": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
         "segmentation": None}
    n.update(kw)
    return n


def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "class_id": -1,
            "class_name": "connector", "centroid": [cy, cx],
            "bbox": None, "segmentation": None}


def _edge(eid, s, t, sp, tp, wps=None):
    return {"id": eid, "source": s, "target": t,
            "source_point": [sp[1], sp[0]], "target_point": [tp[1], tp[0]],
            "waypoints": [[w[1], w[0]] for w in (wps or [])]}


# ── распознавание дефектов ─────────────────────────────────────────────

def test_single_step_detected():
    """Труба идёт, делает шажок вбок и продолжает — это ступенька."""
    e = _edge("e", "a", "b", (100.0, 100.0), (400.0, 112.0),
              [(200.0, 100.0), (200.0, 112.0)])
    assert es.single_step(e) == ("y", 12.0)


def test_two_steps_not_taken():
    """Многоступенчатый в автомат не берётся (связанность — см. шапку)."""
    e = _edge("e", "a", "b", (100.0, 100.0), (500.0, 124.0),
              [(200.0, 100.0), (200.0, 112.0),
               (350.0, 112.0), (350.0, 124.0)])
    assert es.single_step(e) is None
    assert es.shift_target(e) is None


def test_plain_diagonal_is_shift_target():
    """Прямая диагональ — тоже «легко превратить в прямую»."""
    e = _edge("e", "a", "b", (100.0, 100.0), (300.0, 108.0))
    axis, d = es.shift_target(e)
    assert axis == "y" and d == pytest.approx(8.0)


def test_sensor_edge_untouched():
    """Короткое ребро-датчик (< SEG_FLOOR) — структурный пол, не судится."""
    e = _edge("e", "a", "b", (100.0, 100.0), (104.0, 102.0))
    assert es.shift_target(e) is None
    assert es.diag_segments_of(e) == []


# ── подтягивание излома: ничего, кроме колена, не двигается ────────────

def test_snap_waypoint_straightens_inner_slant():
    e = _edge("e", "a", "b", (100.0, 100.0), (100.0, 300.0),
              [(100.0, 180.0), (109.0, 240.0)])
    sp0 = list(e["source_point"])
    tp0 = list(e["target_point"])
    assert es.snap_waypoints(e) >= 1
    assert es.is_ortho(e), f"маршрут остался косым: {es.edge_pts(e)}"
    assert e["source_point"] == sp0 and e["target_point"] == tp0, \
        "посаженные концы обязаны остаться на месте"


def test_snap_does_not_move_seated_ends():
    """Косой сегмент упирается в посаженный конец — излом не выдумывается."""
    e = _edge("e", "a", "b", (100.0, 100.0), (300.0, 140.0),
              [(200.0, 140.0)])
    before = copy.deepcopy(e)
    es.snap_waypoints(e)
    assert e["source_point"] == before["source_point"]
    assert e["target_point"] == before["target_point"]


# ── классификация посадок ──────────────────────────────────────────────

def test_end_kind_all_kinds():
    box = _box("b", 100.0, 100.0)
    conn = _conn("c", 300.0, 100.0)
    poly = _box("p", 500.0, 100.0)
    poly["segmentation"] = [480.0, 80.0, 520.0, 80.0, 520.0, 120.0,
                            480.0, 120.0]
    e = _edge("e", "b", "c", (120.0, 100.0), (300.0, 100.0))
    assert es.end_kind(box, e) == "rect"
    assert es.end_kind(conn, e) == "connector"
    assert es.end_kind(poly, e) == "contour"
    assert es.end_kind(None, e) == "none"
    ports.add_manual_port(box, 120.0, 108.0, e)
    assert es.end_kind(box, e) == "anchor", "якорь обязан быть виден первым"


# ── гейт ───────────────────────────────────────────────────────────────

def test_gate_rejects_side_damage():
    base = {"diag": 3, "dev": 30.0, "near": 0, "along_own": 0,
            "along_foreign": 0, "through": 0, "corner": 0, "adrift": 0,
            "conn_off": 0, "poly_off": 0, "w": 100.0, "h": 100.0}
    better = dict(base, diag=2, dev=20.0)
    assert es.accepts(base, better)
    assert not es.accepts(base, dict(better, through=1)), \
        "ход, родивший прошивание, обязан быть отклонён"
    assert not es.accepts(base, dict(better, along_own=1))
    assert not es.accepts(base, dict(better, w=es.CANVAS_W + 10)), \
        "выход за холст обязан отменять ход"
    assert not es.accepts(base, dict(base, diag=3, dev=30.0)), \
        "ход без выигрыша по ортогональности не принимается"


# ── главный цикл: что неприкосновенно ──────────────────────────────────

def _canvas_with_anchor():
    a = _box("a", 100.0, 100.0)
    b = _box("b", 300.0, 108.0)
    e = _edge("e1", "a", "b", (120.0, 100.0), (280.0, 108.0))
    ports.add_manual_port(a, 120.0, 100.0, e)
    return _g([a, b], [e])


def test_anchor_never_moves():
    """Точка, поставленная оператором, не двигается ни одним лекарством."""
    g = _canvas_with_anchor()
    e = g["links"][0]
    sp0 = list(e["source_point"])
    es.smooth(g, route_fn=None, reseat_fn=None)
    assert e["source_point"] == sp0, "якорь входа сорван сглаживанием"


def test_connector_end_stays_centroid():
    """У коннектора конец — центроид: двигается сам коннектор, а конец
    обязан остаться в нём."""
    a = _box("a", 100.0, 100.0)
    c = _conn("c", 300.0, 112.0)
    e = _edge("e1", "a", "c", (120.0, 100.0), (300.0, 112.0))
    g = _g([a, c], [e])
    es.smooth(g, route_fn=None, reseat_fn=None)
    cy, cx = c["centroid"]
    assert e["target_point"] == pytest.approx([cy, cx]), \
        "конец коннектора обязан совпадать с его центроидом"


def test_topology_untouched():
    """Ни одно лекарство не меняет состав узлов и рёбер."""
    g = _canvas_with_anchor()
    ids0 = ([n["id"] for n in g["nodes"]], [e["id"] for e in g["links"]])
    es.smooth(g, route_fn=None, reseat_fn=None)
    assert ([n["id"] for n in g["nodes"]], [e["id"] for e in g["links"]]) == ids0


def test_big_shift_refused():
    """«Супер движение» (больше бюджета) не делается — уходит оператору."""
    a = _box("a", 100.0, 100.0)
    b = _box("b", 400.0, 200.0)          # расхождение 100px >> BUDGET
    e = _edge("e1", "a", "b", (120.0, 100.0), (380.0, 200.0))
    g = _g([a, b], [e])
    before = copy.deepcopy(g)
    st = es.smooth(g, route_fn=None, reseat_fn=None)
    assert st["узел"] == 0 and st["коннектор"] == 0
    assert g["nodes"] == before["nodes"], "оборудование не должно двигаться"


def test_idempotent():
    """Второй прогон ничего не меняет."""
    g = _canvas_with_anchor()
    es.smooth(g, route_fn=None, reseat_fn=None)
    snapshot = copy.deepcopy(g)
    st = es.smooth(g, route_fn=None, reseat_fn=None)
    assert st["колено"] == st["излом"] == st["скольжение"] == 0
    assert st["коннектор"] == st["узел"] == 0
    assert g == snapshot, "повторное сглаживание изменило холст"
