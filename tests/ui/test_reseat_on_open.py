# -*- coding: utf-8 -*-
"""§8.3.1 EDITOR_AFTER_LAYOUT_PLAN: посадка концов рёбер чинится при открытии
холста (страховка от смеси контрактов посадки до Э1).

Канон — `seating`: конец у коннектора = центроид, жёстко. На каноничном
холсте починка — no-op (файл не перезаписывается).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")


def _canvas(tmp_path, target_point):
    g = {
        "directed": False,
        "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [100.0, 100.0], "bbox": [80, 80, 120, 120]},
            {"id": "b", "type": "connector", "class_name": "connector",
             "centroid": [100.0, 300.0], "bbox": None},
        ],
        "links": [{"id": "e1", "source": "a", "target": "b",
                   "source_point": [100.0, 120.0],
                   "target_point": target_point, "waypoints": []}],
        "text_blocks": [],
        "bindings": [],
    }
    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    return p


def test_off_canon_connector_end_is_reseated_to_centroid(tmp_path):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])   # конец в 4 px от центроида
    assert _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    assert g["links"][0]["target_point"] == [100.0, 300.0]
    # Каноничный конец у оборудования не тронут.
    assert g["links"][0]["source_point"] == [100.0, 120.0]


def test_canonical_canvas_is_noop(tmp_path):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])
    assert _reseat_canvas_endpoints(p)       # первая починка
    before = p.read_text(encoding="utf-8")
    assert not _reseat_canvas_endpoints(p)   # идемпотентно
    assert p.read_text(encoding="utf-8") == before


def test_broken_file_is_skipped(tmp_path):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text("{оборванный", encoding="utf-8")
    assert not _reseat_canvas_endpoints(p)
