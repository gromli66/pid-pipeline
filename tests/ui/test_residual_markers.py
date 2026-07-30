# -*- coding: utf-8 -*-
"""Э12: слой маркеров очагов остатка в редакторе (ResidualLayerMixin).

Маркеры живут на Z=9, переживают _redraw_all (пересоздаются), позиция —
живая (по item'ам узлов); focus_residual центрирует вид на очаге.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

RESIDUAL_Z = 9.0


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph():
    return {
        "directed": False,
        "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [120.0, 120.0], "bbox": [100, 100, 140, 140],
             "degree": 1},
            {"id": "b", "type": "equipment", "class_name": "unknow",
             "centroid": [120.0, 162.0], "bbox": [142, 100, 182, 140],
             "degree": 1},
        ],
        "links": [
            {"id": "e1", "source": "a", "target": "b",
             "source_point": [120.0, 140.0], "target_point": [120.0, 142.0],
             "waypoints": []},
        ],
        "text_blocks": [],
        "bindings": [],
    }


def _residual():
    return {
        "canvas_sha": "x",
        "floor": 6.0,
        "invisible_edges": [{"edge_id": "e1", "nodes": ["a", "b"],
                             "gap": 2.0, "point": [141.0, 120.0]}],
        "box_on_magi": [],
        "overlaps": [],
        "total": 1,
    }


@pytest.fixture()
def editor(qapp, tmp_path):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(400, 400, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_graph()), encoding="utf-8")
    ed = AdvancedGraphEditor()
    assert ed.load_data(str(img_path), str(graph_path))
    return ed


def _marker_items(ed):
    return [it for it in ed.scene.items() if it.zValue() == RESIDUAL_Z]


def test_markers_drawn_on_z9(editor):
    editor.set_residual_defects(_residual())
    items = _marker_items(editor)
    assert len(items) == 2          # кольцо + номер
    assert editor.residual_spots() == [(1, "невидимая труба", "зазор 2.0 px")]


def test_markers_survive_redraw_all(editor):
    editor.set_residual_defects(_residual())
    editor._redraw_all()
    assert len(_marker_items(editor)) == 2


def test_marker_follows_node_positions(editor):
    """Позиция маркера живая — от item'ов узлов, не от точки из файла."""
    data = _residual()
    data["invisible_edges"][0]["point"] = [999.0, 999.0]   # заведомо мимо
    editor.set_residual_defects(data)
    from PySide6.QtWidgets import QGraphicsEllipseItem
    ring = next(it for it in _marker_items(editor)
                if isinstance(it, QGraphicsEllipseItem))
    c = ring.sceneBoundingRect().center()
    # Середина между центрами узлов a (120,120) и b (162,120).
    assert c.x() == pytest.approx(141.0, abs=3.0)
    assert c.y() == pytest.approx(120.0, abs=3.0)


def test_marker_sits_at_gap_not_centroid_mid(qapp, tmp_path):
    """У крупного блока центроид в сотнях px от щели — маркер невидимой трубы
    обязан стоять у щели (середина текущих нарисованных концов ребра)."""
    from PySide6.QtWidgets import QGraphicsEllipseItem

    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    graph = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "big", "type": "equipment", "class_name": "unknow",
             "centroid": [250.0, 250.0], "bbox": [100, 100, 400, 400],
             "degree": 1},
            {"id": "small", "type": "equipment", "class_name": "unknow",
             "centroid": [200.0, 422.0], "bbox": [402, 180, 442, 220],
             "degree": 1},
        ],
        "links": [
            {"id": "e1", "source": "big", "target": "small",
             "source_point": [200.0, 400.0], "target_point": [200.0, 402.0],
             "waypoints": []},
        ],
        "text_blocks": [], "bindings": [],
    }
    img = QImage(500, 500, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    ed = AdvancedGraphEditor()
    assert ed.load_data(str(img_path), str(graph_path))

    ed.set_residual_defects({
        "invisible_edges": [{"edge_id": "e1", "nodes": ["big", "small"],
                             "gap": 2.0, "point": [401.0, 200.0]}],
        "box_on_magi": [], "overlaps": [],
    })
    ring = next(it for it in _marker_items(ed)
                if isinstance(it, QGraphicsEllipseItem))
    c = ring.sceneBoundingRect().center()
    # Щель у x=401, y=200; середина центроидов (336, 225) — далеко мимо.
    assert c.x() == pytest.approx(401.0, abs=3.0)
    assert c.y() == pytest.approx(200.0, abs=3.0)


def test_focus_residual_centers_view(editor):
    editor.set_residual_defects(_residual())
    editor.focus_residual(0)
    center = editor.mapToScene(editor.viewport().rect().center())
    assert center.x() == pytest.approx(141.0, abs=5.0)
    assert center.y() == pytest.approx(120.0, abs=5.0)


def test_visibility_toggle_and_clear(editor):
    editor.set_residual_defects(_residual())
    editor.set_residual_visible(False)
    assert _marker_items(editor) == []
    editor.set_residual_visible(True)
    assert len(_marker_items(editor)) == 2
    editor.clear_residual_defects()
    assert _marker_items(editor) == []


# ─────────────────── провода вкладки: sha-гейт остатка ───────────────────

def _make_tab(monkeypatch, tmp_path, residual_sha):
    """Вкладка с загруженным canvas-редактором и файлом остатка."""
    from unittest.mock import MagicMock

    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.tabs.base_graph_tab import BaseGraphTab

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)
    tab = AdvancedGraphTab("test-uid", "test", api_client=MagicMock())

    g = _graph()
    g["graph"]["canvas_transform"] = {
        "source_sha": "0" * 16, "layout_version": "t", "layout_applied": True}
    img = QImage(400, 400, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(g), encoding="utf-8")

    ed = tab._create_editor()
    assert ed.load_data(str(img_path), str(graph_path))
    ed._canvas_mode = True          # как ставит _on_downloaded в USE_CANVAS
    tab._editor = ed

    data = _residual()
    data["canvas_sha"] = residual_sha
    res_path = tmp_path / "residual_defects.json"
    res_path.write_text(json.dumps(data), encoding="utf-8")
    tab._residual_path = res_path

    tab._on_editor_ready()
    return tab


def test_tab_shows_residual_for_matching_canvas(qapp, monkeypatch, tmp_path):
    from modules.graph.core import canvas_state

    sha = canvas_state.graph_projection_sha(_graph())
    tab = _make_tab(monkeypatch, tmp_path, sha)
    assert tab.btn_residual.isVisible() or tab.btn_residual.isVisibleTo(tab)
    assert tab.btn_residual.text() == "Очаги (1)"
    assert len(_marker_items(tab._editor)) == 2


def test_tab_hides_stale_residual(qapp, monkeypatch, tmp_path):
    tab = _make_tab(monkeypatch, tmp_path, "deadbeefdeadbeef")
    assert not tab.btn_residual.isVisibleTo(tab)
    assert _marker_items(tab._editor) == []
