# -*- coding: utf-8 -*-
"""Выбор узла не должен пережить сам узел.

Крах из живого прогона (2026-08-26, uid ae30deb4): в режиме «Добавить ребро»
узел выбран кликом, потом удалён Ctrl+ПКМ (там приоритет «узел > ребро»), а
`selected_node` остался — и каждое движение мыши падало в
`update_preview_line` → `self.nodes[<удалённый>]` → KeyError на каждый
mouseMove.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 600, 400


def _graph() -> dict:
    nodes = [
        {"id": "box", "type": "equipment", "centroid": [200.0, 200.0],
         "bbox": [150.0, 150.0, 250.0, 250.0], "segmentation": None,
         "class_id": 1, "class_name": "nasos", "degree": 0},
        {"id": "conn", "type": "connector", "centroid": [200.0, 400.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 0},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": [], "text_blocks": [], "bindings": []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _editor(qapp, tmp_path):
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_graph()), encoding="utf-8")

    ed = SimpleGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


def test_delete_of_selected_node_drops_the_selection(qapp, tmp_path):
    """Удаление выбранного узла снимает выбор, а не оставляет висячий id."""
    ed = _editor(qapp, tmp_path)
    ed.set_mode("add_edge")
    ed.select_node("box")
    assert ed.selected_node == "box"

    ed.delete_node("box")

    assert ed.selected_node is None
    # Ровно тот вызов, что падал на каждом движении мыши.
    ed.update_preview_line(300.0, 300.0)


def test_preview_line_survives_a_stale_selection(qapp, tmp_path):
    """Страховка: даже висячий выбор не роняет preview, а тихо снимается."""
    ed = _editor(qapp, tmp_path)
    ed.set_mode("add_edge")
    ed.select_node("conn")
    ed.model.remove_node("conn")          # узел ушёл мимо remove_node_items

    ed.update_preview_line(10.0, 10.0)

    assert ed.selected_node is None
