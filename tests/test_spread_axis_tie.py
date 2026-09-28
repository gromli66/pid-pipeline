# -*- coding: utf-8 -*-
"""Ничья допуска соосности в `spread.pts_orient` не переворачивается шумом.

Дефект (MEASUREMENTS §130.1, блок 2 модели точек прикрепления): ребро,
нарисованное длиной ровно `AXIS_TOL` = 1 px, после перехода рамки посадки на
bbox выходило то 1.0, то 1.0000000000000568. Сравнение `dx <= tol` отдавало
в первом случае 'P', во втором 'H', и раскладка расходилась дальше
(0fc9d04c edge_375, 06b938ec edge_91: +1 дефект на каждом). Лечение — запас
`AXIS_EPS` на шум, смысл порога прежний: не длиннее 1 px — соосно.
"""
from __future__ import annotations

import pytest

from modules.graph.core.layout import spread

TOL = spread.AXIS_TOL
NOISE = (-1e-13, 0.0, 1e-13, 5.68e-14)   # 5.68e-14 — шум edge_375 (0fc9d04c)


def _edge(x1, y1, x2, y2):
    # точки ребра хранятся [y, x]
    return {"source": "a", "target": "b",
            "source_point": [y1, x1], "target_point": [y2, x2]}


@pytest.mark.parametrize("x0", [0.0, 812.0])
def test_one_px_edge_decides_the_same_under_noise(x0):
    """Короткое ребро длиной AXIS_TOL ± 1e-13: одно решение, и это 'P'."""
    got = {spread.pts_orient(_edge(x0, 500.0, x0 + TOL + n, 500.0)) for n in NOISE}
    assert got == {"P"}
    got = {spread.pts_orient(_edge(300.0, x0, 300.0, x0 + TOL + n)) for n in NOISE}
    assert got == {"P"}


@pytest.mark.parametrize("x0", [0.0, 812.0])
def test_skew_of_one_px_keeps_the_axis_under_noise(x0):
    """Длинное ребро со скосом ровно AXIS_TOL ± 1e-13: одна ось."""
    got = {spread.pts_orient(_edge(x0, 500.0, x0 + 40.0, 500.0 + TOL + n))
           for n in NOISE}
    assert got == {"H"}
    got = {spread.pts_orient(_edge(300.0, x0, 300.0 + TOL + n, x0 + 40.0))
           for n in NOISE}
    assert got == {"V"}


def test_threshold_meaning_is_unchanged():
    """Запас — на шум, а не на пиксели: 1.01 px уже не соосно."""
    assert spread.pts_orient(_edge(0.0, 0.0, 0.99, 0.0)) == "P"
    assert spread.pts_orient(_edge(0.0, 0.0, 1.01, 0.0)) == "H"
    assert spread.pts_orient(_edge(0.0, 0.0, 0.0, 1.01)) == "V"
    assert spread.pts_orient(_edge(0.0, 0.0, 40.0, 1.01)) == "D"


def test_move_axis_falls_back_the_same_way_under_noise():
    """Сквозь `move_axis`: центроиды дают диагональ, решают нарисованные точки."""
    byid = {"a": {"id": "a", "centroid": [500.0, 800.0]},
            "b": {"id": "b", "centroid": [520.0, 830.0]}}
    got = {spread.move_axis(_edge(800.0, 500.0, 800.0 + TOL + n, 500.0),
                            byid, byid, want_source=True) for n in NOISE}
    assert len(got) == 1, got
