# -*- coding: utf-8 -*-
"""Пункт 1.1 дороги — undo-инвариант кнопки «Авто-выравнивание» (Э4).

`AdvancedGraphEditor.smooth_canvas` открывает шаг undo ДО работы
(`SnapshotCommand.execute()` снимает «как было»), а закрывает его ПОСЛЕ
(`finalize()` + `push_executed`). Между ними движок мутирует
`model.graph_data` НА МЕСТЕ. Пока закрытие стояло за пределами
`try/finally`, падение движка оставляло оператора с изменённым холстом и
без шага отмены: Ctrl+Z отменял бы предыдущий жест, а не сглаживание.
Вдобавок не рос `undo_mgr.revision`, по которому вкладка считает
`has_unsaved_changes` (`ui/tabs/base_graph_tab.py:1106`), — вкладка
закрывалась без вопроса, унося порчу молча.

Инвариант, который здесь запирается, — про ДАННЫЕ, а не про внутренности
редактора: после вызова `smooth_canvas` в стеке ровно на один шаг больше,
и этот шаг возвращает холст побайтово. Он верен и для успешного прогона,
и для упавшего.

Падение движка подаётся подменой самого движка, а не подбором холста:
через лестницу маршрутов редактора крах воспроизводим (репро в
`tests/test_edit_smooth.py::test_declining_router_does_not_crash_the_ladder`),
но фикстура под него была бы привязана к устройству `_route_orthogonal`,
которое уезжает вместе с декомпозицией UI (этап 10).

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


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph():
    """Косая труба рамка -> рамка: сглаживанию есть что чинить."""
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


def _boom(graph, **_kwargs):
    """Движок, который успел испортить холст и упал.

    `IndexError` — ровно то, чем падал `shift_run` (`edit_smooth.py:322`)
    на укоротившемся снимке; порча ложится ДО падения, как и в бою."""
    graph["links"][0]["waypoints"] = [[999.0, 999.0]]
    raise IndexError("list index out of range")


def test_smooth_canvas_pushes_exactly_one_undo_step(ed):
    """Успешный прогон: ровно один шаг undo, и он возвращает холст."""
    before = _canvas(ed)
    depth0, rev0 = ed.undo_mgr.stack_depth, ed.undo_mgr.revision

    ed.smooth_canvas()

    assert ed.undo_mgr.stack_depth == depth0 + 1
    assert ed.undo_mgr.revision == rev0 + 1
    assert _canvas(ed) != before, "фикстура обязана реально измениться"

    assert ed.undo_mgr.undo() == "Сглаживание"
    assert _canvas(ed) == before, "Ctrl+Z обязан вернуть холст побайтово"
    assert ed.undo_mgr.stack_depth == depth0


def test_engine_crash_still_leaves_the_undo_step(ed, monkeypatch):
    """Падение движка посреди работы: холст изменён — шаг отмены обязан быть.

    Отказ не проглатывается (исключение доходит до вызывающего, у кнопки
    свой обработчик), но оператор остаётся с рабочим Ctrl+Z."""
    from modules.graph.core import edit_smooth

    monkeypatch.setattr(edit_smooth, "smooth", _boom)
    before = _canvas(ed)
    depth0, rev0 = ed.undo_mgr.stack_depth, ed.undo_mgr.revision

    with pytest.raises(IndexError):
        ed.smooth_canvas()

    key = ed.model.edge_key("low", "high")
    assert ed.model.find_edge_data(key)["waypoints"] == [[999.0, 999.0]], \
        "фикстура обязана оставить порчу на холсте"
    assert ed.undo_mgr.stack_depth == depth0 + 1, \
        "холст изменён, а шага отмены нет — оператору нечем откатить"
    assert ed.undo_mgr.revision == rev0 + 1, \
        "revision не вырос — вкладка сочтёт порчу «нет несохранённого»"

    assert ed.undo_mgr.undo() == "Сглаживание"
    assert _canvas(ed) == before, "Ctrl+Z обязан вернуть холст побайтово"
    assert ed.undo_mgr.stack_depth == depth0
