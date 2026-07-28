"""T3 (П2) — ортогональная посадка конца трубы на bbox-узле.

Фиксирует поведение коммита 2dda92a: если партнёр стоит В СТВОРЕ выбранной
стороны, конец садится на его проекцию (труба идёт строго по оси); вне створа
ортогональной трубы не существует — там прежняя середина стороны. До этого
середина давала ВЕЕР: все рёбра, входящие с одной стороны, получали одну точку.

Оговорка: 2dda92a в собственном теле признаёт, что делался под неподтверждённый
диагноз и проверен только синтетикой — T3 фиксирует именно синтетическое
поведение, это осознанно.

Оба пути SimpleGraphEditor покрыты, они асимметричны:
  • add_edge — цепочка (точка на цели считается от центроида источника, потом
    точка на источнике — от УЖЕ ПОСЧИТАННОЙ точки на цели);
  • _on_node_resized — оба конца считаются от центроидов (без цепочки).
Drag узлов в Simple не существует (заглушки в Base, реализация только в
Advanced) — «перетаскивание» тут ни при чём.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 600, 400

# Бокс 100x100 с центром (200, 200); партнёры — коннекторы.
BOX = [150.0, 150.0, 250.0, 250.0]


def _graph(partner_yx) -> dict:
    """Бокс + коннектор в заданной точке [y, x], ребра ещё нет."""
    nodes = [
        {"id": "box", "type": "equipment", "centroid": [200.0, 200.0],
         "bbox": list(BOX), "segmentation": None,
         "class_id": 1, "class_name": "nasos", "degree": 0},
        {"id": "conn", "type": "connector", "centroid": list(partner_yx),
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 0},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": [], "text_blocks": [], "bindings": []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _editor(qapp, tmp_path, graph):
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = SimpleGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


# ── сама функция ─────────────────────────────────────────────────────────

def test_t3_partner_in_span_gives_orthogonal_projection(qapp, tmp_path):
    """Партнёр справа и в створе правой грани → конец на его высоте."""
    ed = _editor(qapp, tmp_path, _graph([180.0, 500.0]))
    assert ed.get_connection_point("box", 500.0, 180.0) == (250.0, 180.0)


def test_t3_partner_out_of_span_falls_back_to_midpoint(qapp, tmp_path):
    """Партнёр справа, но ВЫШЕ бокса — ортогональной трубы нет, середина грани."""
    ed = _editor(qapp, tmp_path, _graph([50.0, 500.0]))
    assert ed.get_connection_point("box", 500.0, 50.0) == (250.0, 200.0)


def test_t3_vertical_span_works_the_same_way(qapp, tmp_path):
    """Ось перепутать нельзя: снизу и в створе → проекция по x."""
    ed = _editor(qapp, tmp_path, _graph([380.0, 170.0]))
    assert ed.get_connection_point("box", 170.0, 380.0) == (170.0, 250.0)
    # снизу (dy доминирует), но левее бокса — створа нет, середина нижней грани
    assert ed.get_connection_point("box", 100.0, 500.0) == (200.0, 250.0)


def test_t3_fan_is_gone(qapp, tmp_path):
    """Три партнёра с одной стороны в створе — три РАЗНЫЕ точки (был веер)."""
    ed = _editor(qapp, tmp_path, _graph([180.0, 500.0]))
    pts = {ed.get_connection_point("box", 500.0, y) for y in (170.0, 200.0, 230.0)}
    assert len(pts) == 3


# ── путь 1: add_edge (цепочка) ───────────────────────────────────────────

def test_t3_add_edge_chain(qapp, tmp_path):
    """add_edge: точка на цели — от центроида источника, точка на источнике —
    от уже посчитанной точки на цели."""
    ed = _editor(qapp, tmp_path, _graph([180.0, 500.0]))
    assert ed.add_edge("box", "conn")
    edge = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))

    # источник (box): партнёр-точка на коннекторе в створе правой грани
    assert edge["source_point"] == [180.0, 250.0]     # [y, x]
    # цель (conn) — виртуальный бокс радиуса CONNECTOR_MARKER_RADIUS
    r = ed.CONNECTOR_MARKER_RADIUS
    assert edge["target_point"] == [180.0, 500.0 - r]


def test_t3_add_edge_out_of_span(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph([50.0, 500.0]))
    assert ed.add_edge("box", "conn")
    edge = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))
    assert edge["source_point"] == [200.0, 250.0]     # середина правой грани


# ── путь 2: _on_node_resized (оба конца по центроидам) ───────────────────

def test_t3_resize_recomputes_both_ends_from_centroids(qapp, tmp_path):
    """_on_node_resized — колбэк ресайза за угловые ручки: оба конца считаются
    от ЦЕНТРОИДОВ, без цепочки (в отличие от add_edge)."""
    ed = _editor(qapp, tmp_path, _graph([180.0, 500.0]))
    assert ed.add_edge("box", "conn")

    # растянуть бокс вниз: створ правой грани станет 150..300
    ed._on_node_resized("box", [150.0, 150.0, 250.0, 300.0])
    edge = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))

    # партнёр (центроид коннектора, y=180) по-прежнему в створе → проекция
    assert edge["source_point"] == [180.0, 250.0]
    r = ed.CONNECTOR_MARKER_RADIUS
    # обратный конец считается от ЦЕНТРОИДА бокса, а он уехал на y=225
    assert edge["target_point"] == [180.0, 500.0 - r]


def test_t3_resize_out_of_span_gives_midpoint(qapp, tmp_path):
    """Ужать бокс так, чтобы партнёр вышел из створа → середина грани."""
    ed = _editor(qapp, tmp_path, _graph([180.0, 500.0]))
    assert ed.add_edge("box", "conn")
    ed._on_node_resized("box", [150.0, 200.0, 250.0, 300.0])   # створ 200..300
    edge = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))
    assert edge["source_point"] == [250.0, 250.0]              # середина правой


def test_t3_polygon_node_uses_polygon_path(qapp, tmp_path):
    """У узла с контуром работает полигонная ветка, а не bbox-ветка."""
    g = _graph([200.0, 500.0])
    g["nodes"][0]["segmentation"] = [150.0, 150.0, 250.0, 150.0,
                                     250.0, 250.0, 150.0, 250.0]
    ed = _editor(qapp, tmp_path, g)
    x, y = ed.get_connection_point("box", 500.0, 200.0)
    assert x == pytest.approx(250.0)
    assert y == pytest.approx(200.0)
