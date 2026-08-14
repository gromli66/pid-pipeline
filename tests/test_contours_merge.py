# -*- coding: utf-8 -*-
"""Влив validated-контуров при построении холста (contours_merge).

Ключевой инвариант — свежесть: влив идёт в КОПИЮ графа, штамп считается от
исходного validated, поэтому холст с влитыми контурами НЕ должен объявляться
устаревшим (sha-проекция включает segmentation — ловушка, ради которой тест
и написан). Отдельно проверяется контурная свежесть (contours_merged_sha).
"""
import copy
import json

from modules.graph.core import canvas_state
from modules.graph.core.canvas_input import to_canvas
from modules.graph.core.contours_merge import (
    contours_are_stale,
    contours_projection_sha,
    load_validated_contours,
    merge_validated_contours,
    stamp_contours,
)


def _validated_graph() -> dict:
    """Мини-граф в координатах растра 3000x2000 (w x h)."""
    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [2000, 3000]},
        "nodes": [
            # скиновый класс: FIXED_SIZES снимет контур на холсте
            {"id": "n1", "type": "equipment", "class_name": "nasos",
             "centroid": [950.0, 1050.0], "bbox": [1000.0, 900.0, 1100.0, 1000.0]},
            # полигонный класс: контур обязан доехать до холста
            {"id": "n2", "type": "equipment", "class_name": "bak",
             "centroid": [700.0, 2150.0], "bbox": [2000.0, 500.0, 2300.0, 900.0]},
            {"id": "n3", "type": "connector", "centroid": [700.0, 1500.0],
             "bbox": None},
        ],
        "links": [],
        "text_blocks": [], "bindings": [],
    }


def _contour_nodes() -> list:
    return [
        # точное совпадение с bbox n2 (COCO [x, y, w, h]) -> IoU 1.0
        {"ann_id": 7, "bbox": [2000.0, 500.0, 300.0, 400.0],
         "polygon_validated": [2000.0, 500.0, 2300.0, 500.0,
                               2300.0, 900.0, 2050.0, 880.0]},
        # шум далеко от всех узлов — не должен пришиться никому
        {"ann_id": 8, "bbox": [10.0, 10.0, 50.0, 50.0],
         "polygon_validated": [10.0, 10.0, 60.0, 10.0, 60.0, 60.0]},
    ]


# ─────────────────────────── merge ───────────────────────────

def test_merge_by_iou_writes_flat_segmentation():
    g = _validated_graph()
    merged = merge_validated_contours(g, _contour_nodes())
    assert merged == 1
    n2 = next(n for n in g["nodes"] if n["id"] == "n2")
    assert n2["segmentation"][:4] == [2000.0, 500.0, 2300.0, 500.0]
    # n1 (низкий IoU с шумом) и connector не тронуты
    n1 = next(n for n in g["nodes"] if n["id"] == "n1")
    assert "segmentation" not in n1


def test_merge_ignores_non_equipment_even_on_match():
    g = _validated_graph()
    for n in g["nodes"]:
        if n["id"] == "n3":
            n["bbox"] = [2000.0, 500.0, 2300.0, 900.0]  # тот же bbox, что у n2
    merge_validated_contours(g, _contour_nodes())
    n3 = next(n for n in g["nodes"] if n["id"] == "n3")
    assert "segmentation" not in n3


def test_merge_flattens_nested_polygon():
    g = _validated_graph()
    nodes = _contour_nodes()
    nodes[0]["polygon_validated"] = [[2000.0, 500.0, 2300.0, 500.0,
                                      2300.0, 900.0]]
    merge_validated_contours(g, nodes)
    n2 = next(n for n in g["nodes"] if n["id"] == "n2")
    assert n2["segmentation"] == [2000.0, 500.0, 2300.0, 500.0, 2300.0, 900.0]


def test_load_validated_contours(tmp_path):
    p = tmp_path / "contours_validated.json"
    p.write_text(json.dumps({"nodes": [
        {"ann_id": 1, "polygon_validated": [1, 2, 3, 4, 5, 6]},
        {"ann_id": 2, "polygon_validated": None},   # не выбран оператором
        {"ann_id": 3},
    ]}), encoding="utf-8")
    nodes = load_validated_contours(p)
    assert [n["ann_id"] for n in nodes] == [1]
    assert load_validated_contours(tmp_path / "нет.json") == []
    assert load_validated_contours(None) == []


# ─────────────────────────── sha и свежесть ───────────────────────────

def test_projection_sha_order_invariant_and_content_sensitive():
    a, b = _contour_nodes()
    assert contours_projection_sha([a, b]) == contours_projection_sha([b, a])
    # 1 и 1.0 — один канон (JSON-обход файла не меняет sha)
    a_int = copy.deepcopy(a)
    a_int["bbox"] = [2000, 500, 300, 400]
    assert contours_projection_sha([a, b]) == contours_projection_sha([a_int, b])
    changed = copy.deepcopy(a)
    changed["polygon_validated"][0] += 5.0
    assert contours_projection_sha([a, b]) != contours_projection_sha([changed, b])


def test_freshness_invariant_after_merge():
    """Влив в копию + штамп от исходного => холст НЕ устаревает."""
    source = _validated_graph()
    nodes = _contour_nodes()

    merged_src = copy.deepcopy(source)
    merge_validated_contours(merged_src, nodes)
    canvas, _transform = to_canvas(merged_src)
    canvas_state.stamp(canvas, source, layout_applied=True)
    stamp_contours(canvas, nodes)

    assert canvas_state.is_stale(canvas, source) == (False, None)
    assert contours_are_stale(canvas, nodes) == (False, None)
    # переигранные контуры → устарел
    changed = copy.deepcopy(nodes)
    changed[0]["polygon_validated"][1] += 3.0
    assert contours_are_stale(canvas, changed)[0] is True


def test_stale_semantics_for_legacy_canvas_without_mark():
    """Старый холст (до механизма): без выбранных контуров не дёргаем."""
    canvas = {"graph": {"canvas_transform": {"layout_version": "x"}}}
    assert contours_are_stale(canvas, []) == (False, None)
    stale, reason = contours_are_stale(canvas, _contour_nodes())
    assert stale is True and "до влива" in reason


def test_stamp_with_empty_contours_marks_canvas():
    canvas = {"graph": {}}
    stamp_contours(canvas, [])
    assert contours_are_stale(canvas, []) == (False, None)


def test_export_gate_rejects_canvas_with_stale_contours(tmp_path):
    """Гейт экспорта FXML: холст со свежей геометрией, но переигранными
    контурами не экспортируется (иначе FXML молча уйдёт без полигонов)."""
    import pytest
    try:
        from worker.tasks.graph import _canvas_is_fresh
    except ImportError as exc:   # окружение без celery — как test_seat_contract
        pytest.skip(f"worker.tasks.graph неимпортируем: {exc!r}")

    source = _validated_graph()
    nodes = _contour_nodes()
    merged_src = copy.deepcopy(source)
    merge_validated_contours(merged_src, nodes)
    canvas, _t = to_canvas(merged_src)
    canvas_state.stamp(canvas, source, layout_applied=True)
    stamp_contours(canvas, nodes)

    canvas_path = tmp_path / "graph_canvas.json"
    validated_path = tmp_path / "graph_validated.json"
    contours_path = tmp_path / "contours_validated.json"
    canvas_path.write_text(json.dumps(canvas), encoding="utf-8")
    validated_path.write_text(json.dumps(source), encoding="utf-8")
    contours_path.write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8")

    assert _canvas_is_fresh(canvas_path, validated_path, "uid",
                            contours_path=contours_path) is True

    changed = copy.deepcopy(nodes)
    changed[0]["polygon_validated"][0] += 4.0
    contours_path.write_text(
        json.dumps({"nodes": changed}), encoding="utf-8")
    assert _canvas_is_fresh(canvas_path, validated_path, "uid",
                            contours_path=contours_path) is False


# ─────────────────────────── интеграция с холстом ───────────────────────────

def test_to_canvas_scales_merged_polygon_and_strips_skin_contour():
    source = _validated_graph()
    nodes = _contour_nodes()
    # nasos тоже получает контур (полное совпадение bbox) — но словарному
    # классу apply_fixed_sizes обязан его снять: одна форма у узла.
    nodes.append({"ann_id": 9, "bbox": [1000.0, 900.0, 100.0, 100.0],
                  "polygon_validated": [1000.0, 900.0, 1100.0, 900.0,
                                        1100.0, 1000.0]})

    merged_src = copy.deepcopy(source)
    assert merge_validated_contours(merged_src, nodes) == 2
    canvas, transform = to_canvas(merged_src)

    s, offx, offy = transform["s"], transform["offx"], transform["offy"]
    n2 = next(n for n in canvas["nodes"] if n["id"] == "n2")
    assert n2["segmentation"][0] == 2000.0 * s + offx
    assert n2["segmentation"][1] == 500.0 * s + offy

    n1 = next(n for n in canvas["nodes"] if n["id"] == "n1")
    assert not n1.get("segmentation"), \
        "скиновый класс обязан потерять контур (одна форма — словарная)"
