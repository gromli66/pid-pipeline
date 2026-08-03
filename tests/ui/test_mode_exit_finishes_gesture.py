# -*- coding: utf-8 -*-
"""Смена режима/Esc посреди жеста EditWaypointHandler обязана добить жест.

Репро аудита 2026-08-03: on_exit только прятал маркеры — кадровое состояние
(_drag_route_ctx, libavoid-сессия, _drag_routable_edges, незакрытый
_ep_snap_cmd) переживало жест: следующие внежестовые роутинги судили
препятствия по снапшоту брошенного жеста, а шаг undo терялся.
Эталон поведения — DragNodeHandler.on_exit (end_drag_node).
Жесты дёргаются теми же внутренними методами, что mouse-механика
(паттерн tests/ui/test_port_model.py).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from ui.editors.mode_handlers.advanced_handlers import EditWaypointHandler  # noqa: E402

IMG_W, IMG_H = 800, 600


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wrap(nodes, links):
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _graph(waypoints):
    nodes = [
        {"id": "box", "type": "equipment", "centroid": [200.0, 120.0],
         "bbox": [80.0, 160.0, 160.0, 240.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "conn", "type": "connector", "centroid": [200.0, 300.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 1},
    ]
    links = [{"id": "edge_1", "source": "box", "target": "conn",
              "source_point": [200.0, 160.0], "target_point": [200.0, 300.0],
              "waypoints": waypoints}]
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


def test_mode_exit_mid_endpoint_drag_finishes_gesture(qapp, tmp_path):
    """Выход из режима посреди протяжки конца: жест завершён, undo записан."""
    ed = _editor(qapp, tmp_path, _graph([]))
    key = ed.model.edge_key("box", "conn")
    depth0 = ed.undo_mgr.stack_depth

    ed._start_endpoint_drag((key, "source"))
    ed._drag_endpoint_to(165.0, 225.0)   # > EP_DRAG_THRESHOLD: жест armed
    assert ed._dragging_endpoint is not None
    assert ed._drag_route_ctx is not None

    EditWaypointHandler().on_exit(ed)

    assert ed._dragging_endpoint is None, "жест не добит при выходе из режима"
    assert ed._drag_route_ctx is None, \
        "кадровый контекст пережил жест — внежестовые роутинги получат стейл"
    assert ed._avoid_session is None, "libavoid-сессия пережила жест"
    assert not ed._drag_routable_edges
    assert getattr(ed, "_ep_snap_cmd", None) is None
    assert ed.undo_mgr.stack_depth == depth0 + 1, \
        "шаг undo брошенного жеста потерян"


def test_mode_exit_mid_waypoint_drag_finishes_gesture(qapp, tmp_path):
    """Выход из режима посреди переноса waypoint: жест завершён, undo записан."""
    ed = _editor(qapp, tmp_path, _graph([[150.0, 230.0]]))
    key = ed.model.edge_key("box", "conn")
    depth0 = ed.undo_mgr.stack_depth

    ed._start_waypoint_drag((key, 0))
    ed._drag_waypoint_to(232.0, 148.0)
    assert ed.dragging_waypoint is not None

    EditWaypointHandler().on_exit(ed)

    assert ed.dragging_waypoint is None, "жест не добит при выходе из режима"
    assert ed.undo_mgr.stack_depth == depth0 + 1, \
        "шаг undo брошенного жеста потерян"
