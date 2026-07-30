# -*- coding: utf-8 -*-
"""Э4-00: после авто-раскладки кнопки «Авто-выравнивание»/«Оптимизировать»
выключены (замер §1.1 EDITOR_AFTER_LAYOUT_PLAN: поверх раскладки — регрессия).

На фолбэк- и legacy-холстах (layout_applied=False или метки нет) кнопки
работают как раньше.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from unittest.mock import MagicMock

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph(layout_applied):
    """Минимальный холст; layout_applied=None — legacy без метки."""
    g = {
        "directed": False,
        "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "centroid": [100.0, 100.0],
             "bbox": [80, 80, 120, 120], "class_name": "unknow", "degree": 1},
            {"id": "b", "type": "connector", "centroid": [100.0, 300.0],
             "bbox": None, "class_name": "connector", "degree": 1},
        ],
        "links": [
            {"id": "e1", "source": "a", "target": "b",
             "source_point": [100.0, 120.0], "target_point": [100.0, 300.0],
             "waypoints": []},
        ],
        "text_blocks": [],
        "bindings": [],
    }
    if layout_applied is not None:
        g["graph"]["canvas_transform"] = {
            "source_sha": "0" * 16,
            "layout_version": "test",
            "layout_applied": layout_applied,
        }
    return g


def _make_tab(monkeypatch, tmp_path, graph):
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.tabs.base_graph_tab import BaseGraphTab

    # Вкладка при создании стартует фоновое скачивание артефактов — в тесте
    # оно не нужно (редактор подставляется руками).
    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)
    tab = AdvancedGraphTab("test-uid", "test", api_client=MagicMock())

    img = QImage(400, 400, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

    editor = tab._create_editor()
    assert editor.load_data(str(img_path), str(graph_path))
    tab._editor = editor
    tab._on_editor_ready()
    return tab


def _lock_buttons(tab):
    return (tab.btn_optimize_edge, tab.btn_optimize_all, tab.btn_auto_fix)


def test_buttons_locked_after_layout(qapp, monkeypatch, tmp_path):
    tab = _make_tab(monkeypatch, tmp_path, _graph(layout_applied=True))
    for btn in _lock_buttons(tab):
        assert not btn.isEnabled()
        assert "раскладка" in btn.toolTip().lower()


def test_buttons_alive_on_fallback_canvas(qapp, monkeypatch, tmp_path):
    tab = _make_tab(monkeypatch, tmp_path, _graph(layout_applied=False))
    for btn in _lock_buttons(tab):
        assert btn.isEnabled()


def test_buttons_alive_on_legacy_canvas_without_stamp(qapp, monkeypatch, tmp_path):
    tab = _make_tab(monkeypatch, tmp_path, _graph(layout_applied=None))
    for btn in _lock_buttons(tab):
        assert btn.isEnabled()


def test_unlock_restores_original_tooltip(qapp, monkeypatch, tmp_path):
    """Повторная загрузка холста без раскладки возвращает кнопки к жизни."""
    tab = _make_tab(monkeypatch, tmp_path, _graph(layout_applied=True))
    orig_tooltips = {}
    for btn in _lock_buttons(tab):
        orig_tooltips[btn] = btn.property("_pre_lock_tooltip")
        assert not btn.isEnabled()

    tab._editor.model.graph_data["graph"]["canvas_transform"]["layout_applied"] = False
    tab._apply_layout_lock()
    for btn in _lock_buttons(tab):
        assert btn.isEnabled()
        assert btn.toolTip() == orig_tooltips[btn]
