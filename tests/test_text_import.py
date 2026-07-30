# -*- coding: utf-8 -*-
"""Импорт подписей на холст и якоря §3.9.

Проверяется главное утверждение §3.9: блок с якорем сохраняет положение
ОТНОСИТЕЛЬНО СВОЕГО ЭЛЕМЕНТА, а не относительно листа. Абсолютная точка при
этом может уехать далеко — ровно настолько, насколько уехал элемент.

Данных заказчика здесь нет: геометрия синтетическая, подписи дописаны в тесте.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from modules.graph.core import text_import

FIXTURE = Path(__file__).parent / "fixtures" / "layout" / "synth_med.json"


def _validated():
    """Крошечный граф в координатах растра + одна подпись у узла."""
    return {
        "graph": {"image_size": [1000, 2000]},
        "nodes": [
            {"id": "node_1", "type": "equipment", "class_name": "armatura_ruchn",
             "centroid": [100.0, 200.0], "bbox": [180.0, 90.0, 220.0, 110.0]},
            {"id": "node_2", "type": "equipment", "class_name": "armatura_ruchn",
             "centroid": [500.0, 900.0], "bbox": [880.0, 490.0, 920.0, 510.0]},
        ],
        "links": [
            {"id": "edge_1", "source": "node_1", "target": "node_2",
             "source_point": [100.0, 220.0], "target_point": [500.0, 880.0],
             "waypoints": []},
        ],
        # подпись НАД узлом: справа от него идёт труба, и блок у трубы
        # честно получил бы якорь-ребро — здесь проверяется якорь-узел
        "text_blocks": [
            {"id": "block_1", "bbox": [180.0, 40.0, 250.0, 60.0],
             "text": "V-1", "merged_into": None},
        ],
        "bindings": [],
    }


def _moved_canvas(validated, dx=0.0, dy=0.0, node_id="node_1"):
    """Холст = pretransform от графа, затем узел уехал на (dx, dy)."""
    from modules.graph.core.canvas_input import to_canvas

    canvas, _tr = to_canvas(validated)
    for n in canvas["nodes"]:
        if n["id"] == node_id:
            c = n["centroid"]
            n["centroid"] = [c[0] + dy, c[1] + dx]
            bb = n["bbox"]
            n["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
    return canvas


def test_block_follows_its_node_not_the_sheet():
    """Узел уехал на 300 px — подпись обязана уехать с ним, а не остаться."""
    validated = _validated()
    canvas = _moved_canvas(validated, dx=300.0, dy=-120.0)

    from modules.graph.core.canvas_input import to_canvas
    pre, _tr = to_canvas(validated)
    before = pre["text_blocks"][0]["bbox"]

    stats = text_import.import_text(canvas, validated)
    after = canvas["text_blocks"][0]["bbox"]

    assert stats["anchored_node"] == 1, stats
    assert after[0] == pytest.approx(before[0] + 300.0, abs=1e-6)
    assert after[1] == pytest.approx(before[1] - 120.0, abs=1e-6)


def test_block_keeps_size():
    validated = _validated()
    canvas = _moved_canvas(validated, dx=50.0)
    from modules.graph.core.canvas_input import to_canvas
    pre, _tr = to_canvas(validated)
    w0 = pre["text_blocks"][0]["bbox"][2] - pre["text_blocks"][0]["bbox"][0]

    text_import.import_text(canvas, validated)
    bb = canvas["text_blocks"][0]["bbox"]
    assert bb[2] - bb[0] == pytest.approx(w0, abs=1e-6)


def test_far_block_has_no_anchor_and_stays():
    """Штамп и рамка ни к чему не относятся — им «как в оригинале» и правильно."""
    validated = _validated()
    validated["text_blocks"][0]["bbox"] = [1500.0, 900.0, 1700.0, 940.0]
    canvas = _moved_canvas(validated, dx=300.0)

    from modules.graph.core.canvas_input import to_canvas
    pre, _tr = to_canvas(validated)
    before = list(pre["text_blocks"][0]["bbox"])

    stats = text_import.import_text(canvas, validated)
    assert stats["no_anchor"] == 1
    assert canvas["text_blocks"][0]["bbox"] == before


def test_anchor_max_is_the_switch():
    """Тот же блок: с большим порогом якорь есть, с нулевым — нет."""
    validated = _validated()
    validated["text_blocks"][0]["bbox"] = [400.0, 92.0, 470.0, 108.0]
    for anchor_max, expect_anchor in ((1000.0, True), (0.5, False)):
        canvas = _moved_canvas(validated, dx=300.0)
        stats = text_import.import_text(canvas, validated,
                                        anchor_max=anchor_max)
        assert (stats["anchored_node"] + stats["anchored_edge"] == 1) is \
            expect_anchor, (anchor_max, stats)


def test_binding_without_side_is_synthesized_and_block_follows():
    """Привязки из вкладки привязки идут БЕЗ side, а без него производная
    позиция не работает ни для одной привязки корпуса."""
    validated = _validated()
    validated["bindings"] = [
        {"block_id": "block_1", "node_id": "node_1", "kind": "node"},
    ]
    canvas = _moved_canvas(validated, dx=400.0, dy=200.0)

    stats = text_import.import_text(canvas, validated)
    assert stats["bound"] == 1
    binding = canvas["bindings"][0]
    assert binding["side"] in ("top", "right", "left", "bottom")
    assert "gap" in binding

    # блок стоит у своей цели, а не там, где был на листе
    node = next(n for n in canvas["nodes"] if n["id"] == "node_1")
    nb = node["bbox"]
    bb = canvas["text_blocks"][0]["bbox"]
    assert bb[0] >= nb[0] - 200 and bb[2] <= nb[2] + 200


def test_existing_side_is_not_overwritten():
    validated = _validated()
    validated["bindings"] = [
        {"block_id": "block_1", "node_id": "node_1", "kind": "node",
         "side": "top", "gap": 11.0},
    ]
    canvas = _moved_canvas(validated)
    text_import.import_text(canvas, validated)
    assert canvas["bindings"][0]["side"] == "top"
    assert canvas["bindings"][0]["gap"] == 11.0


def test_merged_blocks_are_dropped():
    validated = _validated()
    validated["text_blocks"].append(
        {"id": "block_2", "bbox": [230.0, 92.0, 300.0, 108.0], "text": "V-1",
         "merged_into": "block_1"})
    canvas = _moved_canvas(validated)
    stats = text_import.import_text(canvas, validated)
    assert stats["blocks"] == 1
    assert [b["id"] for b in canvas["text_blocks"]] == ["block_1"]


def test_graph_without_text_gives_empty_lists():
    validated = _validated()
    validated["text_blocks"] = []
    canvas = _moved_canvas(validated)
    stats = text_import.import_text(canvas, validated)
    assert stats["blocks"] == 0
    assert canvas["text_blocks"] == [] and canvas["bindings"] == []


def test_validated_graph_is_not_mutated():
    validated = _validated()
    snapshot = deepcopy(validated)
    canvas = _moved_canvas(validated, dx=100.0)
    text_import.import_text(canvas, validated)
    assert validated == snapshot


def test_import_after_real_layout_keeps_labels_at_their_objects():
    """Сквозная проверка на синтетике: после настоящей раскладки подписи
    остаются у своих элементов, а не у случайных соседей."""
    pytest.importorskip("shapely")
    from modules.graph.core.canvas_input import to_canvas
    from modules.graph.core.layout import LayoutParams, layout

    validated = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # синтетика без текста — вешаем подпись рядом с каждым десятым узлом
    validated["text_blocks"] = []
    for i, n in enumerate(validated["nodes"]):
        if i % 10 or not n.get("bbox"):
            continue
        bb = n["bbox"]
        validated["text_blocks"].append({
            "id": f"block_{i}", "text": n["id"], "merged_into": None,
            "bbox": [bb[2] + 5.0, bb[1], bb[2] + 60.0, bb[1] + 18.0],
        })
    validated["bindings"] = []

    canvas, _tr = to_canvas(validated)
    canvas, _stats = layout(canvas, LayoutParams())
    pre, _tr2 = to_canvas(validated)

    stats = text_import.import_text(canvas, validated)
    assert stats["blocks"] == len(validated["text_blocks"])
    assert stats["anchored_node"] + stats["anchored_edge"] > 0

    # каждая подпись — у того же узла, рядом с которым её поставили
    byid = {n["id"]: n for n in canvas["nodes"]}
    off = 0
    for blk in canvas["text_blocks"]:
        node = byid.get(blk["text"])
        if node is None or not node.get("bbox"):
            continue
        bx = (blk["bbox"][0] + blk["bbox"][2]) / 2.0
        by = (blk["bbox"][1] + blk["bbox"][3]) / 2.0
        nb = node["bbox"]
        d = text_import._pt_rect(bx, by, tuple(nb))
        if d > text_import.ANCHOR_MAX:
            off += 1
    assert off == 0, f"{off} подписей уехали от своих узлов"
