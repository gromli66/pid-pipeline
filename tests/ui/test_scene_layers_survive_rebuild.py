# -*- coding: utf-8 -*-
"""Слои холста «Ручной правки» переживают пересборку сцены (блок 1, mefx-1).

Набор написан характеризационным (коммит «до правки») и здесь перевёрнут
на целевое поведение — те же числа, но с другой стороны равенства.

Жалоба оператора 2026-08-25: включил «Скины» → нажал «Светлый лист» или
«Показать подложку» → скины пропали, а кнопка осталась нажатой; приходится
выключать и включать заново.

Механизм (снят по коду): оба переключателя зовут
`BaseGraphEditor.set_light_theme` (base:590) / `set_background_visible`
(base:598), а те — `setup_scene()` (base:401), который делает `scene.clear()`
плюс `_reset_scene_state()` и рисует только лист, подложку, рёбра и узлы.
Всё, что добавляет `AdvancedGraphEditor`, восстанавливает единственный метод
`_redraw_all()` (advanced:558), и `setup_scene` его не зовёт.

Лечение (1.1): `AdvancedGraphEditor.setup_scene` переопределён — `super()`
плюс общий хвост `_redraw_overlays()`, который зовут ОБА пути перерисовки;
живые оверлеи снимаются ДО `scene.clear()`.

Что этот файл запирает (числа сняты замером `MEASUREMENTS §MEFX1`):
* шесть слоёв на сцене: скины 3, сетка 27, OCR 6, рамки «Размеров» 2,
  подсветка выделенного узла 1 и ребра 1 — и столько же ПОСЛЕ переключения
  листа и подложки (до правки было ноль по всем шести при живых флагах);
* инвариант 1.2: кнопка «Скины» нажата ⟺ флаг `show_skins` ⟺ скины на сцене;
* три живых оверлея (правка полигона, ручки текст-блока, рамка добавления
  блока) не оставляют ссылок на разрушенные объекты C++ — до правки
  следующий жест оператора падал `RuntimeError`.

⛔ Переключатель сверяется с ПРОТИВОПОЛОЖНЫМ значением, а не с текущим:
`set_light_theme(light)` при `light == self._light_theme` выходит сразу
(base:593-594), и тест с дефолтным `True` был бы зелёным на любом коде.
Дефолты редактора — `_light_theme = True`, `_bg_visible = True`.

Координаты двойственны (CODING_GUIDE §6): centroid = [y, x],
bbox = [x1, y1, x2, y2].
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF                     # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsRectItem   # noqa: E402
from PySide6.QtGui import QImage, QColor              # noqa: E402

IMG_W, IMG_H = 800, 600

#: Шаг сетки задаём сами: иначе он выводится из медианы боксов
#: (`_compute_grid_size`) и число линий поплывёт от правки фикстуры.
GRID = 120

#: Сколько чего лежит на сцене при всех включённых слоях (замер §MEFX1).
ARMED = {
    "скины": 3,          # три equipment-бокса, у всех классов есть PNG
    "сетка": 27,         # холст 1920x1080 шагом 120: 17 вертикалей + 10 горизонталей
    "OCR": 6,            # два текст-блока, по три предмета сцены на блок
    "рамки": 2,          # набор «Размеров» по классу nasos
    "подсв.узла": 1,
    "подсв.ребра": 1,
}

#: Всего предметов на сцене при всех слоях (замер §MEFX1; до правки
#: переключение листа оставляло 13 — лист, подложку, рёбра и узлы).
SCENE_ARMED = 53

RESIZE_CLASS = "nasos"
RESIZE_SET = {"pump_a", "pump_b"}
SEL_NODE = "filtr"
SEL_EDGE = ("pump_a", "conn")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph() -> dict:
    """Три бокса оборудования со скинами, коннектор, два ребра, два текст-блока."""
    nodes = [
        {"id": "pump_a", "type": "equipment", "centroid": [130.0, 230.0],
         "bbox": [200.0, 100.0, 260.0, 160.0], "segmentation": None,
         "class_id": 1, "class_name": "nasos", "degree": 1},
        {"id": "pump_b", "type": "equipment", "centroid": [130.0, 430.0],
         "bbox": [400.0, 100.0, 460.0, 160.0], "segmentation": None,
         "class_id": 1, "class_name": "nasos", "degree": 1},
        {"id": "filtr", "type": "equipment", "centroid": [330.0, 630.0],
         "bbox": [600.0, 300.0, 660.0, 360.0], "segmentation": None,
         "class_id": 2, "class_name": "filtr_meh", "degree": 2},
        {"id": "conn", "type": "connector", "centroid": [230.0, 430.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 3},
    ]
    links = [
        {"id": "e1", "source": "pump_a", "target": "conn",
         "source_point": [130.0, 260.0], "target_point": [225.0, 430.0],
         "waypoints": []},
        {"id": "e2", "source": "conn", "target": "filtr",
         "source_point": [235.0, 430.0], "target_point": [330.0, 600.0],
         "waypoints": []},
    ]
    blocks = [
        {"id": "b1", "bbox": [200.0, 60.0, 280.0, 80.0], "text": "10LAB10AP001",
         "confidence": 0.9, "source": "ocr", "merged_into": None},
        {"id": "b2", "bbox": [600.0, 260.0, 680.0, 280.0], "text": "DN100",
         "confidence": 0.8, "source": "ocr", "merged_into": None},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links,
            "text_blocks": blocks, "bindings": []}


def _write_data(tmp_path, graph: dict):
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    return str(ip), str(gp)


@pytest.fixture
def data_paths(tmp_path):
    return _write_data(tmp_path, _graph())


def _new_editor(data_paths):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    ed = AdvancedGraphEditor()
    ed._canvas_mode = True                # «Ручная правка» — единственный холст
    assert ed.load_data(*data_paths)
    return ed


def _dispose(ed, qapp):
    """Снос детерминированный, а не «пусть соберёт сборщик» (PROTOCOL §5, замер 1-32)."""
    ed.set_mode("idle")
    ed.scene.clear()
    ed.setParent(None)
    ed.deleteLater()
    qapp.processEvents()


@pytest.fixture
def editor(qapp, data_paths):
    ed = _new_editor(data_paths)
    yield ed
    _dispose(ed, qapp)


def _alive(ed, items) -> int:
    """Сколько предметов слоя РЕАЛЬНО на сцене.

    Считать по словарям редактора нельзя: `_reset_scene_state` не чистит
    `_skin_items`, и после `scene.clear()` он держит три МЁРТВЫХ обёртки
    (замер §MEFX1: длина `_skin_items` = 3 при нуле скинов на сцене).
    """
    n = 0
    for it in items:
        try:
            if it.scene() is ed.scene:
                n += 1
        except RuntimeError:              # объект C++ уже удалён
            pass
    return n


def _skin_items(ed) -> list:
    return [it for v in ed._skin_items.values() for it in v]


def _layers(ed) -> dict:
    """Слои, которые сегодня восстанавливает только `_redraw_all`."""
    ocr = [it for pair in ed._ocr_block_items.values()
           for it in pair.values() if it is not None]
    return {
        "скины": _alive(ed, _skin_items(ed)),
        "сетка": _alive(ed, ed._grid_items),
        "OCR": _alive(ed, ocr),
        "рамки": _alive(ed, ed._resize_frames),
        "подсв.узла": _alive(ed, list(ed._selection_highlights.values())),
        "подсв.ребра": _alive(ed, list(ed._edge_selection_highlights.values())),
    }


def _arm(ed):
    """Включить всё, что оператор держит на холсте: скины, сетку, OCR, набор, выделение."""
    ed.set_show_skins(True)
    ed.grid_size = GRID
    ed.toggle_grid()
    ed.display_regime = "ocr"
    ed.refresh_ocr_layer()
    ed.set_mode("resize_objects")
    ed.set_resize_class(RESIZE_CLASS)
    ed.selected_nodes.add(SEL_NODE)
    ed.selected_edges.add(ed.model.edge_key(*SEL_EDGE))
    ed._update_selection_visuals()
    return ed


@pytest.fixture
def armed(editor):
    return _arm(editor)


# ── обстановка ───────────────────────────────────────────────────────────

def test_фикстура_поднимает_все_шесть_слоёв(armed):
    """Без этого замка остальные тесты сравнивали бы ноль с нулём."""
    assert _layers(armed) == ARMED
    assert len(armed.scene.items()) == SCENE_ARMED
    assert armed._resize_sel == RESIZE_SET, "набор «Размеров» не собрался"


def test_переключатель_тем_же_значением_ничего_не_делает(armed):
    """`set_light_theme(True)` при дефолтном `True` — ранний выход (base:593).

    Замок против теста, который выглядит как проверка, но сцену не пересобирает.
    """
    assert armed._light_theme is True, "дефолт темы сдвинулся"
    assert armed._bg_visible is True, "дефолт подложки сдвинулся"

    armed.set_light_theme(True)
    armed.set_background_visible(True)

    assert _layers(armed) == ARMED


# ── слои переживают пересборку ───────────────────────────────────────────

def test_светлый_лист_слои_не_уносит(armed):
    armed.set_light_theme(False)

    assert _layers(armed) == ARMED
    assert len(armed.scene.items()) == SCENE_ARMED


def test_подложка_слои_не_уносит(armed):
    armed.set_background_visible(False)

    assert _layers(armed) == ARMED
    assert len(armed.scene.items()) == SCENE_ARMED - 1      # ушла сама подложка


def test_переключения_подряд_слои_не_копят_и_не_теряют(armed):
    """Идемпотентность: четыре переключения дают ту же картинку, что и одно."""
    armed.set_light_theme(False)
    armed.set_background_visible(False)
    armed.set_light_theme(True)
    armed.set_background_visible(True)

    assert _layers(armed) == ARMED
    assert len(armed.scene.items()) == SCENE_ARMED


def test_флаги_слоёв_переключение_переживают(armed):
    """Данные переживали пересборку и раньше — теперь картинка им отвечает."""
    armed.set_light_theme(False)

    assert armed.show_skins is True
    assert armed.grid_visible is True
    assert armed._resize_sel == RESIZE_SET
    assert armed.selected_nodes == {SEL_NODE}
    assert len(armed.selected_edges) == 1


def test_кнопка_скинов_не_врёт(qapp, data_paths, monkeypatch):
    """Инвариант 1.2: кнопка нажата ⟺ флаг `show_skins` ⟺ скины на сцене."""
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    tab = AdvancedGraphTab("mefx1", "проба блока 1", object())
    ed = _new_editor(data_paths)
    tab._editor = ed
    tab._on_editor_ready()
    try:
        tab.btn_show_skins.setChecked(True)          # путь оператора: нажатие кнопки
        assert _alive(ed, _skin_items(ed)) == ARMED["скины"], "скины не поднялись"

        ed.set_light_theme(False)

        assert tab.btn_show_skins.isChecked() is True
        assert ed.show_skins is True
        assert _alive(ed, _skin_items(ed)) == ARMED["скины"]

        tab.btn_show_skins.setChecked(False)         # и обратно — обе полярности

        assert ed.show_skins is False
        assert _alive(ed, _skin_items(ed)) == 0
    finally:
        tab.cleanup()
        _dispose(ed, qapp)


# ── живые оверлеи: ни одной ссылки на разрушенный объект C++ ────────────

def _poly_editor(tmp_path):
    """Редактор, у которого у узла есть контур — иначе правка полигона не откроется."""
    g = _graph()
    for n in g["nodes"]:
        if n["id"] == "pump_a":
            n["segmentation"] = [200.0, 100.0, 260.0, 100.0,
                                 260.0, 160.0, 200.0, 160.0]
    return _new_editor(_write_data(tmp_path, g))


def test_правка_полигона_переключение_листа_не_ломает(qapp, tmp_path):
    """До правки первый же жест с вершиной падал `RuntimeError` (замер §MEFX1)."""
    ed = _poly_editor(tmp_path)
    try:
        ed._enter_polygon_editing_mode("pump_a")
        assert ed._poly_overlay is not None
        before = list(ed.nodes["pump_a"]["segmentation"])

        ed.set_light_theme(False)

        assert ed._poly_overlay is None, "мёртвый оверлей остался ссылкой"
        assert ed._poly_edit_node is None
        assert list(ed.nodes["pump_a"]["segmentation"]) == before, "контур тронут"

        # Оператор входит в правку заново — и она работает.
        ed._enter_polygon_editing_mode("pump_a")
        idx = ed._poly_overlay.find_vertex_at(200.0, 100.0)
        assert idx == 0, "вершина под курсором не найдена — жест не тот"
        ed._poly_overlay.start_drag(idx)
        ed._poly_overlay.drag_to(210.0, 110.0)
        assert ed._poly_overlay.is_dragging
    finally:
        _dispose(ed, qapp)


def test_ручки_текст_блока_переключение_листа_не_ломает(armed):
    armed._show_ocr_block_resize("b1")
    assert armed._ocr_resize_overlay is not None

    armed.set_light_theme(False)

    assert armed._ocr_resize_overlay is None, "мёртвые ручки остались ссылкой"
    assert armed._ocr_resize_block_id is None

    armed._show_ocr_block_resize("b1")             # показать заново — работает
    assert armed._ocr_resize_overlay is not None
    armed._ocr_resize_overlay.hide()


def test_рамка_добавления_блока_переключение_листа_не_ломает(armed):
    rect = QGraphicsRectItem(QRectF(10.0, 10.0, 20.0, 20.0))
    armed.scene.addItem(rect)
    armed._ocr_add_preview = rect
    armed._ocr_add_start = (10.0, 10.0)

    armed.set_light_theme(False)

    assert armed._ocr_add_preview is None, "мёртвая рамка осталась ссылкой"
    assert armed._ocr_add_start is None
