"""T1/T2/T11 — контракт П0: нарисованный радиус коннектора не трогает данные.

CONNECTOR_DRAW_RADIUS (кружок на экране, крутится ползунком «размер
коннекторов») и CONNECTOR_MARKER_RADIUS (виртуальный bbox коннектора) —
разные величины. Всё, что уходит в FXML (source_point/target_point), сторона
ребра (_src_side/_tgt_side) и геометрия ОКР-привязки обязаны зависеть только
от второго.

Уровень — offscreen (шаблон tests/ui/test_bundled_fonts.py): _get_node_bbox это
метод QGraphicsView, «геометрия без Qt» невозможна. Данные синтетические:
в корпусе storage/ привязок нет ни одной, а equipment без валидного bbox
(фолбэк на радиус) встречается редко.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 400, 300

# Прогон «ползунка»: от почти нуля до заведомо больше геометрического (8).
DRAW_RADII = (0.5, 2.0, 4.0, 8.0, 20.0, 60.0)
OUTLINE_WIDTHS = (0.5, 1.0, 2.0, 6.0)


def _synthetic_graph() -> dict:
    """Граф из 4 узлов: бокс, equipment БЕЗ bbox (фолбэк на радиус), 2 коннектора.

    centroid — [y, x], source_point/target_point — [y, x] (CODING_GUIDE §6).
    """
    nodes = [
        {"id": "eq_box", "type": "equipment", "centroid": [100.0, 100.0],
         "bbox": [70.0, 80.0, 130.0, 120.0], "segmentation": None,
         "class_id": 1, "class_name": "nasos", "degree": 1},
        # equipment без валидного bbox → _get_node_bbox уходит на фолбэк-радиус
        {"id": "eq_nobbox", "type": "equipment", "centroid": [100.0, 260.0],
         "bbox": None, "segmentation": None,
         "class_id": 2, "class_name": "zadvizhka", "degree": 1},
        {"id": "conn_a", "type": "connector", "centroid": [220.0, 100.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 2},
        {"id": "conn_b", "type": "connector", "centroid": [220.0, 260.0],
         "bbox": None, "segmentation": None,
         "class_id": -1, "class_name": "connector", "degree": 2},
    ]
    links = [
        {"id": "edge_1", "source": "eq_box", "target": "conn_a",
         "source_point": [120.0, 100.0], "target_point": [212.0, 100.0],
         "waypoints": [], "length": 92.0, "straight_line_distance": 92.0},
        {"id": "edge_2", "source": "conn_a", "target": "conn_b",
         "source_point": [220.0, 108.0], "target_point": [220.0, 252.0],
         "waypoints": [], "length": 144.0, "straight_line_distance": 144.0},
        {"id": "edge_3", "source": "conn_b", "target": "eq_nobbox",
         "source_point": [212.0, 260.0], "target_point": [108.0, 260.0],
         "waypoints": [], "length": 104.0, "straight_line_distance": 104.0},
    ]
    text_blocks = [
        {"id": "block_1", "bbox": [60.0, 40.0, 140.0, 60.0], "text": "10LAB10AP001",
         "confidence": 0.9, "source": "ocr", "merged_into": None},
        {"id": "block_2", "bbox": [240.0, 200.0, 300.0, 216.0], "text": "DN100",
         "confidence": 0.8, "source": "ocr", "merged_into": None},
        {"id": "block_3", "bbox": [40.0, 250.0, 90.0, 266.0], "text": "V01",
         "confidence": 0.7, "source": "ocr", "merged_into": None},
    ]
    bindings = [
        {"block_id": "block_1", "node_id": "eq_box", "kind": "node",
         "text": "10LAB10AP001", "side": "top", "gap": 6.0},
        {"block_id": "block_2", "node_id": "conn_a", "kind": "node",
         "text": "DN100", "side": "right", "gap": 6.0},
        # привязка к equipment без bbox — цель у неё виртуальная, от радиуса
        {"block_id": "block_3", "node_id": "eq_nobbox", "kind": "node",
         "text": "V01", "side": "left", "gap": 6.0},
    ]
    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [IMG_H, IMG_W], "num_nodes": len(nodes),
                  "num_edges": len(links)},
        "nodes": nodes, "links": links,
        "text_blocks": text_blocks, "bindings": bindings,
    }


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(qapp, tmp_path):
    """AdvancedGraphEditor со синтетическим графом, после полного рендера.

    Advanced — потому что T2 нужен OcrLayerMixin (привязки), которого нет в Base.
    """
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_synthetic_graph()), encoding="utf-8")

    ed = AdvancedGraphEditor()
    assert ed.load_data(str(img_path), str(graph_path))
    # Эталон снимаем ПОСЛЕ первого полного рендера: _draw_ocr_block штатно
    # синкает blk['bbox'] у привязок с side (ocr_layer_mixin:129-139).
    ed.display_regime = "ocr"
    ed.refresh_ocr_layer()
    return ed


def _settle(ed):
    """Пересчитать всё, что вообще может зависеть от радиуса, и вернуть слепок.

    Возвращается (сохранённый JSON целиком, стороны рёбер, стороны привязок).
    """
    for e in list(ed.edges_data):
        ed._recalculate_edge(e)
    for b in list(ed.model.bindings):
        blk = ed.model.find_text_block(b.get("block_id"))
        if blk is not None:
            ed._attach_binding_position(b, blk)
    ed.refresh_ocr_layer()

    sides = {e["id"]: (e.get("_src_side"), e.get("_tgt_side")) for e in ed.edges_data}
    bind_sides = {b["block_id"]: b.get("side") for b in ed.model.bindings}
    ed.model.graph_data["nodes"] = list(ed.model.nodes.values())
    ed.model.graph_data["links"] = ed.model.edges_data
    ed.model.graph_data["text_blocks"] = ed.model.text_blocks
    ed.model.graph_data["bindings"] = ed.model.bindings
    dump = json.dumps(ed.model.graph_data, sort_keys=True, ensure_ascii=False)
    return dump, sides, bind_sides


# ── T1 ───────────────────────────────────────────────────────────────────

def test_t1_node_bbox_independent_of_draw_radius(editor):
    """_get_node_bbox не зависит от нарисованного радиуса — ни у коннекторов,
    ни у equipment без валидного bbox (фолбэк base_graph_editor:740)."""
    baseline = {nid: list(editor._get_node_bbox(nid)) for nid in editor.nodes}

    for r in DRAW_RADII:
        editor.CONNECTOR_DRAW_RADIUS = r
        editor._redraw_all()
        got = {nid: list(editor._get_node_bbox(nid)) for nid in editor.nodes}
        assert got == baseline, f"_get_node_bbox поехал при CONNECTOR_DRAW_RADIUS={r}"


def test_t1_equipment_without_bbox_uses_geometric_radius(editor):
    """Фолбэк для equipment без bbox сидит на ГЕОМЕТРИЧЕСКОМ радиусе."""
    editor.CONNECTOR_DRAW_RADIUS = 40.0
    r = editor.CONNECTOR_MARKER_RADIUS
    cy, cx = editor.nodes["eq_nobbox"]["centroid"]
    assert editor._get_node_bbox("eq_nobbox") == [cx - r, cy - r, cx + r, cy + r]


def test_t1_draw_radius_present_in_canvas_map():
    """Новый ключ обязан быть и в _VIS_KEYS, и в _VIS_CANVAS.

    _apply_visuals читает self._VIS_CANVAS[key] без .get — ключ только в
    _VIS_KEYS даёт KeyError при входе в canvas-режим.
    """
    from ui.editors.base_graph_editor import BaseGraphEditor

    assert "CONNECTOR_DRAW_RADIUS" in BaseGraphEditor._VIS_KEYS
    for key in BaseGraphEditor._VIS_KEYS:
        assert key in BaseGraphEditor._VIS_CANVAS, f"{key} нет в _VIS_CANVAS"


def test_t1_apply_visuals_keeps_geometry_mode_dependent(editor):
    """Геометрический радиус остаётся режимозависимым (8 растр / 4.0 холст).

    «Фиксированный» ≠ одна абсолютная константа: сравняв режимы, мы поменяли бы
    посадку рёбер в WYSIWYG относительно сегодняшнего поведения.
    """
    editor._apply_visuals(canvas=False)
    assert editor.CONNECTOR_MARKER_RADIUS == 8
    editor._apply_visuals(canvas=True)
    assert editor.CONNECTOR_MARKER_RADIUS == 4.0
    assert editor.CONNECTOR_DRAW_RADIUS == 4.0
    editor._apply_visuals(canvas=False)


# ── T2 ───────────────────────────────────────────────────────────────────

def test_t2_saved_json_and_sides_survive_slider_sweep(editor):
    """Прогон ползунков → сохранённый граф целиком, стороны рёбер и стороны
    привязок побайтово те же."""
    base_dump, base_sides, base_bind = _settle(editor)

    for r in DRAW_RADII:
        for w in OUTLINE_WIDTHS:
            editor.CONNECTOR_DRAW_RADIUS = r
            editor.OUTLINE_WIDTH = w
            editor._redraw_all()
            dump, sides, bind = _settle(editor)
            assert sides == base_sides, f"_src/_tgt_side поехали при r={r}, w={w}"
            assert bind == base_bind, f"сторона привязки поехала при r={r}, w={w}"
            assert dump == base_dump, f"сохранённый граф изменился при r={r}, w={w}"


def test_t2_binding_target_bbox_independent_of_draw_radius(editor):
    """Цель привязки (_binding_target_bbox → _get_node_bbox) не зависит от
    нарисованного радиуса — это то, из чего считается side и производный bbox
    текст-блока, уходящий в <Text> FXML."""
    baseline = {b["block_id"]: editor._binding_target_bbox(b)
                for b in editor.model.bindings}
    for r in DRAW_RADII:
        editor.CONNECTOR_DRAW_RADIUS = r
        got = {b["block_id"]: editor._binding_target_bbox(b)
               for b in editor.model.bindings}
        assert got == baseline, f"цель привязки поехала при CONNECTOR_DRAW_RADIUS={r}"


# ── T11 ──────────────────────────────────────────────────────────────────

def test_t11_canvas_edge_width_matches_fxml_stroke():
    """Равенство «толщина трубы в холсте == LINE_STROKE_WIDTH» держится только
    на комментарии в base_graph_editor — страхуем тестом."""
    from ui.editors.base_graph_editor import BaseGraphEditor
    from modules.graph_to_fxml import LINE_STROKE_WIDTH

    assert BaseGraphEditor._VIS_CANVAS["EDGE_WIDTH"] == LINE_STROKE_WIDTH
