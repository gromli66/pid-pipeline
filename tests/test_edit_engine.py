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


def _poly_node():
    # квадратный контур 0..60, класс вне FIXED_SIZES — контурная посадка
    return {"id": "p", "type": "equipment", "class_name": "unknow",
            "centroid": [30.0, 30.0], "bbox": [0, 0, 60, 60],
            "segmentation": [0, 0, 60, 0, 60, 60, 0, 60]}


def test_poly_far_from_vertex_keeps_as_is():
    # «как получились»: конец на участке дальше отступа — бит-в-бит
    n = _poly_node()
    x, y = edit_engine.seat_end(n, None, {"id": "e"}, "s", [25.0, 60.0],
                                100.0, 25.0, try_slack=False,
                                snap_threshold=12, node_edges=[])
    assert (x, y) == (60.0, 25.0)


def test_poly_end_pushed_off_vertex():
    # решение 2026-08-01: конец в 2px от вершины сдвигается на отступ 6px
    n = _poly_node()
    x, y = edit_engine.seat_end(n, None, {"id": "e"}, "s", [2.0, 60.0],
                                100.0, 2.0, try_slack=False,
                                snap_threshold=12, node_edges=[])
    assert (x, y) == (60.0, 6.0)


def test_poly_two_edges_one_run_distributed():
    # две трубы в один прямой участок — не в одну точку: симметрично
    # вокруг середины участка, шаг min(18, 60/3)=18
    n = _poly_node()
    e1 = {"id": "e1", "source": "p", "target": "c1",
          "source_point": [28.0, 60.0], "target_point": [28.0, 120.0],
          "waypoints": []}
    e2 = {"id": "e2", "source": "p", "target": "c2",
          "source_point": [28.0, 60.0], "target_point": [32.0, 120.0],
          "waypoints": []}
    edges = [e1, e2]
    x1, y1 = edit_engine.seat_end(n, None, e1, "s", e1["source_point"],
                                  120.0, 28.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e1["source_point"] = [y1, x1]
    x2, y2 = edit_engine.seat_end(n, None, e2, "s", e2["source_point"],
                                  120.0, 32.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    assert x1 == x2 == 60.0
    assert (y1, y2) == (21.0, 39.0)           # 30 ± 18/2, порядок по ref
