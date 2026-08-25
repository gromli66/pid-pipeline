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
    """Кнопки, которые замок Э4-00 выключает после раскладки.

    2026-08-02: «Авто-выравнивание» ИЗ ЗАМКА ВЫВЕДЕНО — за ней теперь не
    прежний auto_fix (он и давал регрессию), а Э4-сглаживание, которое как
    раз для холста после раскладки и предназначено. 2026-08-25 (4.1): у неё
    свой замок, ОБРАТНЫЙ — запирается там, где раскладки нет, — поэтому в
    этот перебор она по-прежнему не входит."""
    return (tab.btn_optimize_edge, tab.btn_optimize_all)


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


def _editor_with(tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(400, 400, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    ed = AdvancedGraphEditor()
    assert ed.load_data(str(img_path), str(graph_path))
    return ed


def test_apply_resize_skips_full_graph_autofix_after_layout(qapp, monkeypatch, tmp_path):
    """Обход Э4-00 закрыт: «Применить» в панели «Размеры» не запускает
    полнографный auto_fix_graph на холсте после раскладки."""
    import ui.editors.advanced_graph_editor as age

    calls = []
    monkeypatch.setattr(age, "auto_fix_graph", lambda *a, **k: calls.append(1))
    ed = _editor_with(tmp_path, _graph(layout_applied=True))
    ed._resize_sel = {"a"}
    ed.apply_resize(width=50, height=50)
    assert calls == []


def test_apply_resize_keeps_autofix_on_fallback_canvas(qapp, monkeypatch, tmp_path):
    import ui.editors.advanced_graph_editor as age

    calls = []
    monkeypatch.setattr(age, "auto_fix_graph", lambda *a, **k: calls.append(1))
    ed = _editor_with(tmp_path, _graph(layout_applied=False))
    ed._resize_sel = {"a"}
    ed.apply_resize(width=50, height=50)
    assert calls == [1]


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


def test_autofix_button_free_after_layout(qapp, monkeypatch, tmp_path):
    """«Авто-выравнивание» после раскладки ДОСТУПНО и ведёт в Э4-сглаживание
    (решение заказчика 2026-08-02: «это вместо автовыравнивания кнопки»)."""
    tab = _make_tab(monkeypatch, tmp_path, _graph(layout_applied=True))
    assert tab.btn_auto_fix.isEnabled(), \
        "кнопка сглаживания обязана работать на холсте после раскладки"

    called = []
    monkeypatch.setattr(tab._editor, "smooth_canvas",
                        lambda *a, **k: called.append("smooth") or {})
    monkeypatch.setattr(tab._editor, "auto_fix",
                        lambda *a, **k: called.append("autofix"))
    tab._auto_fix()
    assert called == ["smooth"], f"после раскладки ожидалось сглаживание: {called}"


@pytest.mark.parametrize("layout_applied", [False, None],
                         ids=["фолбэк", "легаси-без-метки"])
def test_autofix_button_is_locked_without_layout(qapp, monkeypatch, tmp_path,
                                                 layout_applied):
    """ПЕРЕОБЪЯВЛЕН 4.1 (2026-08-25): на холсте БЕЗ раскладки кнопка заперта.

    Было наоборот — «кнопка по-прежнему зовёт прежний auto_fix, там он в
    родной среде». Родной средой он не оказался: медианное выравнивание
    цепочек идёт без единой проверки коллизий и без отката хода, а ветка
    достижима в обычном рабочем цикле (подробности и оба пути —
    `tests/ui/test_auto_fix_button_contract.py`).
    """
    tab = _make_tab(monkeypatch, tmp_path, _graph(layout_applied=layout_applied))
    assert not tab.btn_auto_fix.isEnabled()
    called = []
    monkeypatch.setattr(tab._editor, "smooth_canvas",
                        lambda *a, **k: called.append("smooth") or {})
    monkeypatch.setattr(tab._editor, "auto_fix",
                        lambda *a, **k: called.append("autofix"))
    tab._auto_fix()
    assert called == [], f"прежний auto_fix запущен на холсте без раскладки: {called}"
