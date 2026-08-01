# -*- coding: utf-8 -*-
"""Этап B — оконная libavoid-сессия drag (modules/graph/core/edit_avoid.py).

Модульные тесты сессии на голых данных (Qt не нужен): окно, пины в
посаженных концах, кадр moveShape+транзакция, фиксы чужих рёбер,
сторожа молчаливого fallback, рычаг PID_EDIT_AVOID.

Ловушки биндинга, которые здесь закреплены (снятые живым пробником):
пины не удаляются и не двигаются — сместившийся конец обязан получать
НОВЫЙ classId через setEndpoints; прокси живут в keep-списке сессии.
"""
import pytest

from modules.graph.core import edit_avoid

if edit_avoid.load_binding() is None:
    pytest.skip("биндинг libavoid недоступен — сессия и не строится "
                "(редактор остаётся на лестнице)", allow_module_level=True)


def _ekey(a, b):
    return tuple(sorted((a, b)))


def _box(nid, x1, y1, x2, y2):
    return {"id": nid, "type": "equipment",
            "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
            "bbox": [x1, y1, x2, y2], "segmentation": None,
            "class_id": 99, "class_name": "unknow"}


def _edge(eid, s, t, sp_xy, tp_xy, wps_xy=(), **extra):
    e = {"id": eid, "source": s, "target": t,
         "source_point": [sp_xy[1], sp_xy[0]],
         "target_point": [tp_xy[1], tp_xy[0]],
         "waypoints": [[y, x] for x, y in wps_xy]}
    e.update(extra)
    return e


def _mini():
    """A --- B с препятствием C на прямой между ними."""
    nodes = {
        "a": _box("a", 0, 0, 40, 40),
        "b": _box("b", 200, 0, 240, 40),
        "c": _box("c", 100, -10, 140, 30),
    }
    edges = [_edge("e1", "a", "b", (40.0, 20.0), (200.0, 20.0))]
    return nodes, edges


def _session(nodes, edges, moving, live):
    return edit_avoid.AvoidDragSession.build(
        nodes, edges, moving, {_ekey(*k) for k in live}, _ekey)


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PID_EDIT_AVOID", "0")
    nodes, edges = _mini()
    assert _session(nodes, edges, {"a"}, [("a", "b")]) is None


def test_no_live_edges_no_session():
    nodes, edges = _mini()
    assert edit_avoid.AvoidDragSession.build(
        nodes, edges, {"a"}, set(), _ekey) is None


def test_route_avoids_obstacle_with_clearance():
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    assert ses is not None
    out = ses.route_frame(nodes, edges)
    r = out[_ekey("a", "b")]
    assert r["ends_ok"] and r["ortho"]
    pts = r["pts"]
    # концы в посаженных концах данных
    assert pts[0] == (40.0, 20.0) and pts[-1] == (200.0, 20.0)
    # обход препятствия c с клиренсом: маршрут ортогонален, поэтому
    # bbox-пересечение сегмента с раздутым на (CLEARANCE - 0.5) нутром
    # c == настоящее пересечение; ни один сегмент туда не заходит
    m = edit_avoid.CLEARANCE - 0.5
    ix1, iy1, ix2, iy2 = 100 - m, -10 - m, 140 + m, 30 + m
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        assert (max(ax, bx) < ix1 or min(ax, bx) > ix2
                or max(ay, by) < iy1 or min(ay, by) > iy2), \
            f"сегмент {(ax, ay)}->{(bx, by)} ближе клиренса к препятствию c"
    ses.close()


def test_frame_follows_move():
    """moveShape ведёт пины: конец на форме едет с узлом без нового пина,
    маршрут перестраивается от новой позиции."""
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    # кадр: узел a уехал вниз на 60, посадка конца едет с ним
    for k in (1, 3):
        nodes["a"]["bbox"][k] += 60.0
    nodes["a"]["centroid"][0] += 60.0
    edges[0]["source_point"] = [80.0, 40.0]        # [y, x]
    out = ses.route_frame(nodes, edges)
    r = out[_ekey("a", "b")]
    assert r["ends_ok"] and r["ortho"]
    assert r["pts"][0] == (40.0, 80.0)
    ses.close()


def test_pin_slot_shift_new_class():
    """Слот сместился ПО ГРАНИ (узел стоит) — новый пин через setEndpoints:
    маршрут стартует из нового слота (в биндинге пины не двигаются)."""
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    edges[0]["source_point"] = [30.0, 40.0]        # слот уехал по грани R
    out = ses.route_frame(nodes, edges)
    r = out[_ekey("a", "b")]
    assert r["ends_ok"] and r["ortho"]
    assert r["pts"][0] == (40.0, 30.0)
    ses.close()


def test_foreign_edge_fixed_not_rerouted():
    """Чужое ребро — фикс: в выдаче кадра его нет, данные не тронуты."""
    nodes, edges = _mini()
    nodes["d"] = _box("d", 100, 60, 140, 100)
    foreign = _edge("e2", "c", "d", (120.0, 30.0), (120.0, 60.0))
    snapshot = {k: (list(foreign["source_point"]),
                    list(foreign["target_point"]),
                    [list(w) for w in foreign["waypoints"]])
                for k in ("snap",)}["snap"]
    edges.append(foreign)
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    out = ses.route_frame(nodes, edges)
    assert _ekey("c", "d") not in out
    assert (foreign["source_point"], foreign["target_point"],
            foreign["waypoints"]) == (snapshot[0], snapshot[1], snapshot[2])
    ses.close()


def test_manual_route_of_moving_node_is_fixed():
    """_manual_route таскаемого узла не live даже как инцидентное —
    редактор его в live_keys не даёт; сессия держит фиксом и обновляет
    по данным (трансляция концов на кадре)."""
    nodes, edges = _mini()
    manual = _edge("e3", "a", "c", (20.0, 0.0), (110.0, -10.0),
                   _manual_route=True)
    edges.append(manual)
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    assert _ekey("a", "c") not in ses.route_frame(nodes, edges)
    ses.close()


def test_straight_pair_two_point_route():
    """Соосные пины — прямая (2 точки, mid пуст): паритет гашения H7."""
    nodes, edges = _mini()
    del nodes["c"]                                  # прямой коридор
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    r = ses.route_frame(nodes, edges)[_ekey("a", "b")]
    assert r["ends_ok"] and r["ortho"] and len(r["pts"]) == 2
    ses.close()


def test_window_cap_keeps_moving_and_live():
    """Окно > MAX_SHAPES фигур капается; таскаемый узел и концы live-рёбер
    обязаны остаться в окне."""
    nodes, edges = _mini()
    for i in range(edit_avoid.MAX_SHAPES + 30):
        gx = 1000.0 + (i % 30) * 60.0
        gy = 1000.0 + (i // 30) * 60.0
        nodes[f"far_{i}"] = _box(f"far_{i}", gx, gy, gx + 40, gy + 40)
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    assert ses._capped
    assert "a" in ses._shapes and "b" in ses._shapes
    assert len(ses._shapes) <= edit_avoid.MAX_SHAPES
    r = ses.route_frame(nodes, edges)[_ekey("a", "b")]
    assert r["ends_ok"] and r["ortho"]
    ses.close()


def test_needs_rebuild_only_when_capped():
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    nodes["a"]["centroid"] = [20.0 + edit_avoid.REBUILD_DIST + 50, 20.0]
    assert not ses.needs_rebuild(nodes)             # окно полное — незачем
    ses.close()


def test_pin_cache_no_churn_on_zigzag():
    """Конец гуляет между ДВУМЯ смещениями (зигзаг/гистерезис) — пины
    переиспользуются из кэша, чурна нет (в биндинге пин вечен)."""
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    ses.route_frame(nodes, edges)
    base = ses._pin_count
    for k in range(40):
        edges[0]["source_point"] = [20.0 if k % 2 else 30.0, 40.0]
        ses.route_frame(nodes, edges)
    assert ses._pin_count <= base + 2, \
        f"чурн пинов на зигзаге: {ses._pin_count - base} новых"
    ses.close()


def test_pin_budget_triggers_rebuild():
    """Монотонно скользящая посадка (замок оси у контуров) рождает пин на
    кадр — перебор бюджета обязан просить пересборку сессии."""
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    k = 0
    while not ses.needs_rebuild(nodes):
        k += 1
        assert k < edit_avoid.PIN_BUDGET + 50, "needs_rebuild не наступил"
        edges[0]["source_point"] = [20.0 + 0.01 * k, 40.0]
        ses.route_frame(nodes, edges)
    assert ses._pin_count > edit_avoid.PIN_BUDGET
    ses.close()


def test_connector_virtual_box_is_shape_with_pin():
    """Коннектор с виртуальным боксом — фигура с пином в центре: свой
    маршрут доходит до стыка, ЧУЖОЙ обходит бокс с клиренсом
    (сторож == судья: _fb_seg_bad судит коннектор тем же боксом)."""
    nodes, edges = _mini()
    del nodes["c"]
    # стык-коннектор ровно на прямой a-b
    nodes["j"] = {"id": "j", "type": "connector", "centroid": [20.0, 120.0],
                  "bbox": None, "segmentation": None,
                  "class_id": -1, "class_name": "connector"}
    vb = {"j": (116.0, 16.0, 124.0, 24.0)}
    ses = edit_avoid.AvoidDragSession.build(
        nodes, edges, {"a"}, {_ekey("a", "b")}, _ekey, virtual_boxes=vb)
    out = ses.route_frame(nodes, edges)
    r = out[_ekey("a", "b")]
    assert r["ends_ok"] and r["ortho"]
    # чужой для ребра коннектор обойдён: сегменты вне бокса с клиренсом
    m = edit_avoid.CLEARANCE - 0.5
    for (ax, ay), (bx, by) in zip(r["pts"], r["pts"][1:]):
        assert (max(ax, bx) < 116 - m or min(ax, bx) > 124 + m
                or max(ay, by) < 16 - m or min(ay, by) > 24 + m), \
            f"сегмент {(ax, ay)}->{(bx, by)} лёг на стык j"
    # свой коннектор: live-ребро к нему садится в его центр
    edges.append(_edge("e_j", "b", "j", (200.0, 20.0), (120.0, 20.0)))
    ses.close()
    ses = edit_avoid.AvoidDragSession.build(
        nodes, edges, {"b"}, {_ekey("b", "j")}, _ekey, virtual_boxes=vb)
    r2 = ses.route_frame(nodes, edges)[_ekey("b", "j")]
    assert r2["ends_ok"], "пин на своём виртуальном боксе недостижим"
    ses.close()


def test_stale_model_raises():
    """Undo/redo/delete снапшотом подменяет словари рёбер — сессия обязана
    заметить отвязку и попросить закрытие (StaleModelError)."""
    import copy
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    ses.route_frame(nodes, edges)
    detached = [copy.deepcopy(e) for e in edges]
    with pytest.raises(edit_avoid.StaleModelError):
        ses.route_frame(nodes, detached)
    ses.close()


def test_gc_survives_frames():
    """SWIG-GC-ловушка: агрессивный gc между кадрами не вынимает
    пины/шейпы из роутера (keep-список сессии держит прокси)."""
    import gc
    nodes, edges = _mini()
    ses = _session(nodes, edges, {"a"}, [("a", "b")])
    base = ses.route_frame(nodes, edges)[_ekey("a", "b")]["pts"]
    old = gc.get_threshold()
    gc.set_threshold(1, 1, 1)
    try:
        for k in range(5):
            gc.collect()
            r = ses.route_frame(nodes, edges)[_ekey("a", "b")]
            assert r["ends_ok"] and r["ortho"] and r["pts"] == base
    finally:
        gc.set_threshold(*old)
        ses.close()
