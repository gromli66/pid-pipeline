# -*- coding: utf-8 -*-
"""Этап B — drag через оконную libavoid-сессию (интеграция редактора).

Механика фикстур — как в test_interactive_drag.py: headless-редактор,
жест теми же методами, что дёргает мышь. Здесь закреплены обещания
Этапа B: сессия включается на жест и закрывается на release; live-ребро
получает маршрут с клиренсом от чужой формы («вдоль/сквозь» исчезают по
построению); чужая труба и _manual_route байт-в-байт; предпросмотр ==
итог; undo побайтово; PID_EDIT_AVOID=0 возвращает лестницу.

Координаты двойственны (CODING_GUIDE §6): centroid/точки концов/waypoints
= [y, x]; bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from modules.graph.core import edit_avoid           # noqa: E402

if edit_avoid.load_binding() is None:
    pytest.skip("биндинг libavoid недоступен — сессия и не строится",
                allow_module_level=True)

IMG_W, IMG_H = 600, 400


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wrap(nodes, links):
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _graph():
    """A -- B по оси y=200; препятствие C ниже прямой; чужое ребро C--D."""
    nodes = [
        {"id": "box_a", "type": "equipment", "centroid": [200.0, 100.0],
         "bbox": [60.0, 160.0, 140.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "box_b", "type": "equipment", "centroid": [200.0, 460.0],
         "bbox": [420.0, 160.0, 500.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "box_c", "type": "equipment", "centroid": [290.0, 280.0],
         "bbox": [250.0, 250.0, 310.0, 330.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "box_d", "type": "equipment", "centroid": [370.0, 130.0],
         "bbox": [100.0, 350.0, 160.0, 390.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [
        {"id": "edge_1", "source": "box_a", "target": "box_b",
         "source_point": [200.0, 140.0], "target_point": [200.0, 420.0],
         "waypoints": []},
        {"id": "edge_2", "source": "box_c", "target": "box_d",
         "source_point": [330.0, 280.0], "target_point": [350.0, 130.0],
         "waypoints": [[340.0, 280.0], [340.0, 130.0]]},
    ]
    return _wrap(nodes, links)


def _editor(qapp, tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    ed = AdvancedGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


def _edge(ed, a, b):
    return ed.model.find_edge_data(ed.model.edge_key(a, b))


def _proj(e):
    return (copy.deepcopy(e.get("source_point")),
            copy.deepcopy(e.get("target_point")),
            copy.deepcopy(e.get("waypoints") or []),
            bool(e.get("_auto_route")), bool(e.get("_manual_route")))


def _full_pts(e):
    return ([(e["source_point"][1], e["source_point"][0])]
            + [(w[1], w[0]) for w in (e.get("waypoints") or [])]
            + [(e["target_point"][1], e["target_point"][0])])


def _assert_ortho(pts):
    for a, b in zip(pts, pts[1:]):
        assert abs(a[0] - b[0]) <= 0.5 or abs(a[1] - b[1]) <= 0.5, \
            f"диагональ {a}->{b}"


def test_session_lives_gesture_and_closes(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph())
    ed.start_drag_node("box_a")
    assert ed._avoid_session is not None, \
        "биндинг доступен, routable-рёбра есть — сессия обязана собраться"
    ed.drag_node_to(100.0, 260.0)
    ed.end_drag_node()
    assert ed._avoid_session is None, "release обязан закрыть сессию"


def test_avoid_route_clears_foreign_box(qapp, tmp_path):
    """Обещание B: увод узла так, что прямая легла бы на чужой бокс,
    даёт обходной маршрут с клиренсом — «вдоль/сквозь» нет по построению."""
    ed = _editor(qapp, tmp_path, _graph())
    # box_a вниз до оси box_c: прямая A-B прошла бы сквозь bbox C
    ed.start_drag_node("box_a")
    ed.drag_node_to(100.0, 290.0)
    e = _edge(ed, "box_a", "box_b")
    pts = _full_pts(e)
    _assert_ortho(pts)
    assert e.get("_auto_route") and (e.get("waypoints") or []), \
        "обход чужого бокса обязан родиться на кадре"
    ed.end_drag_node()
    # ни один сегмент не в клиренс-коридоре C (ортогональный маршрут:
    # bbox-пересечение == пересечение)
    m = edit_avoid.CLEARANCE - 0.5
    ix1, iy1 = 250.0 - m, 250.0 - m
    ix2, iy2 = 310.0 + m, 330.0 + m
    for a, b in zip(pts, pts[1:]):
        assert (max(a[0], b[0]) < ix1 or min(a[0], b[0]) > ix2
                or max(a[1], b[1]) < iy1 or min(a[1], b[1]) > iy2), \
            f"сегмент {a}->{b} лёг на чужой box_c (клиренс)"
    assert not e.get("_route_defect")


def test_foreign_edge_byte_identical(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph())
    before = _proj(_edge(ed, "box_c", "box_d"))
    ed.start_drag_node("box_a")
    for x in (120.0, 160.0, 220.0):
        ed.drag_node_to(x, 290.0)
    ed.end_drag_node()
    assert _proj(_edge(ed, "box_c", "box_d")) == before, \
        "чужая труба обязана быть байт-в-байт (контракт 2026-08-01)"


def test_manual_route_untouched_by_session(qapp, tmp_path):
    g = _graph()
    g["links"][0]["_manual_route"] = True
    g["links"][0]["waypoints"] = [[260.0, 140.0], [260.0, 420.0]]
    ed = _editor(qapp, tmp_path, g)
    ed.start_drag_node("box_c")           # box_c: ребро edge_2 live
    ed.drag_node_to(300.0, 290.0)
    e1 = _edge(ed, "box_a", "box_b")
    assert e1.get("_manual_route") and \
        e1["waypoints"] == [[260.0, 140.0], [260.0, 420.0]], \
        "_manual_route неприкосновенен для сессии"
    ed.end_drag_node()


def test_preview_equals_result(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph())
    ed.start_drag_node("box_a")
    ed.drag_node_to(100.0, 290.0)
    frame = _proj(_edge(ed, "box_a", "box_b"))
    ed.end_drag_node()
    assert _proj(_edge(ed, "box_a", "box_b")) == frame, \
        "итог жеста = последний кадр (C7)"


def test_undo_restores_bytes(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph())
    before = [_proj(e) for e in ed.edges_data]
    cent = list(ed.nodes["box_a"]["centroid"])
    ed.start_drag_node("box_a")
    ed.drag_node_to(100.0, 290.0)
    ed.end_drag_node()
    ed.undo_mgr.undo()
    assert [_proj(e) for e in ed.edges_data] == before
    assert ed.nodes["box_a"]["centroid"] == cent


def test_env_off_falls_back_to_ladder(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("PID_EDIT_AVOID", "0")
    ed = _editor(qapp, tmp_path, _graph())
    ed.start_drag_node("box_a")
    assert ed._avoid_session is None, "PID_EDIT_AVOID=0 — сессии нет"
    ed.drag_node_to(100.0, 290.0)
    e = _edge(ed, "box_a", "box_b")
    _assert_ortho(_full_pts(e))           # лестница ведёт кадр по-старому
    ed.end_drag_node()


def test_batch_all_internal_no_session_pure_translation(qapp, tmp_path):
    """Оба конца в выделении — routable-рёбер нет, сессия и не строится:
    internal-ребро едет жёсткой трансляцией (паритет со старым batch)."""
    ed = _editor(qapp, tmp_path, _graph())
    ed.selected_nodes = {"box_a", "box_b"}
    ed.start_drag_node("box_a")
    assert ed._avoid_session is None, "нечего роутить — сессия не нужна"
    ed.drag_node_to(100.0, 230.0)      # вся пара вниз на 30
    e1 = _edge(ed, "box_a", "box_b")
    assert e1["source_point"] == [230.0, 140.0]
    assert e1["target_point"] == [230.0, 420.0]
    ed.end_drag_node()


def test_batch_boundary_routed_by_session(qapp, tmp_path):
    """Смешанное выделение: boundary live-ребро ведёт сессия (ортогонально,
    без диагоналей), ребро с waypoints оператора не перекладывается."""
    ed = _editor(qapp, tmp_path, _graph())
    e2_wps_before = copy.deepcopy(_edge(ed, "box_c", "box_d")["waypoints"])
    ed.selected_nodes = {"box_a", "box_c"}
    ed.start_drag_node("box_a")
    assert ed._avoid_session is not None
    ed.drag_node_to(100.0, 290.0)      # box_a к оси box_c
    e1 = _edge(ed, "box_a", "box_b")   # boundary live: сессия роутит
    _assert_ortho(_full_pts(e1))
    ed.end_drag_node()
    # waypoints оператора у edge_2 байт-в-байт: ребро не routable —
    # boundary-кадр пересаживает только ближний конец, полилинию не трогает
    assert _edge(ed, "box_c", "box_d")["waypoints"] == e2_wps_before


def test_session_failure_mid_gesture_degrades(qapp, tmp_path, monkeypatch):
    """Сбой кадра сессии не роняет жест: лестница доводит, сессия
    закрывается, флагов-сирот нет."""
    ed = _editor(qapp, tmp_path, _graph())
    ed.start_drag_node("box_a")
    assert ed._avoid_session is not None

    def boom(*a, **k):
        raise RuntimeError("имитация сбоя роутера")

    monkeypatch.setattr(ed._avoid_session, "route_frame", boom)
    ed.drag_node_to(100.0, 290.0)
    assert ed._avoid_session is None, "после сбоя сессия закрыта"
    e = _edge(ed, "box_a", "box_b")
    _assert_ortho(_full_pts(e))
    ed.end_drag_node()


def test_no_orphan_auto_route_on_double_refusal(qapp, tmp_path, monkeypatch):
    """Репро скептика ревью: кадр 1 — сессия приняла колено; кадр 2 —
    приёмка бракует (ends_ok=False), лестница гасит в прямую. Флаг
    _auto_route НЕ имеет права пережить маршрут (паритет :2155-2163)."""
    ed = _editor(qapp, tmp_path, _graph())
    ed.start_drag_node("box_a")
    ed.drag_node_to(100.0, 290.0)                  # колено от сессии
    e = _edge(ed, "box_a", "box_b")
    assert e.get("_auto_route") and e.get("waypoints")
    real = ed._avoid_session.route_frame

    def reject(nodes, edges_data):
        out = real(nodes, edges_data)
        for r in out.values():
            r["ends_ok"] = False               # имитация fallback libavoid
        return out

    monkeypatch.setattr(ed._avoid_session, "route_frame", reject)
    ed.drag_node_to(100.0, 200.0)                  # назад к соосности
    assert not e.get("waypoints"), "соосная пара обязана погаснуть"
    assert not e.get("_auto_route"), "сирота _auto_route на прямом ребре"
    ed.end_drag_node()


def test_mode_exit_finishes_gesture(qapp, tmp_path):
    """Esc/смена режима посреди протяжки в режиме drag_node: жест
    завершается штатно — сессия закрыта, dragging_node снят (undo-снапшот
    отправлен). Репро скептика: сессия переживала жест."""
    ed = _editor(qapp, tmp_path, _graph())
    ed.set_mode("drag_node")
    ed.start_drag_node("box_a")
    ed.drag_node_to(100.0, 290.0)
    assert ed._avoid_session is not None
    ed.set_mode("idle")                            # путь Esc (_handle_escape)
    assert ed.dragging_node is None, "выход из режима обязан завершить жест"
    assert ed._avoid_session is None, "сессия обязана закрыться"
    assert ed.undo_mgr.can_undo, "жест обязан доехать до undo-снапшота"
