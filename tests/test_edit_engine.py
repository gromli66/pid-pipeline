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


def test_poly_adjust_moves_adjacent_waypoint_with_end():
    # репро graph_edited0598 (хвостики 18px): сдвиг конца вдоль участка
    # обязан утащить смежное колено — подводящий стаб остаётся прямым
    n = _poly_node()
    e = {"id": "e", "source": "p", "target": "c",
         "source_point": [2.0, 60.0], "target_point": [2.0, 200.0],
         "waypoints": [[2.0, 100.0]]}
    x, y = edit_engine.seat_end(n, None, e, "s", e["source_point"],
                                100.0, 2.0, try_slack=False,
                                snap_threshold=12, node_edges=[e])
    assert (x, y) == (60.0, 6.0)              # отступ от вершины
    assert e["waypoints"][0] == [6.0, 100.0]  # колено уехало вместе с концом


def test_poly_adjust_skips_distribution_for_skewed_stub():
    # стаб не перпендикулярен участку — не косить: посадка «как получились»
    n = _poly_node()
    e = {"id": "e", "source": "p", "target": "c",
         "source_point": [2.0, 60.0], "target_point": [2.0, 200.0],
         "waypoints": [[40.0, 100.0]]}       # стаб (60,2)->(100,40) косой
    x, y = edit_engine.seat_end(n, None, e, "s", e["source_point"],
                                100.0, 40.0, try_slack=False,
                                snap_threshold=12, node_edges=[e])
    assert e["waypoints"][0] == [40.0, 100.0]  # колено не тронуто


def test_poly_slot_collision_takes_free_slot():
    # репро graph_edited0598 (оба конца в одном слоте): позиция, занятая
    # соседом, не выдаётся — берётся свободный слот
    n = _poly_node()
    sib = {"id": "e1", "source": "p", "target": "c1",
           "source_point": [21.0, 60.0], "target_point": [21.0, 120.0],
           "waypoints": []}
    e = {"id": "e2", "source": "p", "target": "c2",
         "source_point": [30.0, 60.0], "target_point": [10.0, 120.0],
         "waypoints": []}
    x, y = edit_engine.seat_end(n, None, e, "s", e["source_point"],
                                120.0, 10.0, try_slack=False,
                                snap_threshold=12, node_edges=[sib, e])
    assert x == 60.0
    assert abs(y - 21.0) >= 2.0               # чужой слот не занят
    assert y == 39.0                          # взят свободный


def test_skin_seats_on_bbox_frame():
    # решение 2026-08-01 «символ тянется на рамку»: конец скин-узла — на
    # рамке bbox, не на letterbox-прямоугольнике серверного канона
    from modules.graph.core.pretransform import FIXED_SIZES
    cls = sorted(FIXED_SIZES)[0]
    n = {"id": "s", "type": "equipment", "class_name": cls,
         "centroid": [21.5, 140.0], "bbox": [0, 0, 280, 43]}
    x, y = edit_engine.seat_end(n, None, {"id": "e"}, "s", None, 400.0, 21.5,
                                try_slack=False, snap_threshold=12)
    assert (x, y) == (280.0, 21.5)


def test_two_edges_same_side_opposite_directions_slotted():
    # репро заказчика 2026-08-01: цели в РАЗНЫЕ стороны — раньше оба конца
    # сливались в середину; сторона соседа теперь судится его ориентиром
    node = _box("a", 0, 0, 40, 40)
    e1 = {"id": "e1", "source": "a", "target": "c1",
          "source_point": [20.0, 40.0], "target_point": [-40.0, 100.0],
          "waypoints": []}
    e2 = {"id": "e2", "source": "a", "target": "c2",
          "source_point": [20.0, 40.0], "target_point": [80.0, 100.0],
          "waypoints": []}
    edges = [e1, e2]
    x1, y1 = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                  100.0, -40.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e1["source_point"] = [y1, x1]
    x2, y2 = edit_engine.seat_end(node, None, e2, "s", e2["source_point"],
                                  100.0, 80.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    assert x1 == x2 == 40.0
    assert y1 < 20.0 < y2                     # разведены, не в одну точку
    assert abs(y1 - y2) >= 2.0


def test_slots_survive_frame_change():
    # репро «наслаиваются при resize»: рамка выросла, концы соседей ещё на
    # СТАРОЙ рамке — членство по ориентирам всё равно видит обоих (k=2)
    node = _box("a", 0, 0, 40, 40)
    e1 = {"id": "e1", "source": "a", "target": "c1",
          "source_point": [20.0, 40.0], "target_point": [15.0, 140.0],
          "waypoints": []}
    e2 = {"id": "e2", "source": "a", "target": "c2",
          "source_point": [20.0, 40.0], "target_point": [25.0, 140.0],
          "waypoints": []}
    node["bbox"] = [0, 0, 80, 60]             # resize: старые концы вне рамки
    edges = [e1, e2]
    x1, y1 = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                  140.0, 15.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e1["source_point"] = [y1, x1]
    x2, y2 = edit_engine.seat_end(node, None, e2, "s", e2["source_point"],
                                  140.0, 25.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    assert x1 == x2 == 80.0                   # новая грань
    assert abs(y1 - y2) >= 2.0                # НЕ наслоились
    assert y1 < 30.0 < y2                     # симметрично вокруг середины


def test_thick_edges_get_wider_slot_pitch():
    # репро 2026-08-01 «после ресайза [толщины] ребра наслаиваются»: шаг
    # слотов держит зазор по ЧЕРНИЛАМ — (w1+w2)/2 + 4 между осями
    node = _box("a", 0, 0, 60, 60)
    e1 = {"id": "e1", "source": "a", "target": "c1", "render_width": 16,
          "source_point": [30.0, 60.0], "target_point": [25.0, 140.0],
          "waypoints": []}
    e2 = {"id": "e2", "source": "a", "target": "c2", "render_width": 16,
          "source_point": [30.0, 60.0], "target_point": [35.0, 140.0],
          "waypoints": []}
    edges = [e1, e2]
    x1, y1 = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                  140.0, 25.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e1["source_point"] = [y1, x1]
    x2, y2 = edit_engine.seat_end(node, None, e2, "s", e2["source_point"],
                                  140.0, 35.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    assert x1 == x2 == 60.0
    assert abs(y2 - y1) == 20.0               # (16+16)/2 + 4, не 18
    # чистый зазор между краями линий >= 4px
    assert abs(y2 - y1) - 16.0 >= 4.0


def test_default_width_pitch_unchanged():
    # бит-совместимость: без render_width шаг прежний (18)
    node, e1, e2 = _two_edge_node()
    edges = [e1, e2]
    x1, y1 = edit_engine.seat_end(node, None, e1, "s", e1["source_point"],
                                  100.0, 15.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    e1["source_point"] = [y1, x1]
    x2, y2 = edit_engine.seat_end(node, None, e2, "s", e2["source_point"],
                                  100.0, 25.0, try_slack=False,
                                  snap_threshold=12, node_edges=edges)
    assert abs(abs(y2 - y1) - 40.0 / 3.0) < 1e-9   # min(18, 40/3) — как раньше
