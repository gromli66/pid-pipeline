# -*- coding: utf-8 -*-
"""Пункт 1.x7 дороги — undo-инвариант легаси-кнопки «Авто-выравнивание».

Близнец 1.1 (`tests/ui/test_smooth_undo_invariant.py`), другой носитель того
же механизма. `AdvancedGraphEditor.auto_fix` открывает шаг undo ДО работы
(`SnapshotCommand.execute()` снимает «как было»), а закрывает его ПОСЛЕ
(`finalize()` + `push_executed`). Между ними `autofix_chains.auto_fix_graph`
мутирует узлы и рёбра НА МЕСТЕ (Step 4 — centroid/bbox/segmentation, затем
пересадка рёбер каноном `seating.reseat_edge`). Пока закрытие стояло за
пределами `try/finally`, падение движка оставляло оператора с изменённым
холстом и без шага отмены, а `undo_mgr.revision` не рос — вкладка считает по
нему `has_unsaved_changes` (`ui/tabs/base_graph_tab.py`) и закрывалась без
вопроса.

Вторая половина, тихая: `_redraw_all()` тоже стоял за `try`, поэтому после
падения сцена показывала СТАРЫЕ координаты поверх уже сдвинутой модели —
оператор видел одно, а сохранялось и экспортировалось другое.

Путь живой: `ui/tabs/advanced_graph_tab.py` зовёт `auto_fix()` в фолбэк-ветке
кнопки (холст без раскладки), у самой кнопки свой обработчик исключения.

⚠ Движок подменяется в `ui.editors.advanced_graph_editor`, а НЕ в
`ui.editors.autofix_chains`: редактор связывает имя `auto_fix_graph` в свой
модуль обычным `from ... import` на уровне модуля, и подмена в исходном модуле
до вызова не доходит — тест остался бы зелёным при любом дефекте.

Координаты двойственны (CODING_GUIDE §6): centroid/source_point = [y, x],
bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 800, 600

# Допуск сверки сцены с моделью — правило класса [ui] дороги (гейт 0.4).
SCENE_TOL = 4.0

BOOM_SHIFT = 17.0     # px — сдвиг, который «движок» успевает положить до падения


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph():
    """Косая труба рамка -> рамка: цепочке есть что выравнивать."""
    nodes = [
        {"id": "low", "type": "equipment", "centroid": [400.0, 220.0],
         "bbox": [200.0, 380.0, 240.0, 420.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "high", "type": "equipment", "centroid": [120.0, 260.0],
         "bbox": [240.0, 100.0, 280.0, 140.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [{"id": "e1", "source": "low", "target": "high",
              "source_point": [380.0, 220.0], "target_point": [140.0, 260.0],
              "waypoints": []}]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


@pytest.fixture
def ed(qapp, tmp_path):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_graph()), encoding="utf-8")

    editor = AdvancedGraphEditor()
    assert editor.load_data(str(ip), str(gp))
    return editor


def _canvas(editor):
    """Холст как ДАННЫЕ: узлы и рёбра, без служебных полей модели."""
    return copy.deepcopy((editor.model.graph_data.get("nodes"),
                          editor.model.graph_data.get("links")))


def _scene_centers(editor):
    """Где узлы НАРИСОВАНЫ, в координатах сцены: {id: (x, y)}."""
    out = {}
    for node_id, item in editor.node_items.items():
        center = item.sceneBoundingRect().center()
        out[node_id] = (center.x(), center.y())
    return out


def _model_centers(editor):
    """Где узлы ЛЕЖАТ В МОДЕЛИ, в тех же координатах: {id: (x, y)}."""
    return {node_id: (node["centroid"][1], node["centroid"][0])
            for node_id, node in editor.nodes.items()}


def _boom(nodes, edges_data, **_kwargs):
    """Движок, который успел сдвинуть узлы и упал.

    Порча ложится ДО падения — как в бою: `auto_fix_graph` двигает centroid
    в Step 4, а упасть может дальше, на пересадке рёбер."""
    for node in nodes.values():
        node["centroid"] = [node["centroid"][0] + BOOM_SHIFT,
                            node["centroid"][1] + BOOM_SHIFT]
    edges_data[0]["waypoints"] = [[999.0, 999.0]]
    raise IndexError("list index out of range")


def _crash_the_engine(monkeypatch):
    import ui.editors.advanced_graph_editor as editor_module

    monkeypatch.setattr(editor_module, "auto_fix_graph", _boom)


def test_auto_fix_pushes_exactly_one_undo_step(ed):
    """Успешный прогон: ровно один шаг undo, и он возвращает холст."""
    before = _canvas(ed)
    depth0, rev0 = ed.undo_mgr.stack_depth, ed.undo_mgr.revision

    ed.auto_fix()

    assert ed.undo_mgr.stack_depth == depth0 + 1
    assert ed.undo_mgr.revision == rev0 + 1
    assert _canvas(ed) != before, "фикстура обязана реально измениться"

    assert ed.undo_mgr.undo() == "Auto-Fix (chains)"
    assert _canvas(ed) == before, "Ctrl+Z обязан вернуть холст побайтово"
    assert ed.undo_mgr.stack_depth == depth0


def test_engine_crash_still_leaves_the_undo_step(ed, monkeypatch):
    """Падение движка посреди работы: холст изменён — шаг отмены обязан быть.

    Отказ не проглатывается (исключение доходит до вызывающего, у кнопки
    свой обработчик), но оператор остаётся с рабочим Ctrl+Z."""
    _crash_the_engine(monkeypatch)
    before = _canvas(ed)
    depth0, rev0 = ed.undo_mgr.stack_depth, ed.undo_mgr.revision

    with pytest.raises(IndexError):
        ed.auto_fix()

    assert _model_centers(ed)["low"][0] == 220.0 + BOOM_SHIFT, \
        "фикстура обязана оставить порчу на холсте"
    assert ed.undo_mgr.stack_depth == depth0 + 1, \
        "холст изменён, а шага отмены нет — оператору нечем откатить"
    assert ed.undo_mgr.revision == rev0 + 1, \
        "revision не вырос — вкладка сочтёт порчу «нет несохранённого»"

    assert ed.undo_mgr.undo() == "Auto-Fix (chains)"
    assert _canvas(ed) == before, "Ctrl+Z обязан вернуть холст побайтово"
    assert ed.undo_mgr.stack_depth == depth0


def test_engine_crash_keeps_the_scene_equal_to_the_model(ed, monkeypatch):
    """Тихая половина: после падения оператор видит то, что лежит в модели.

    Перерисовка стояла за `try`, поэтому сцена показывала прежние координаты
    поверх уже сдвинутых узлов — сохранялось и экспортировалось не то, что
    на экране."""
    _crash_the_engine(monkeypatch)

    with pytest.raises(IndexError):
        ed.auto_fix()

    model, scene = _model_centers(ed), _scene_centers(ed)
    assert set(model) == set(scene)
    diverged = {
        node_id: (model[node_id], scene[node_id])
        for node_id in model
        if abs(model[node_id][0] - scene[node_id][0]) > SCENE_TOL
        or abs(model[node_id][1] - scene[node_id][1]) > SCENE_TOL
    }
    assert not diverged, \
        f"сцена расходится с моделью — оператор видит не свой холст: {diverged}"
