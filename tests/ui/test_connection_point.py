"""T3 (П2) — ортогональная посадка конца трубы на bbox-узле.

Э1 (2026-07-31): get_connection_point переведён на канон посадки
`modules/graph/core/seating.node_anchor` — ожидания обновлены под канон:
  • партнёр в створе грани → конец на его проекции (как и раньше, 2dda92a);
  • партнёр ВНЕ створа → проекция КЛАМПИТСЯ в грань (угол бокса), а не
    прежняя середина стороны;
  • конец у коннектора — жёстко центроид (ободок виртуального bbox
    r=CONNECTOR_MARKER_RADIUS из посадки убран);
  • скин (FIXED_SIZES) приоритетнее контура.

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


def test_t3_partner_out_of_span_clamps_to_face(qapp, tmp_path):
    """Партнёр справа, но ВЫШЕ бокса — ортогональной трубы нет.

    Э1: канон (node_anchor) клампит проекцию партнёра в грань (угол бокса),
    а не сажает на прежнюю середину стороны."""
    ed = _editor(qapp, tmp_path, _graph([50.0, 500.0]))
    assert ed.get_connection_point("box", 500.0, 50.0) == (250.0, 150.0)


def test_t3_vertical_span_works_the_same_way(qapp, tmp_path):
    """Ось перепутать нельзя: снизу и в створе → проекция по x."""
    ed = _editor(qapp, tmp_path, _graph([380.0, 170.0]))
    assert ed.get_connection_point("box", 170.0, 380.0) == (170.0, 250.0)
    # снизу (dy доминирует), но левее бокса — створа нет: Э1 канон клампит
    # проекцию в нижнюю грань (угол), не середина
    assert ed.get_connection_point("box", 100.0, 500.0) == (150.0, 250.0)


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
    # цель (conn) — Э1: канон, конец коннектора жёстко в центроиде
    # (раньше — ободок виртуального бокса r=CONNECTOR_MARKER_RADIUS)
    assert edge["target_point"] == [180.0, 500.0]


def test_t3_add_edge_out_of_span(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph([50.0, 500.0]))
    assert ed.add_edge("box", "conn")
    edge = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))
    # Э1: канон клампит проекцию в правую грань (угол), не середина
    assert edge["source_point"] == [150.0, 250.0]


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
    # обратный конец — Э1: канон, центроид коннектора (без ободка r)
    assert edge["target_point"] == [180.0, 500.0]


def test_t3_resize_out_of_span_clamps_to_face(qapp, tmp_path):
    """Ужать бокс так, чтобы партнёр вышел из створа.

    Э1: канон клампит проекцию партнёра (y=180) в грань — угол y=200,
    не прежняя середина правой грани."""
    ed = _editor(qapp, tmp_path, _graph([180.0, 500.0]))
    assert ed.add_edge("box", "conn")
    ed._on_node_resized("box", [150.0, 200.0, 250.0, 300.0])   # створ 200..300
    edge = ed.model.find_edge_data(ed.model.edge_key("box", "conn"))
    assert edge["source_point"] == [200.0, 250.0]


def test_t3_polygon_node_uses_polygon_path(qapp, tmp_path):
    """У узла с контуром работает полигонная ветка, а не bbox-ветка.

    Э1: у канона скин ПРИОРИТЕТНЕЕ контура, поэтому полигонную ветку
    проверяем на узле без скина (class_name вне FIXED_SIZES)."""
    g = _graph([200.0, 500.0])
    g["nodes"][0]["class_name"] = "unknow"
    g["nodes"][0]["segmentation"] = [150.0, 150.0, 250.0, 150.0,
                                     250.0, 250.0, 150.0, 250.0]
    ed = _editor(qapp, tmp_path, g)
    x, y = ed.get_connection_point("box", 500.0, 200.0)
    assert x == pytest.approx(250.0)
    assert y == pytest.approx(200.0)
