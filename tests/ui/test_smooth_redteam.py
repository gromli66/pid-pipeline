# -*- coding: utf-8 -*-
"""Кнопка «Выровнять» в живом редакторе — находки red-team блока 6
(решение Максима 01.10: чинить все шесть).

Сценарии — живой `AdvancedGraphEditor` в режиме холста (`_canvas_mode` до
`load_data`, как ставит вкладка), перед кнопкой — чужое действие оператора
(протяжка узла и её отмена Ctrl+Z). Координаты двойственны (CODING_GUIDE §6):
centroid / точки = [y, x], bbox = [x1, y1, x2, y2].
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from modules.graph.core import contact               # noqa: E402
from modules.graph.core import edit_smooth as es     # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _canvas(nodes, links, model=True):
    meta = {"image_size": [1080, 1920]}
    if model:
        meta["canvas_transform"] = {"attach_model": contact.ATTACH_MODEL}
    return {"directed": False, "multigraph": False, "graph": meta,
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _editor(tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor
    img = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    ed = AdvancedGraphEditor()
    ed._canvas_mode = True
    assert ed.load_data(str(ip), str(gp))
    return ed


def _drag_and_undo(ed, nid, dx, dy):
    """Чужое действие оператора: протяжка узла и её отмена (Ctrl+Z)."""
    before = json.dumps(ed.model.graph_data, sort_keys=True)
    y, x = ed.nodes[nid]["centroid"]
    ed.set_mode("drag_node")
    ed.start_drag_node(nid)
    ed.drag_node_to(x + dx, y + dy)
    ed.end_drag_node()
    ed.set_mode("idle")
    ed.undo_mgr.undo()
    assert json.dumps(ed.model.graph_data, sort_keys=True) == before


def _valve(nid, bb, cls="armatura_electro", axis="H"):
    return {"id": nid, "type": "equipment", "class_name": cls, "class_id": 3,
            "centroid": [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2],
            "bbox": list(bb), "_axis": axis, "segmentation": None}


def _box(nid, x1, y1, x2, y2):
    return {"id": nid, "type": "equipment", "class_name": "unknow",
            "class_id": 99, "centroid": [(y1 + y2) / 2, (x1 + x2) / 2],
            "bbox": [x1, y1, x2, y2], "segmentation": None}


# ── (2) ход не уводит узел за лист ─────────────────────────────────────
def _edge_of_sheet(x1, right):
    """Репро red-team e7_offsheet в живом редакторе: H-клапан с рамкой от
    x1, труба из талии внутренней грани к боксу q в 3.4 px от грани —
    вылет меньше клиренса лестницы маршрута, «е» отводит клапан наружу,
    к краю листа, на 3.1 px."""
    bb = (x1, 280.0, x1 + 42.0, 318.0)
    v = _valve("v", bb)
    fx = bb[0] if right else bb[2]
    qx = fx - 3.4 if right else fx + 3.4
    w = contact.face_refs(v, bb)["L" if right else "R"]
    q = _box("q", qx - 20.0, 500.0, qx + 20.0, 540.0)
    e = {"id": "em", "source": "v", "target": "q", "source_point": [w, fx],
         "target_point": [500.0, qx], "waypoints": []}
    return _canvas([v, q], [e])


@pytest.mark.parametrize("right", [False, True], ids=["слева", "справа"])
def test_button_keeps_nodes_on_the_sheet(qapp, tmp_path, right):
    """Находка red-team (2): у края листа кнопка уводила клапан за лист (x1
    1 -> −2.1, x2 1919 -> 1922.1; гейт судил только габарит). Теперь узлы в
    листе — клапан на месте. В 5 px от края «е» жив: клапан отходит на
    3.1 px и остаётся в листе."""
    x1 = 1877.0 if right else 1.0
    ed = _editor(tmp_path, _edge_of_sheet(x1, right))
    _drag_and_undo(ed, "q", 30.0, 0.0)
    ed.smooth_canvas()
    g = ed.model.graph_data
    assert es.sheet_out(g) == 0.0
    assert ed.nodes["v"]["bbox"] == [x1, 280.0, x1 + 42.0, 318.0]

    d = tmp_path / "room"
    d.mkdir()
    room = x1 + (-5.0 if right else 5.0)
    ed = _editor(d, _edge_of_sheet(room, right))
    ed.smooth_canvas()
    bb = ed.nodes["v"]["bbox"]
    assert bb[0] == pytest.approx(room + (3.1 if right else -3.1))
    assert es.sheet_out(ed.model.graph_data) == 0.0


# ── (3) allow_node_shift=False: оборудование стоит на всех ступенях ────
def test_no_node_shift_keeps_the_valve(qapp, tmp_path):
    """Находка red-team (3): `smooth_canvas(allow_node_shift=False)` держала
    только сдвиг узла (ступень 4) — «е» отводил клапан. Холст модели, в 5 px
    от края листа: с флагом клапан бит-в-бит; без флага «е» отводит его на
    3.1 px."""
    bb = [5.0, 280.0, 47.0, 318.0]
    ed = _editor(tmp_path, _edge_of_sheet(5.0, False))
    _drag_and_undo(ed, "q", 30.0, 0.0)
    st = ed.smooth_canvas(allow_node_shift=False)
    assert ed.nodes["v"]["bbox"] == bb, st

    d = tmp_path / "free"
    d.mkdir()
    ed = _editor(d, _edge_of_sheet(5.0, False))
    ed.smooth_canvas()
    assert ed.nodes["v"]["bbox"][0] == pytest.approx(5.0 - 3.1)
