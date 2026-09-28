# -*- coding: utf-8 -*-
"""Клиент против модели точек прикрепления (решение заказчика 2026-09-22).

Холст, собранный сервером, открывается в «Ручной правке» НЕПОДВИЖНО; жест
оператора держит трубу в выверенном порту клапана; скин рисуется той же
ориентацией, что уйдёт в FXML (иначе нарисованная талия и посадка
разъезжаются — до правки на шести классах привод был нарисован справа, а в
САПФИР он слева).

Координаты двойственны (CODING_GUIDE §6): centroid / source_point /
target_point = [y, x]; bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage, QTransform     # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from modules.graph.core import contact, seating          # noqa: E402

IMG_W, IMG_H = 1920, 1080
# armatura_electro по оси H: бокс 42x37.8, талия ниже центра рамки на 8.3916
WAIST_Y = 200.0
BOX_CY = WAIST_Y - 0.222 * 37.8
VALVE_BBOX = [279.0, BOX_CY - 18.9, 321.0, BOX_CY + 18.9]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _canvas():
    """Кусок серверного холста: коннектор — клапан — бокс на линии талии."""
    nodes = [
        {"id": "c", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [WAIST_Y, 200.0],
         "bbox": None, "segmentation": None, "degree": 1},
        {"id": "v", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [WAIST_Y, 300.0], "bbox": list(VALVE_BBOX),
         "segmentation": None, "degree": 2},
        {"id": "b", "type": "equipment", "class_id": 99,
         "class_name": "unknow", "centroid": [WAIST_Y, 400.0],
         "bbox": [380.0, WAIST_Y - 20.0, 420.0, WAIST_Y + 20.0],
         "segmentation": None, "degree": 1},
    ]
    links = [
        {"id": "e1", "source": "c", "target": "v",
         "source_point": [WAIST_Y, 200.0], "target_point": [WAIST_Y, 279.0],
         "waypoints": []},
        {"id": "e2", "source": "v", "target": "b",
         "source_point": [WAIST_Y, 321.0], "target_point": [WAIST_Y, 380.0],
         "waypoints": []},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _valve_port(node):
    rect = seating._anchor_rect(node)
    return contact.face_refs(node, rect)["L"]


def test_canvas_fixture_is_what_the_server_builds():
    """Guard: вход — свежий выход серверной посадки (пересадка ничего не
    двигает), иначе тесты ниже проверяли бы не то."""
    g = copy.deepcopy(_canvas())
    seating.reseat_all_endpoints(g, model=True, slots=True)
    assert g["links"] == _canvas()["links"]
    assert _valve_port(g["nodes"][1]) == pytest.approx(WAIST_Y)


def test_open_does_not_move_a_canvas_built_by_the_server(tmp_path):
    """«Открыл холст, ничего не тронул» — ни один конец не сдвинулся, файл
    не переписан (посадка при открытии — та же модель, что у сервера)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(_canvas()), encoding="utf-8")

    assert not _reseat_canvas_endpoints(p), "холст переписан при открытии"
    assert json.loads(p.read_text(encoding="utf-8"))["links"] \
        == _canvas()["links"]


def test_open_keeps_slots_of_several_pipes_on_one_face(tmp_path):
    """Три трубы в одну грань (репро 51b339ab: стрелка направления и
    арматура) стоят слотами вокруг опорной точки — открытие их НЕ сводит
    в опорную точку одну на другую."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = _canvas()
    g["links"] = [
        {"id": f"m{i}", "source": "b", "target": "v",
         "source_point": [WAIST_Y, 380.0], "target_point": [WAIST_Y, 321.0],
         "waypoints": []} for i in range(3)]
    g["nodes"][1]["degree"] = 3
    g["nodes"][2]["degree"] = 3
    seating.reseat_all_endpoints(g, model=True, slots=True)

    ys = sorted(e["target_point"][0] for e in g["links"])
    assert len(set(ys)) == 3, f"слоты не развели трубы: {ys}"
    port = _valve_port(g["nodes"][1])
    assert sum(ys) / 3 == pytest.approx(port, abs=0.01), \
        "слоты обязаны стоять симметрично вокруг выверенного порта"

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    _reseat_canvas_endpoints(p)
    got = sorted(e["target_point"][0]
                 for e in json.loads(p.read_text(encoding="utf-8"))["links"])
    assert got == pytest.approx(ys), "открытие схлопнуло слоты"


def _editor(qapp, tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = AdvancedGraphEditor()
    ed._canvas_mode = True          # так делает вкладка: флаг ДО load_data
    assert ed.load_data(str(ip), str(gp))
    return ed


def test_drag_valve_keeps_both_pipes_in_the_waist(qapp, tmp_path):
    """Оператор тянет клапан — обе трубы остаются в выверенном порту его
    новой рамки (а не в середине грани)."""
    ed = _editor(qapp, tmp_path, _canvas())
    ed.start_drag_node("v")
    ed.drag_node_to(330.0, 240.0)
    ed.end_drag_node()

    v = ed.nodes["v"]
    port = _valve_port(v)
    rect = seating._anchor_rect(v)
    by_id = {e["id"]: e for e in ed.edges_data}
    assert by_id["e1"]["target_point"][0] == pytest.approx(port, abs=0.5)
    assert by_id["e1"]["target_point"][1] == pytest.approx(rect[0], abs=0.5)
    assert by_id["e2"]["source_point"][0] == pytest.approx(port, abs=0.5)
    assert by_id["e2"]["source_point"][1] == pytest.approx(rect[2], abs=0.5)
    # талия ниже центра рамки — конец НЕ в середине грани
    assert port - (rect[1] + rect[3]) / 2 == pytest.approx(0.222 * 37.8)


def test_skin_orientation_and_reverse_match_the_export(qapp, tmp_path):
    """Ориентация скина — та же, что эмитит выгрузка; вертикальная арматура
    рисуется развёрнутой (привод слева, как в САПФИР)."""
    g = _canvas()
    g["nodes"][1]["_axis"] = "V"
    ed = _editor(qapp, tmp_path, g)

    assert ed._node_orientation("v") == "VERTICAL_REVERSE"
    base = ed._skin_pixmap_for("armatura_electro")
    assert base is not None and not base.isNull()
    rev = ed._oriented_skin_pixmap(base, "VERTICAL_REVERSE")
    assert rev.toImage() == base.transformed(QTransform().rotate(180)).toImage()
    assert ed._oriented_skin_pixmap(base, "VERTICAL").toImage() \
        == base.toImage(), "VERTICAL — база как есть"


def test_horizontal_valve_skin_is_rotated_not_reversed(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _canvas())
    assert ed._node_orientation("v") == "HORIZONTAL"
    base = ed._skin_pixmap_for("armatura_electro")
    got = ed._oriented_skin_pixmap(base, "HORIZONTAL")
    assert got.toImage() == base.transformed(QTransform().rotate(-90)).toImage()


def _old_canvas_42x38():
    """Старый холст: armatura_ruchn в рамке 42x38 без _axis (сборка до
    FIXED_SIZES по аспекту) и коннектор под ним. Конец сохранён старым
    каноном — на letterbox-следе графики y=260.5 (поля по 8.5 px)."""
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": [
                {"id": "s", "type": "equipment", "class_id": 1,
                 "class_name": "armatura_ruchn", "centroid": [250.0, 821.0],
                 "bbox": [800.0, 231.0, 842.0, 269.0],
                 "segmentation": None, "degree": 1},
                {"id": "p", "type": "connector", "class_id": -1,
                 "class_name": "connector", "centroid": [420.0, 821.0],
                 "bbox": None, "segmentation": None, "degree": 1}],
            "links": [{"id": "e", "source": "s", "target": "p",
                       "source_point": [260.5, 821.0],
                       "target_point": [420.0, 821.0], "waypoints": []}],
            "text_blocks": [], "bindings": []}


def test_server_reseat_agrees_with_editor_on_old_skin_frame(qapp, tmp_path):
    """Д1, Р2: сторож == судья на старой рамке скина ПОСЛЕ жестов оператора.

    Открытие -> оператор тянет коннектор-партнёр в сторону -> «Оптимизировать
    ребро». Редактор сажает конец скина на нижнюю грань РАМКИ y=269; серверная
    посадка того же состояния — туда же. До Р2 сервер уводил его на letterbox
    y=260.5: расхождение 8.5 px, и следующая пересборка тащила трубу внутрь
    рамки."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(_old_canvas_42x38()), encoding="utf-8")
    _reseat_canvas_endpoints(p)
    ed = _editor(qapp, tmp_path, json.loads(p.read_text(encoding="utf-8")))

    ed.start_drag_node("p")
    ed.drag_node_to(830.0, 470.0)
    ed.end_drag_node()
    assert ed.optimize_edge("s", "p")
    assert ed.edges_data[0]["source_point"] == [269.0, 821.0]    # [y, x]

    st = {"graph": {"image_size": [IMG_H, IMG_W]},
          "nodes": copy.deepcopy(list(ed.nodes.values())),
          "links": copy.deepcopy(ed.edges_data)}
    seating.reseat_all_endpoints(st, model=True, slots=True)
    assert st["links"][0]["source_point"] == [269.0, 821.0], \
        "серверная посадка увела конец с рамки (letterbox 260.5?)"
