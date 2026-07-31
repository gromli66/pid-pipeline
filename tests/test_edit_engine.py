# -*- coding: utf-8 -*-
"""Тесты движка посадки edit_engine (Э2 пересборки). Без Qt.

Координаты в комментариях — (x, y); в данных [y, x], bbox [x1, y1, x2, y2].
"""
from modules.graph.core import edit_engine


def _box(nid, x1, y1, x2, y2, cls="block"):
    return {"id": nid, "type": "equipment", "class_name": cls,
            "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
            "bbox": [x1, y1, x2, y2]}


def _conn(nid, x, y):
    return {"id": nid, "type": "connector", "centroid": [y, x], "bbox": None}


def test_connector_seats_on_centroid():
    n = _conn("c", 50, 60)
    x, y = edit_engine.seat_end(n, None, {}, "s", None, 200.0, 60.0,
                                try_slack=False, snap_threshold=12)
    assert (x, y) == (50.0, 60.0)


def test_straight_lock_keeps_axis():
    # подводящий сегмент строго горизонтален по y=20 — конец на оси, на грани
    n = _box("a", 0, 0, 40, 40)
    x, y = edit_engine.seat_end(n, None, {}, "s", [20.0, 40.0], 100.0, 20.0,
                                try_slack=False, snap_threshold=12)
    assert (x, y) == (40.0, 20.0)


def test_ineffective_lock_falls_to_port_not_corner():
    # ось y=50 вне рамки [0..40]: кламп дал бы угол (40, 40) — замок
    # объявляется неэффективным, конец уходит на порт (середина грани)
    n = _box("a", 0, 0, 40, 40)
    x, y = edit_engine.seat_end(n, None, {}, "s", [50.0, 40.0], 100.0, 50.0,
                                try_slack=False, snap_threshold=12)
    assert (x, y) == (40.0, 20.0)
    assert (x, y) != (40.0, 40.0)


def test_box_without_lock_seats_on_midpoint_port():
    n = _box("a", 0, 0, 40, 40)
    x, y = edit_engine.seat_end(n, None, {}, "s", None, 100.0, 30.0,
                                try_slack=False, snap_threshold=12)
    assert (x, y) == (40.0, 20.0)


def test_side_slots_k1_is_center():
    from modules.graph.core import ports
    n = _box("a", 0, 0, 40, 40)
    assert ports.side_slots(n, "R", 1) == [(40.0, 20.0, 1.0, 0.0, False)]


def test_side_slots_symmetric_inside_face():
    from modules.graph.core import ports
    n = _box("a", 0, 0, 40, 40)
    s = ports.side_slots(n, "R", 3)          # pitch = min(18, 40/4) = 10
    ys = [p[1] for p in s]
    assert ys == [10.0, 20.0, 30.0]          # симметрично вокруг середины
    assert all(0.0 < y < 40.0 for y in ys)   # до углов не доходит


def _two_edge_node():
    node = _box("a", 0, 0, 40, 40)
    e1 = {"id": "e1", "source": "a", "target": "c1",
          "source_point": [20.0, 40.0], "target_point": [15.0, 100.0],
          "waypoints": []}
    e2 = {"id": "e2", "source": "a", "target": "c2",
          "source_point": [20.0, 40.0], "target_point": [25.0, 100.0],
          "waypoints": []}
    return node, e1, e2


def test_two_edges_same_side_get_distinct_slots():
    # Э2b: две трубы в одну грань не сливаются в точку — слоты вокруг
    # середины, порядок по дальним ориентирам (без перехлёста)
    node, e1, e2 = _two_edge_node()
    edges = [e1, e2]
    x1, y1 = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                  100.0, 15.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e1["source_point"] = [y1, x1]
    x2, y2 = edit_engine.seat_end(node, None, e2, "s", e2["source_point"],
                                  100.0, 25.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e2["source_point"] = [y2, x2]
    assert x1 == x2 == 40.0
    assert y1 < 20.0 < y2                     # симметрично вокруг середины
    assert abs((20.0 - y1) - (y2 - 20.0)) < 1e-6


def test_slot_assignment_stable_under_ref_jitter():
    # дрожание дальних ориентиров на ±3px не меняет ни порядок, ни слоты
    node, e1, e2 = _two_edge_node()
    edges = [e1, e2]
    base = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                100.0, 15.0, try_slack=False,
                                snap_threshold=12, node_edges=edges)
    for dy in (-3.0, -1.0, 2.0, 3.0):
        e2["target_point"] = [25.0 + dy, 100.0]
        got = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                   100.0, 15.0, try_slack=False,
                                   snap_threshold=12, node_edges=edges)
        assert got == base


def test_manual_route_edges_do_not_occupy_slots():
    # _manual_route не участвует в членстве грани — авто-ребро одно => середина
    node, e1, e2 = _two_edge_node()
    e2["_manual_route"] = True
    x, y = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                100.0, 15.0, try_slack=False,
                                snap_threshold=12, node_edges=[e1, e2])
    assert (x, y) == (40.0, 20.0)
