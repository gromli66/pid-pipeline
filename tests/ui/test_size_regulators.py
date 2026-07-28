"""T5/T6 — регуляторы субъективных размеров (П4/П4а): визуал и только визуал.

T5: прогон всех размерных ползунков мин→макс не меняет данные (граф целиком,
    text_blocks и bindings). Эталон снимается ПОСЛЕ первого полного рендера —
    _draw_ocr_block штатно синкает blk['bbox'] у привязок с side, иначе
    сравнение дало бы ложный красный.
T6: настройки переживают пересоздание редактора (вкладка пересоздаёт его при
    каждом открытии), а общий сброс возвращает исходный вид ВКЛЮЧАЯ размерные
    регуляторы — т.е. apply_default_appearance расширен.

UISettings подменяется на память: тест не должен писать в реестр пользователя.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QWidget          # noqa: E402
from PySide6.QtGui import QImage, QColor                     # noqa: E402

IMG_W, IMG_H = 400, 300
UID = "pytest-size-regulators"

# Ползунок ходит 25..400 % (см. AppearanceMixin._add_size_setting).
FACTORS = (0.25, 0.5, 1.0, 2.0, 4.0)


class _MemSettings:
    """Настройки в памяти — вместо QSettings(реестр)."""

    def __init__(self):
        self._d: dict[str, object] = {}

    def _k(self, uid, key):
        return f"appearance/{uid}/{key}"

    def get_appearance(self, uid, key, default):
        val = self._d.get(self._k(uid, key), default)
        try:
            if isinstance(default, bool):
                return bool(val)
            if isinstance(default, float):
                return float(val)
            if isinstance(default, int):
                return int(val)
        except (TypeError, ValueError):
            return default
        return val

    def set_appearance(self, uid, key, value):
        self._d[self._k(uid, key)] = value

    def has_appearance(self, uid, key):
        return self._k(uid, key) in self._d

    def clear_appearance(self, uid):
        prefix = f"appearance/{uid}/"
        for k in [k for k in self._d if k.startswith(prefix)]:
            del self._d[k]


def _synthetic_graph() -> dict:
    nodes = [
        {"id": "eq_box", "type": "equipment", "centroid": [100.0, 100.0],
         "bbox": [70.0, 80.0, 130.0, 120.0], "segmentation": None,
         "class_id": 1, "class_name": "nasos", "degree": 1},
        {"id": "eq_poly", "type": "equipment", "centroid": [100.0, 260.0],
         "bbox": [240.0, 80.0, 280.0, 120.0],
         "segmentation": [240.0, 80.0, 280.0, 80.0, 280.0, 120.0, 240.0, 120.0],
         "class_id": 2, "class_name": "zadvizhka", "degree": 1},
        {"id": "conn_a", "type": "connector", "centroid": [220.0, 100.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 2},
    ]
    links = [
        {"id": "edge_1", "source": "eq_box", "target": "conn_a",
         "source_point": [120.0, 100.0], "target_point": [212.0, 100.0],
         "waypoints": [], "length": 92.0, "straight_line_distance": 92.0},
        {"id": "edge_2", "source": "conn_a", "target": "eq_poly",
         "source_point": [220.0, 108.0], "target_point": [120.0, 252.0],
         "waypoints": [], "length": 160.0, "straight_line_distance": 160.0},
    ]
    text_blocks = [
        {"id": "block_1", "bbox": [60.0, 40.0, 140.0, 60.0], "text": "10LAB10AP001",
         "confidence": 0.9, "source": "ocr", "merged_into": None},
        {"id": "block_2", "bbox": [240.0, 200.0, 300.0, 216.0], "text": "DN100",
         "confidence": 0.8, "source": "ocr", "merged_into": None},
    ]
    bindings = [
        {"block_id": "block_1", "node_id": "eq_box", "kind": "node",
         "text": "10LAB10AP001", "side": "top", "gap": 6.0},
        {"block_id": "block_2", "node_id": "conn_a", "kind": "node",
         "text": "DN100", "side": "right", "gap": 6.0},
    ]
    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [IMG_H, IMG_W]},
        "nodes": nodes, "links": links,
        "text_blocks": text_blocks, "bindings": bindings,
    }


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def data_paths(tmp_path):
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_synthetic_graph()), encoding="utf-8")
    return str(img_path), str(graph_path)


@pytest.fixture
def mem_settings(monkeypatch):
    from ui.services.ui_settings import UISettings

    mem = _MemSettings()
    monkeypatch.setattr(UISettings, "_instance", mem)
    yield mem
    UISettings._instance = None


def _make_editor(qapp, data_paths):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    ed = AdvancedGraphEditor()
    assert ed.load_data(*data_paths)
    ed.display_regime = "ocr"
    ed.refresh_ocr_layer()
    return ed


def _dump(ed) -> str:
    ed.model.graph_data["nodes"] = list(ed.model.nodes.values())
    ed.model.graph_data["links"] = ed.model.edges_data
    ed.model.graph_data["text_blocks"] = ed.model.text_blocks
    ed.model.graph_data["bindings"] = ed.model.bindings
    return json.dumps(ed.model.graph_data, sort_keys=True, ensure_ascii=False)


# ── T5 ───────────────────────────────────────────────────────────────────

def test_t5_slider_sweep_does_not_touch_data(qapp, data_paths):
    """Прогон всех размерных ползунков мин→макс → данные побайтово исходные."""
    ed = _make_editor(qapp, data_paths)
    baseline = _dump(ed)

    for key in ed.SIZE_FACTOR_KEYS:
        for f in FACTORS:
            ed.set_size_factor(key, f)
            assert _dump(ed) == baseline, f"данные изменил ползунок {key}={f}"
    ed.reset_size_factors()
    assert _dump(ed) == baseline


def test_t5_geometry_and_hittest_keys_are_not_factorable(qapp, data_paths):
    """Геометрия, hit-test и толщина трубы не могут попасть под ползунок."""
    ed = _make_editor(qapp, data_paths)
    for key in ("CONNECTOR_MARKER_RADIUS", "CLICK_THRESHOLD", "EDGE_WIDTH"):
        assert key not in ed.SIZE_FACTOR_KEYS
        before = getattr(ed, key)
        ed.set_size_factor(key, 4.0)      # должен быть проигнорирован
        assert getattr(ed, key) == before


def test_t5_factor_is_idempotent_and_survives_mode_switch(qapp, data_paths):
    """Множитель применяется ОТ БАЗЫ (не накапливается) и переживает
    переключение legacy/canvas — иначе setup_scene молча откатил бы ползунок."""
    ed = _make_editor(qapp, data_paths)
    base_legacy = ed.CONNECTOR_DRAW_RADIUS

    ed.set_size_factor("CONNECTOR_DRAW_RADIUS", 2.0)
    doubled = ed.CONNECTOR_DRAW_RADIUS
    assert doubled == base_legacy * 2

    # повторное применение того же фактора не накапливается
    ed.set_size_factor("CONNECTOR_DRAW_RADIUS", 2.0)
    assert ed.CONNECTOR_DRAW_RADIUS == doubled

    # штатный путь перерисовки сцены не сбрасывает ползунок
    ed.setup_scene()
    assert ed.CONNECTOR_DRAW_RADIUS == doubled

    # и в холсте множитель тот же, а база — канвасная
    ed._apply_visuals(canvas=True)
    assert ed.CONNECTOR_DRAW_RADIUS == ed._VIS_CANVAS["CONNECTOR_DRAW_RADIUS"] * 2


def test_t5_ocr_border_regulator_keeps_canvas_scale(qapp, data_paths):
    """Рамка текст-блока крутится ползунком, но множитель _ocr_vis_scale
    сохраняется — иначе в холсте рамка задавит блок ~15x6 px."""
    ed = _make_editor(qapp, data_paths)
    base = ed.OCR_BORDER_W
    ed.set_size_factor("OCR_BORDER_W", 3.0)
    assert ed.OCR_BORDER_W == base * 3
    ed._canvas_mode = True
    ed._bg_scale = 0.25
    assert ed._ocr_vis_scale() == 0.25


# ── T6 ───────────────────────────────────────────────────────────────────

@pytest.fixture
def tab_cls():
    """Минимальная вкладка на РЕАЛЬНЫХ методах BaseGraphTab.

    Вкладку целиком не поднять (в __init__ — сеть и тулбар), но T6 стережёт
    именно трёхчастный контракт AppearanceMixin, а он живёт в этих методах.
    """
    from ui.tabs.base_graph_tab import BaseGraphTab

    class _Tab(BaseGraphTab):
        def __init__(self, uid, editor):
            QWidget.__init__(self)
            self.uid = uid
            self._editor = editor
            self._appearance_panel = None

        def _create_editor(self):
            return self._editor

        def _setup_toolbar(self, toolbar):
            return

    return _Tab


def test_t6_saved_sizes_survive_editor_recreation(qapp, data_paths, mem_settings,
                                                  tab_cls):
    """Настройки переживают пересоздание редактора (= переоткрытие вкладки)."""
    ed1 = _make_editor(qapp, data_paths)
    tab1 = tab_cls(UID, ed1)
    # «оператор подвигал ползунки»
    mem_settings.set_appearance(UID, "size_connector", 250.0)
    mem_settings.set_appearance(UID, "size_outline", 300.0)
    mem_settings.set_appearance(UID, "size_ocr_border", 50.0)
    tab1.apply_saved_appearance()
    expect = (ed1.CONNECTOR_DRAW_RADIUS, ed1.OUTLINE_WIDTH)

    # вкладка закрылась и открылась заново — новый редактор, те же настройки
    ed2 = _make_editor(qapp, data_paths)
    tab2 = tab_cls(UID, ed2)
    tab2.apply_saved_appearance()
    assert (ed2.CONNECTOR_DRAW_RADIUS, ed2.OUTLINE_WIDTH) == expect
    assert ed2.CONNECTOR_DRAW_RADIUS == ed2._vis_base["CONNECTOR_DRAW_RADIUS"] * 2.5


def test_t6_reset_restores_default_sizes(qapp, data_paths, mem_settings, tab_cls):
    """Общий сброс возвращает исходный вид, включая размерные регуляторы."""
    ed = _make_editor(qapp, data_paths)
    tab = tab_cls(UID, ed)
    defaults = (ed.CONNECTOR_DRAW_RADIUS, ed.OUTLINE_WIDTH, ed.OCR_BORDER_W)

    ed.set_size_factor("CONNECTOR_DRAW_RADIUS", 3.0)
    ed.set_size_factor("OUTLINE_WIDTH", 3.0)
    ed.set_size_factor("OCR_BORDER_W", 3.0)
    assert (ed.CONNECTOR_DRAW_RADIUS, ed.OUTLINE_WIDTH, ed.OCR_BORDER_W) != defaults

    tab.apply_default_appearance()
    assert (ed.CONNECTOR_DRAW_RADIUS, ed.OUTLINE_WIDTH, ed.OCR_BORDER_W) == defaults


def test_t6_reset_appearance_clears_stored_sizes(qapp, data_paths, mem_settings,
                                                 tab_cls):
    """reset_appearance стирает и сохранённые размеры (ключи per-uid плоские)."""
    ed = _make_editor(qapp, data_paths)
    tab = tab_cls(UID, ed)
    mem_settings.set_appearance(UID, "size_connector", 250.0)
    tab.reset_appearance()
    assert not mem_settings.has_appearance(UID, "size_connector")


def test_t6_panel_sliders_are_built_and_wired(qapp, data_paths, mem_settings,
                                              tab_cls):
    """Размерные ползунки реально появляются в шестерёнке и двигают редактор."""
    from PySide6.QtWidgets import QSlider, QLabel
    from ui.widgets.appearance_panel import AppearancePanel

    ed = _make_editor(qapp, data_paths)
    tab = tab_cls(UID, ed)
    panel = AppearancePanel(tab)
    tab._build_appearance_controls(panel)

    titles = {lbl.text().split(":")[0] for lbl in panel.findChildren(QLabel)}
    assert "Размер коннекторов" in titles
    assert "Толщина рамки боксов" in titles

    base = ed.CONNECTOR_DRAW_RADIUS
    # подвинуть первый размерный ползунок (25..400) — редактор обязан отреагировать
    size_sliders = [s for s in panel.findChildren(QSlider)
                    if (s.minimum(), s.maximum()) == (25, 400)]
    assert len(size_sliders) >= 2
    size_sliders[0].setValue(200)
    assert ed.CONNECTOR_DRAW_RADIUS == base * 2
    assert mem_settings.has_appearance(UID, "size_connector")


def test_t6_ocr_binding_tab_contract(qapp, tmp_path, mem_settings):
    """Зеркало T6 для «Привязки подписей»: сеттеры есть, сброс возвращает базу."""
    from ui.editors.ocr_binding_editor import OcrBindingEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    p = tmp_path / "raster.png"
    img.save(str(p))

    ed = OcrBindingEditor()
    ed.load_data(str(p), [{"bbox": [10.0, 10.0, 60.0, 26.0], "text": "V1",
                           "confidence": 0.9}],
                 {"nodes": [{"id": "n1", "type": "equipment",
                             "centroid": [50.0, 50.0], "bbox": None}],
                  "links": []}, [])
    base = (ed.NODE_DRAW_RADIUS, ed.OCR_BORDER_WIDTH, ed._ocr_bound_border_w)
    geom_before = ed.NODE_RADIUS

    ed.set_node_size_factor(3.0)
    ed.set_text_border_factor(0.5)
    assert (ed.NODE_DRAW_RADIUS, ed.OCR_BORDER_WIDTH, ed._ocr_bound_border_w) != base
    assert ed.NODE_RADIUS == geom_before, "ползунок тронул геометрию привязки"

    ed.set_node_size_factor(1.0)
    ed.set_text_border_factor(1.0)
    assert (ed.NODE_DRAW_RADIUS, ed.OCR_BORDER_WIDTH, ed._ocr_bound_border_w) == base
