# -*- coding: utf-8 -*-
"""Цвет рёбер в редакторах графа (Проверка схемы / Ручная правка / Контуры).

Замки на решения 2026-08-24:
* базовый цвет — кислотно-зелёный непрозрачный, ОДИН на обе темы: прежние
  цвета по теме (белый полупрозрачный на тёмном фоне, тёмно-серый на светлом
  листе) сливались с линиями скана под графом;
* выбранный оператором цвет переживает переключения «Светлый лист» и
  «Показать подложку» — раньше каждое из них пересобирало сцену и затирало
  выбор, из-за чего цвет приходилось выставлять заново;
* цвет из шторки НЕ уходит в FXML: он меняет только константу отрисовки, а
  экспорт смотрит на `render_color`, который ставит режим «Линии → Цвет».
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 400, 300
ACID_GREEN = "#39ff14"
PICKED = "#ff0000"        # что оператор выбрал в шторке


def _graph() -> dict:
    """Два коннектора и ребро между ними."""
    nodes = [
        {"id": "a", "type": "connector", "centroid": [100.0, 100.0],
         "bbox": None, "segmentation": None, "class_id": -1,
         "class_name": "connector", "degree": 1},
        {"id": "b", "type": "connector", "centroid": [100.0, 300.0],
         "bbox": None, "segmentation": None, "class_id": -1,
         "class_name": "connector", "degree": 1},
    ]
    links = [{"id": "e1", "source": "a", "target": "b",
              "source_point": [100.0, 100.0], "target_point": [100.0, 300.0]}]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(qapp, tmp_path):
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


def _edge_pen_color(ed) -> QColor:
    """Цвет, которым ребро РЕАЛЬНО нарисовано (а не только константа)."""
    (item,) = ed.edge_items.values()
    return item.pen().color()


@pytest.mark.parametrize("light", [True, False])
def test_базовый_цвет_рёбер_кислотно_зелёный(editor, light):
    """Один цвет на обе темы: на белом листе он виден так же, как на тёмном."""
    editor.set_light_theme(light)

    assert editor.COLOR_EDGE.name() == ACID_GREEN
    assert editor.COLOR_EDGE.alpha() == 255
    assert _edge_pen_color(editor).name() == ACID_GREEN


def test_выбранный_цвет_переживает_переключение_листа(editor):
    """Переключение листа пересобирает сцену — цвет обязан остаться выбранным."""
    editor.set_edge_color(QColor(PICKED))
    editor.set_light_theme(not editor._light_theme)

    assert editor.COLOR_EDGE.name() == PICKED
    assert _edge_pen_color(editor).name() == PICKED


def test_выбранный_цвет_переживает_переключение_подложки(editor):
    editor.set_edge_color(QColor(PICKED))
    editor.set_background_visible(not editor._bg_visible)

    assert editor.COLOR_EDGE.name() == PICKED
    assert _edge_pen_color(editor).name() == PICKED


def test_сброс_оформления_возвращает_базовый(editor):
    """`set_edge_color(None)` — путь кнопки «Сбросить» в шторке."""
    editor.set_edge_color(QColor(PICKED))
    editor.set_edge_color(None)

    assert editor.COLOR_EDGE.name() == ACID_GREEN
    assert _edge_pen_color(editor).name() == ACID_GREEN


def test_цвет_из_шторки_не_уходит_в_данные_рёбер(editor):
    """Иначе он уехал бы в FXML: экспорт печатает `render_color` как есть."""
    editor.set_edge_color(QColor(PICKED))

    assert all(e.get("render_color") is None for e in editor.edges_data)
