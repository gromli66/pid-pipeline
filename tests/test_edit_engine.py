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
