# -*- coding: utf-8 -*-
"""Э13: два набора легальных пар (§7.1.1 EDITOR_AFTER_LAYOUT_PLAN).

Решателю — прежний набор с допуском близости BORDER_TOL (прижатое к блоку не
расталкивать), судье — строгий (легально только пересечение площадью > 0 в
детектированной геометрии). Приёмка плана: синтетическая псевдо-пара,
наложенная искусственно «после операции», видна строгому судье.
"""
from copy import deepcopy

import pytest

pytest.importorskip("shapely")

from modules.graph.core.layout import _overlaps, _shapes
from modules.graph.core.layout.params import LayoutParams

# Порог решателя в тестах берётся тем же маршрутом, что в проде
# (layout/__init__.py: p.border_tol) и бенче — не через _shapes.BORDER_TOL.
SOLVER_TOL = LayoutParams().border_tol


def test_solver_tol_single_source():
    """_shapes.BORDER_TOL и LayoutParams.border_tol — два литерала одной
    величины; их дрейф при тюнинге (Э7) должен будить тесты, а не молчать."""
    assert SOLVER_TOL == _shapes.BORDER_TOL


def _graph():
    """A — блок с настоящим контуром; B — блок в 1 px от контура A
    (0 < зазор < BORDER_TOL → псевдо-пара, амнистия близости);
    C — блок, реально пересекающий A в детекции (настоящая легальная пара)."""
    return {
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [150.0, 150.0], "bbox": [100, 100, 200, 200],
             "segmentation": [100, 100, 200, 100, 200, 200, 100, 200]},
            {"id": "b", "type": "equipment", "class_name": "armatura",
             "centroid": [150.0, 216.0], "bbox": [201, 140, 231, 160],
             "segmentation": None},
            {"id": "c", "type": "equipment", "class_name": "nasos",
             "centroid": [110.0, 105.0], "bbox": [90, 95, 120, 125],
             "segmentation": None},
        ],
        "links": [],
        "graph": {},
    }


def _det(g):
    return {n["id"]: n["bbox"] for n in g["nodes"]}


def _overlap_b_onto_a(g):
    """«Операция» наложила B на A: сдвиг влево на 15 px."""
    after = deepcopy(g)
    b = after["nodes"][1]
    b["bbox"] = [186, 140, 216, 160]
    b["centroid"] = [150.0, 201.0]
    return after


def test_solver_amnesties_border_pair_judge_does_not():
    g = _graph()
    solver = _shapes.legal_pairs(g, _det(g), SOLVER_TOL)
    strict = _shapes.legal_pairs(g, _det(g), 0.0)
    assert ("a", "b") in solver, "решателю псевдо-пара легальна (BORDER_TOL)"
    assert ("a", "b") not in strict, "судье амнистия близости не положена"
    # Настоящая пара (пересечение площадью > 0 в детекции) легальна обоим.
    assert ("a", "c") in solver
    assert ("a", "c") in strict


def test_judge_sees_pseudo_pair_overlapped_after_operation():
    g = _graph()
    solver = _shapes.legal_pairs(g, _det(g), SOLVER_TOL)
    strict = _shapes.legal_pairs(g, _det(g), 0.0)
    after = _overlap_b_onto_a(g)

    # Латентная дыра до Э13: судья со старым набором пару прощал.
    assert _overlaps.strict_block_pairs(after, solver) == 0
    # Строгий судья видит дефект; настоящая пара a-c по-прежнему легальна.
    assert _overlaps.strict_block_pairs(after, strict) == 1


def test_bench_legal_leak_counts_only_new_overlaps():
    import importlib.util
    from pathlib import Path

    pytest.importorskip("numpy")
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "lb_for_leak_test", repo / "tools" / "layout_bench.py")
    lb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lb)

    g = _graph()
    solver = _shapes.legal_pairs(g, _det(g), SOLVER_TOL)
    strict = _shapes.legal_pairs(g, _det(g), 0.0)
    pseudo = solver - strict
    assert pseudo == {("a", "b")}
    after = _overlap_b_onto_a(g)

    # До операции псевдо-пара не наложена — утечки нет.
    assert lb._legal_leak(g, g, pseudo) == 0
    # Наложена после операции, на входе — нет: утечка.
    assert lb._legal_leak(after, g, pseudo) == 1
    # Метаправило «сравнение с базой входа»: пара, наложенная уже НА ВХОДЕ
    # раскладки, — не утечка слоя.
    assert lb._legal_leak(after, after, pseudo) == 0
