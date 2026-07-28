"""T12 — контракт П4а: нарисованный радиус узла в «Привязке подписей» не
трогает данные привязки.

`OcrBindingEditor` не наследник BaseGraphEditor, поэтому П0 его не покрывает, а
дефект тот же: NODE_RADIUS был одновременно кружком на экране и целевым
прямоугольником узла без bbox (_node_target_bbox) — а из него считаются сторона
привязки (_bind_side_of) и block["bbox"] (_auto_bind_bbox), который уходит в
артефакт ocr_binding и дальше в <Text> FXML.

Важное отличие от T5: мутация данных происходит НЕ при движении ползунка, а
при следующей операции привязки. Поэтому тест обязан привязывать заново после
каждого значения ползунка.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 400, 300

DRAW_RADII = (1.0, 3.0, 7.0, 18.0, 45.0)


def _graph_data() -> dict:
    """Узел с bbox и узел БЕЗ bbox (у него цель привязки = центроид ± радиус)."""
    return {
        "nodes": [
            {"id": "eq_box", "type": "equipment", "centroid": [100.0, 100.0],
             "bbox": [70.0, 80.0, 130.0, 120.0], "class_name": "nasos"},
            {"id": "eq_nobbox", "type": "equipment", "centroid": [200.0, 260.0],
             "bbox": None, "class_name": "zadvizhka"},
            {"id": "conn_a", "type": "connector", "centroid": [100.0, 260.0],
             "bbox": None, "class_name": "connector"},
        ],
        "links": [
            {"id": "edge_1", "source": "eq_box", "target": "conn_a",
             "source_point": [120.0, 100.0], "target_point": [100.0, 252.0],
             "waypoints": []},
        ],
    }


def _blocks() -> list[dict]:
    return [
        {"bbox": [60.0, 30.0, 140.0, 50.0], "text": "10LAB10AP001", "confidence": 0.9},
        {"bbox": [300.0, 250.0, 360.0, 268.0], "text": "V01", "confidence": 0.8},
        {"bbox": [40.0, 240.0, 90.0, 258.0], "text": "DN100", "confidence": 0.7},
    ]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(qapp, tmp_path):
    from ui.editors.ocr_binding_editor import OcrBindingEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    img_path = tmp_path / "raster.png"
    img.save(str(img_path))

    ed = OcrBindingEditor()
    ed.load_data(str(img_path), _blocks(), _graph_data(), [])
    assert ed._data_loaded
    return ed


# Что к чему привязываем: (индекс блока, id узла).
CASES = ((0, "eq_box"), (1, "eq_nobbox"), (2, "conn_a"))


def _bind_round(ed) -> dict:
    """Привязать все блоки заново и снять результат (сторона + bbox блока)."""
    ed._ocr_blocks = _blocks()
    ed._bindings = []
    out = {}
    for idx, nid in CASES:
        origin = list(ed._ocr_blocks[idx]["bbox"])
        node = ed._find_node(nid)
        tb = ed._node_target_bbox(node)
        side = ed._bind_side_of(origin, tb) if tb else None
        ed._bind_to_node(idx, nid)
        out[nid] = (side, list(ed._ocr_blocks[idx]["bbox"]))
    return out


def test_t12_binding_side_and_bbox_survive_slider_sweep(editor):
    """Прогон ползунка «размер узлов» → сторона и bbox привязки не меняются
    ПОСЛЕ следующей операции привязки."""
    baseline = _bind_round(editor)

    for r in DRAW_RADII:
        editor.NODE_DRAW_RADIUS = r
        editor._draw_graph_nodes()
        got = _bind_round(editor)
        assert got == baseline, f"привязка поехала при NODE_DRAW_RADIUS={r}"


def test_t12_node_target_bbox_uses_geometric_radius(editor):
    """Цель привязки узла без bbox считается от ГЕОМЕТРИЧЕСКОГО радиуса."""
    editor.NODE_DRAW_RADIUS = 45.0
    node = editor._find_node("eq_nobbox")
    cx, cy = editor._node_center(node)
    r = editor.NODE_RADIUS
    assert editor._node_target_bbox(node) == [cx - r, cy - r, cx + r, cy + r]


def test_t12_real_bbox_target_untouched(editor):
    """У узла с реальным bbox цель — сам bbox, радиус вообще ни при чём."""
    node = editor._find_node("eq_box")
    for r in DRAW_RADII:
        editor.NODE_DRAW_RADIUS = r
        assert editor._node_target_bbox(node) == [70.0, 80.0, 130.0, 120.0]
