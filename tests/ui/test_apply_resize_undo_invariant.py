# -*- coding: utf-8 -*-
"""Пункт 1.x8 дороги, носитель 3 — undo-инвариант кнопки «Применить» в «Размерах».

ТРЕТИЙ носитель формы, закрытой в 1.1 (`test_smooth_undo_invariant.py`) и 1.x7
(`test_auto_fix_undo_invariant.py`); адрес назван самим 1.x7 строкой в §60ж и
правкой сознательно не тронут — «это другая команда, а её снимок завязан на
`_resize_model_base` — территорию 1-6».

`AdvancedGraphEditor.apply_resize` открывает шаг undo ДО работы
(`SnapshotCommand` с `_before = self._resize_model_base`, то есть состоянием
ДО живого превью), а закрывает ПОСЛЕ — `finalize()` + `push_executed`. Между
ними четыре мутатора: `_apply_sizes_from_base` (centroid/bbox/segmentation),
`_spread_overlaps` (расталкивание соседей), `_reseat_after_resize` (пересадка
концов рёбер каноном `seating.reseat_edge`) и тот же `auto_fix_graph`, что
уронил 1.x7. Пока закрытие стояло вне `try/finally`, падение любого из них
оставляло оператора с изменённым холстом и без шага отмены, а
`undo_mgr.revision` не рос — вкладка считает по нему `has_unsaved_changes()`
и закрывалась БЕЗ вопроса (замер §73в: rev 0→0, стек 0→0, Ctrl+Z не возвращает).

Вторая половина, тихая, ровно как в 1.x7: `_redraw_all()` тоже стоял за `try`,
поэтому после падения сцена показывала СТАРУЮ рамку `[24.5, 214.5, 45.5, 251.5]`
поверх уже изменённой модели `[-10.0, 188.0, 80.0, 278.0]` — оператор видит
одно, а на сервер уходит другое (семья 1.5).

⚠ Движок подменяется в `ui.editors.advanced_graph_editor`, а НЕ в
`ui.editors.autofix_chains`: редактор связывает имя `auto_fix_graph` в свой
модуль обычным `from ... import` на уровне модуля, и подмена в исходном модуле
до вызова не доходит — тест остался бы зелёным при любом дефекте (ловушка
из 1.x7, наследникам в докстринге).

Координаты двойственны (CODING_GUIDE §6): centroid = [y, x], bbox = [x1, y1, x2, y2].
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

CLASS = "zadvizhka"
NEW_W, NEW_H = 90.0, 90.0     # цель «Применить»: квадрат 90×90 вместо 40×40
BOOM_SHIFT = 17.0             # px — сдвиг, который «движок» кладёт до падения


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph():
    """Два бокса одного класса на косой трубе — набору «Размеров» есть что менять."""
    nodes = [
        {"id": "low", "type": "equipment", "centroid": [400.0, 220.0],
         "bbox": [200.0, 380.0, 240.0, 420.0], "segmentation": None,
         "class_id": 7, "class_name": CLASS, "degree": 1},
        {"id": "high", "type": "equipment", "centroid": [120.0, 260.0],
         "bbox": [240.0, 100.0, 280.0, 140.0], "segmentation": None,
         "class_id": 7, "class_name": CLASS, "degree": 1},
    ]
    links = [{"id": "e1", "source": "low", "target": "high",
              "source_point": [380.0, 220.0], "target_point": [140.0, 260.0],
              "waypoints": []}]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


@pytest.fixture
def ed(qapp, tmp_path):
    """Редактор в инструменте «Размеры» с набранным классом — как после кнопки."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_graph()), encoding="utf-8")

    editor = AdvancedGraphEditor()
    assert editor.load_data(str(ip), str(gp))
    editor.set_mode("resize_objects")
    editor.set_resize_class(CLASS)
    assert editor._resize_kind() == "box", "набор должен быть чисто боксовым"
    assert editor._resize_sel == {"low", "high"}, "набор не собрался"
    yield editor

    # ⛔ Снос детерминированный, а не «пусть соберёт GC» (замер §73е). Брошенный
    # редактор режима «Размеры» оставляет в очереди Qt отложенные удаления;
    # Python успевает освободить C++-объект раньше, чем очередь дойдёт до них,
    # и падает ЧУЖОЙ набор — тот, что первым крутит `processEvents`
    # (`test_frame_tab_opens_on_broken_artifact::_pump`, access violation
    # 5 прогонов из 8). Очередь обязана быть выпита здесь, своим набором.
    editor.set_mode("idle")
    editor.scene.clear()
    editor.setParent(None)
    editor.deleteLater()
    qapp.processEvents()


def _canvas(editor):
    """Холст как ДАННЫЕ: узлы и рёбра, без служебных полей модели."""
    return copy.deepcopy((editor.model.graph_data.get("nodes"),
                          editor.model.graph_data.get("links")))


def _scene_boxes(editor):
    """Где рамки НАРИСОВАНЫ, в координатах сцены: {id: (x1, y1, x2, y2)}."""
    out = {}
    for node_id, item in editor.bbox_items.items():
        r = item.sceneBoundingRect()
        out[node_id] = (r.left(), r.top(), r.right(), r.bottom())
    return out


def _model_boxes(editor):
    """Где рамки ЛЕЖАТ В МОДЕЛИ, в тех же координатах."""
    return {node_id: tuple(float(v) for v in node["bbox"])
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


def test_apply_resize_pushes_exactly_one_undo_step(ed):
    """Успешный прогон: ровно один шаг undo, и он возвращает холст."""
    before = _canvas(ed)
    depth0, rev0 = ed.undo_mgr.stack_depth, ed.undo_mgr.revision

    ed.apply_resize(width=NEW_W, height=NEW_H)

    assert ed.undo_mgr.stack_depth == depth0 + 1
    assert ed.undo_mgr.revision == rev0 + 1
    assert _canvas(ed) != before, "фикстура обязана реально измениться"

    assert ed.undo_mgr.undo() == "Размер объектов"
    assert _canvas(ed) == before, "Ctrl+Z обязан вернуть холст побайтово"
    assert ed.undo_mgr.stack_depth == depth0


def test_engine_crash_still_leaves_the_undo_step(ed, monkeypatch):
    """Падение движка посреди работы: холст изменён — шаг отмены обязан быть.

    Отказ не проглатывается (исключение доходит до вызывающего), но оператор
    остаётся с рабочим Ctrl+Z."""
    _crash_the_engine(monkeypatch)
    before = _canvas(ed)
    depth0, rev0 = ed.undo_mgr.stack_depth, ed.undo_mgr.revision

    with pytest.raises(IndexError):
        ed.apply_resize(width=NEW_W, height=NEW_H)

    assert _model_boxes(ed)["low"] != tuple(before[0][0]["bbox"]), \
        "фикстура обязана оставить порчу на холсте"
    assert ed.undo_mgr.stack_depth == depth0 + 1, \
        "холст изменён, а шага отмены нет — оператору нечем откатить"
    assert ed.undo_mgr.revision == rev0 + 1, \
        "revision не вырос — вкладка сочтёт порчу «нет несохранённого»"

    assert ed.undo_mgr.undo() == "Размер объектов"
    assert _canvas(ed) == before, "Ctrl+Z обязан вернуть холст побайтово"
    assert ed.undo_mgr.stack_depth == depth0


def test_engine_crash_keeps_the_scene_equal_to_the_model(ed, monkeypatch):
    """Тихая половина: после падения оператор видит то, что лежит в модели.

    Перерисовка стояла за `try`, поэтому сцена показывала прежнюю рамку 40×40
    поверх уже увеличенной до 90×90 модели — сохранялось и экспортировалось
    не то, что на экране."""
    _crash_the_engine(monkeypatch)

    with pytest.raises(IndexError):
        ed.apply_resize(width=NEW_W, height=NEW_H)

    model, scene = _model_boxes(ed), _scene_boxes(ed)
    assert set(model) == set(scene)
    diverged = {
        node_id: (model[node_id], scene[node_id])
        for node_id in model
        if any(abs(m - s) > SCENE_TOL
               for m, s in zip(model[node_id], scene[node_id]))
    }
    assert not diverged, \
        f"сцена расходится с моделью — оператор видит не свой холст: {diverged}"


def test_undo_after_crash_returns_the_size_the_operator_started_from(ed, monkeypatch):
    """Число, ради которого пункт заводился: Ctrl+Z возвращает 40×40, а не 90×90."""
    _crash_the_engine(monkeypatch)
    base = _model_boxes(ed)["low"]
    assert (base[2] - base[0], base[3] - base[1]) == (40.0, 40.0), "фикстура сдвинулась"

    with pytest.raises(IndexError):
        ed.apply_resize(width=NEW_W, height=NEW_H)

    grown = _model_boxes(ed)["low"]
    assert (grown[2] - grown[0], grown[3] - grown[1]) == (NEW_W, NEW_H), \
        "размер не применился — падение поймано не там"

    ed.undo()

    back = _model_boxes(ed)["low"]
    assert (back[2] - back[0], back[3] - back[1]) == (40.0, 40.0)
    assert (back[2] - back[0], back[3] - back[1]) != (NEW_W, NEW_H)
