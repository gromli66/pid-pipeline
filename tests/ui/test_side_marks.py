"""T4 — подсветка стороны блока, где есть подключение (П8).

Формулировка контракта: участков РОВНО столько, сколько разных точек входа
труб (совпавшие схлопнуты) — не «сторон». Два ребра, вошедшие в одну сторону в
разных точках, дают два участка.

Плюс: участок полигонного узла лежит на контуре, а у скинового узла в холсте —
на bbox (контур там не рисуется, _draws_polygon), иначе штрих встал бы на
невидимую фигуру.
"""
import json
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 400, 300


# ── чистая геометрия участка ─────────────────────────────────────────────

def _len(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def test_mark_is_centred_on_projection_and_has_requested_length():
    from ui.editors.graph_geometry import boundary_mark_points

    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    pts = boundary_mark_points(square, 50.0, 0.0, 20.0, clip_to_face=True)
    assert pts[0] == (40.0, 0.0)
    assert pts[-1] == (60.0, 0.0)
    assert _len(pts) == pytest.approx(20.0)


def test_mark_on_small_box_is_clipped_to_face():
    """У мелкого бокса половина длины больше грани — штрих не заворачивает
    за угол, а обрезается по грани."""
    from ui.editors.graph_geometry import boundary_mark_points

    box = [(0.0, 0.0), (10.0, 0.0), (10.0, 40.0), (0.0, 40.0)]
    pts = boundary_mark_points(box, 5.0, 0.0, 40.0, clip_to_face=True)
    assert pts[0] == (0.0, 0.0) and pts[-1] == (10.0, 0.0)
    assert all(y == 0.0 for _x, y in pts), "штрих ушёл с верхней грани"


def test_mark_on_polygon_follows_contour_across_vertices():
    """На контуре штрих идёт ВДОЛЬ границы через вершины (не по хорде)."""
    from ui.editors.graph_geometry import boundary_mark_points

    tri = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0)]
    pts = boundary_mark_points(tri, 20.0, 0.0, 20.0, clip_to_face=False)
    assert (20.0, 0.0) in pts, "вершина должна попасть в полилинию участка"
    assert _len(pts) == pytest.approx(20.0)
    assert len(pts) >= 3


def test_mark_never_longer_than_boundary():
    from ui.editors.graph_geometry import boundary_mark_points

    box = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    pts = boundary_mark_points(box, 2.0, 0.0, 1000.0, clip_to_face=False)
    assert _len(pts) <= 16.0 + 1e-6


# ── участки в редакторе ──────────────────────────────────────────────────

def _graph(seg_for_eq=None) -> dict:
    """Бокс с ДВУМЯ подводками в одну сторону в разных точках + полигон."""
    nodes = [
        {"id": "eq_box", "type": "equipment", "centroid": [100.0, 100.0],
         "bbox": [60.0, 60.0, 140.0, 140.0], "segmentation": seg_for_eq,
         "class_id": 1, "class_name": "nasos", "degree": 2},
        {"id": "conn_a", "type": "connector", "centroid": [40.0, 260.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
        {"id": "conn_b", "type": "connector", "centroid": [120.0, 260.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [
        # обе трубы входят СПРАВА (x = 140), но в разные точки по y
        {"id": "edge_1", "source": "eq_box", "target": "conn_a",
         "source_point": [80.0, 140.0], "target_point": [40.0, 252.0],
         "waypoints": []},
        {"id": "edge_2", "source": "eq_box", "target": "conn_b",
         "source_point": [120.0, 140.0], "target_point": [120.0, 252.0],
         "waypoints": []},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links,
            "text_blocks": [], "bindings": []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _editor(qapp, tmp_path, graph: dict, cls=None):
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

    ed = (cls or SimpleGraphEditor)()
    assert ed.load_data(str(img_path), str(graph_path))
    return ed


def test_t4_two_edges_one_side_give_two_marks(qapp, tmp_path):
    """Два ребра в ОДНУ сторону в разных точках = два участка."""
    ed = _editor(qapp, tmp_path, _graph())
    assert len(ed.side_items.get("eq_box", [])) == 2


def test_t4_coincident_entry_points_collapse(qapp, tmp_path):
    """Совпавшие точки входа схлопываются в один участок."""
    g = _graph()
    g["links"][1]["source_point"] = list(g["links"][0]["source_point"])
    ed = _editor(qapp, tmp_path, g)
    assert len(ed.side_items.get("eq_box", [])) == 1


def test_t4_connectors_have_no_marks(qapp, tmp_path):
    """Подсвечиваем БЛОКИ; у коннектора нарисованной формы нет."""
    ed = _editor(qapp, tmp_path, _graph())
    assert "conn_a" not in ed.side_items
    assert "conn_b" not in ed.side_items


def test_t4_mark_lies_on_polygon_contour(qapp, tmp_path):
    """У полигонного узла участок лежит на контуре, а не на bbox."""
    seg = [60.0, 60.0, 140.0, 60.0, 140.0, 140.0, 100.0, 170.0, 60.0, 140.0]
    ed = _editor(qapp, tmp_path, _graph(seg_for_eq=seg))
    items = ed.side_items.get("eq_box", [])
    assert items
    # обе точки входа сидят на x=140 — правая грань контура тоже x=140
    for it in items:
        poly = it.path().toFillPolygon()
        assert all(abs(p.x() - 140.0) < 1e-6 for p in poly), "участок ушёл с контура"


def test_t4_marks_follow_edge_add_and_remove(qapp, tmp_path):
    """Участки живут по хуку в create/remove edge item, а не по разовой отрисовке."""
    ed = _editor(qapp, tmp_path, _graph())
    assert len(ed.side_items.get("eq_box", [])) == 2

    key = ed.model.edge_key("eq_box", "conn_b")
    ed.model.remove_edge(key)
    ed.remove_edge_item(key)
    assert len(ed.side_items.get("eq_box", [])) == 1


def test_t4_toggle_hides_and_restores_marks(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _graph())
    ed.set_side_marks_visible(False)
    assert not ed.side_items
    ed.set_side_marks_visible(True)
    assert len(ed.side_items.get("eq_box", [])) == 2


def test_t4_mark_len_is_mode_dependent_and_present_in_canvas_map():
    """SIDE_MARK_LEN обязан быть и в _VIS_KEYS, и в _VIS_CANVAS."""
    from ui.editors.base_graph_editor import BaseGraphEditor

    assert "SIDE_MARK_LEN" in BaseGraphEditor._VIS_KEYS
    assert BaseGraphEditor._VIS_CANVAS["SIDE_MARK_LEN"] < BaseGraphEditor.SIDE_MARK_LEN


def test_t4_zvalue_between_frame_and_marker(qapp, tmp_path):
    """Участок поверх рамки узла (2), но под маркером-центроидом (3)."""
    ed = _editor(qapp, tmp_path, _graph())
    for it in ed.side_items["eq_box"]:
        assert 2 < it.zValue() < 3


def test_t4_skin_node_marks_on_bbox_not_contour(qapp, tmp_path):
    """В холсте у скинового узла контур не рисуется — участок обязан лежать
    на bbox, иначе штрих встанет на невидимую фигуру."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    seg = [60.0, 60.0, 140.0, 60.0, 140.0, 140.0, 100.0, 170.0, 60.0, 140.0]
    ed = _editor(qapp, tmp_path, _graph(seg_for_eq=seg), cls=AdvancedGraphEditor)
    outline_poly, clip_poly = ed._node_outline_points("eq_box")
    assert clip_poly is False and len(outline_poly) == 5

    ed._canvas_mode = True
    ed.show_skins = True
    assert ed._node_has_skin(ed.nodes["eq_box"]), "тестовый узел должен быть скиновым"
    outline_box, clip_box = ed._node_outline_points("eq_box")
    assert clip_box is True, "у скинового узла форма = bbox"
    assert outline_box == [(60.0, 60.0), (140.0, 60.0),
                           (140.0, 140.0), (60.0, 140.0)]
