# -*- coding: utf-8 -*-
"""Тесты судьи геометрии «Ручной правки» (Э0 пересборки).

Синтетика без данных заказчика. Координаты в комментариях — (x, y);
в данных точки [y, x], bbox [x1, y1, x2, y2], segmentation [x, y, ...].
"""
import subprocess
import sys

from modules.graph.core import edit_checks as ec


def _box(nid, x1, y1, x2, y2, cls="block"):
    return {"id": nid, "type": "equipment", "class_name": cls,
            "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
            "bbox": [x1, y1, x2, y2]}


def _conn(nid, x, y):
    return {"id": nid, "type": "connector", "centroid": [y, x], "bbox": None}


def _edge(eid, s, t, sp_xy, tp_xy, wps_xy=()):
    return {"id": eid, "source": s, "target": t,
            "source_point": [sp_xy[1], sp_xy[0]],
            "target_point": [tp_xy[1], tp_xy[0]],
            "waypoints": [[y, x] for x, y in wps_xy]}


def _graph(nodes, links):
    return {"nodes": nodes, "links": links}


# ------------------------------------------------------------------- углы

def test_corner_detects_exact_vertex():
    g = _graph([_box("a", 0, 0, 40, 40), _conn("b", 200, 200)],
               [_edge("e1", "a", "b", (40, 40), (200, 200))])
    hits = ec.corner_ends(g)
    assert len(hits) == 1
    assert hits[0]["edge"] == "e1" and hits[0]["node"] == "a"


def test_corner_face_middle_is_clean():
    g = _graph([_box("a", 0, 0, 40, 40), _conn("b", 200, 20)],
               [_edge("e1", "a", "b", (40, 20), (200, 20))])
    assert ec.corner_ends(g) == []
    assert ec.corner_ends(g, tol=ec.CORNER_NEAR) == []


# ------------------------------------------------- диагонали и почти-оси

def test_diag_and_near_measured_in_px():
    g = _graph([_conn("a", 0, 0), _conn("b", 100, 50)],
               [_edge("e1", "a", "b", (0, 0), (100, 50)),      # dev 50 -> diag
                _edge("e2", "a", "b", (0, 0), (100, 1.0)),     # dev 1.0 -> near
                _edge("e3", "a", "b", (0, 0), (4, 3))])        # короче floor
    r = ec.diag_segments(g)
    assert [d["edge"] for d in r["diag"]] == ["e1"]
    assert [d["edge"] for d in r["near"]] == ["e2"]
    assert r["max_dev"] == 50.0


# ------------------------------------------------------- вдоль границы

def test_along_own_seated_face_zero_gap():
    # конец на правой грани, стаб бежит ВДОЛЬ неё — дефект «вдоль своей»
    g = _graph([_box("a", 0, 0, 40, 40), _conn("b", 40, 80)],
               [_edge("e1", "a", "b", (40, 20), (40, 80), [(40, 35)])])
    hits = ec.along_border(g)
    assert len(hits) == 1
    assert hits[0]["own"] is True and hits[0]["gap"] == 0.0


def test_along_exempts_perpendicular_stub():
    # перпендикулярный выход из порта легален (отходит в пределах EXIT_RUN)
    g = _graph([_box("a", 0, 0, 40, 40), _conn("b", 100, 20)],
               [_edge("e1", "a", "b", (40, 20), (100, 20))])
    assert ec.along_border(g) == []


def test_along_corner_exit_hug_is_caught():
    # определение заказчика: вышла из УГЛА и не отошла — бежит вдоль
    # своего же бокса (тут — вниз вдоль правой грани)
    g = _graph([_box("a", 0, 0, 40, 40), _conn("b", 80, 30)],
               [_edge("e1", "a", "b", (40, 0), (80, 30), [(40, 30)])])
    hits = ec.along_border(g)
    assert len(hits) == 1
    assert hits[0]["own"] is True and hits[0]["gap"] == 0.0
    assert hits[0]["overlap"] >= 28


def test_along_foreign_face():
    # средний сегмент бежит в 3px от чужой грани
    g = _graph([_conn("a", 0, 100), _conn("b", 200, 100),
                _box("c", 50, 50, 150, 97)],
               [_edge("e1", "a", "b", (0, 100), (200, 100))])
    hits = ec.along_border(g)
    assert len(hits) == 1
    assert hits[0]["node"] == "c" and hits[0]["own"] is False
    assert abs(hits[0]["gap"] - 3.0) < 0.01


def test_along_contour_face_end_vertex_is_clean():
    # случай edge_170/node_28: конец в ВЕРШИНЕ контурной грани, сегмент
    # уходит прочь — по реальному контуру прилегания нет (bbox бы налгал)
    seg = [0, 0, 40, 0, 40, 20, 25, 40, 0, 40]   # V-грань x=40 на y[0,20]
    node = {"id": "p", "type": "equipment", "class_name": "unknow",
            "centroid": [20, 20], "bbox": [0, 0, 40, 40],
            "segmentation": seg}
    g = _graph([node, _conn("b", 40, 100)],
               [_edge("e1", "p", "b", (40, 20), (40, 100))])
    assert ec.along_border(g) == []


def test_along_contour_face_hug_is_caught():
    # а честный пробег ВДОЛЬ контурной грани ловится (ngle: edge_107)
    seg = [0, 0, 40, 0, 40, 60, 0, 60]
    node = {"id": "p", "type": "equipment", "class_name": "unknow",
            "centroid": [30, 20], "bbox": [0, 0, 40, 60],
            "segmentation": seg}
    g = _graph([node, _conn("a", 40, 5), _conn("b", 40, 100)],
               [_edge("e1", "a", "b", (40, 5), (40, 100))])
    hits = ec.along_border(g)
    assert len(hits) == 1 and hits[0]["gap"] == 0.0


# ----------------------------------------------------- сквозь чужой узел

def _u_node():
    # квадрат 60x60 с карманом x[20,40] y[20,60] (открыт снизу)
    seg = [0, 0, 60, 0, 60, 60, 40, 60, 40, 20, 20, 20, 20, 60, 0, 60]
    return {"id": "u", "type": "equipment", "class_name": "unknow",
            "centroid": [30, 30], "bbox": [0, 0, 60, 60],
            "segmentation": seg}


def test_through_pocket_of_contour_is_legal():
    # сегмент целиком в кармане: bbox накрывает, контур — нет (node_28)
    g = _graph([_u_node(), _conn("a", 30, 25), _conn("b", 30, 55)],
               [_edge("e1", "a", "b", (30, 25), (30, 55))])
    assert ec.through_box(g) == []


def test_through_contour_arm_is_caught():
    g = _graph([_u_node(), _conn("a", -10, 40), _conn("b", 70, 40)],
               [_edge("e1", "a", "b", (-10, 40), (70, 40))])
    hits = ec.through_box(g)
    assert len(hits) == 1 and hits[0]["node"] == "u"


def test_through_rect_touch_is_legal():
    g = _graph([_box("c", 0, 0, 40, 40), _conn("a", -10, 40), _conn("b", 50, 40)],
               [_edge("e1", "a", "b", (-10, 40), (50, 40)),     # касание грани
                _edge("e2", "a", "b", (-10, 20), (50, 20))])    # сквозь нутро
    hits = ec.through_box(g)
    assert [h["edge"] for h in hits] == ["e2"]


# ------------------------------------------------- классификация концов

def test_end_classes_connector_midpoint_side_adrift():
    g = _graph([_box("a", 0, 0, 40, 40), _conn("c", 10, 100)],
               [_edge("e1", "a", "c", (40, 20), (10, 100)),     # midpoint
                _edge("e2", "a", "c", (40, 30), (10.2, 100)),   # side, conn ok
                _edge("e3", "a", "c", (25, 25), (12, 103))])    # adrift, conn off
    r = ec.end_classes(g)
    assert [d["edge"] for d in r["midpoint"]] == ["e1"]
    assert [d["edge"] for d in r["side"]] == ["e2"]
    assert [d["edge"] for d in r["adrift"]] == ["e3"]
    assert len(r["conn_ok"]) == 2
    assert [d["edge"] for d in r["conn_off"]] == ["e3"]


# --------------------------------------------------------------- чистота

def test_engine_pure_stdlib():
    # судью импортирует UI (requirements/ui.txt без shapely/numpy) и сервер
    code = ("import sys; import modules.graph.core.edit_checks; "
            "bad = [m for m in ('shapely', 'numpy', 'PySide6') "
            "if m in sys.modules]; "
            "sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ec.__file__)
                       .replace("modules\\graph\\core\\edit_checks.py", "")
                       .replace("modules/graph/core/edit_checks.py", ""))
    assert r.returncode == 0
