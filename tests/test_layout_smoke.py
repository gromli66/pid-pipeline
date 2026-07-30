# -*- coding: utf-8 -*-
"""Смоук авто-раскладки: отрабатывает, инвариантов не ломает, считает секунды.

Данных заказчика здесь нет. Фикстура `fixtures/layout/synth_med.json` —
синтетическая схема (208 узлов / 239 рёбер), сгенерированная детерминированно
(фиксированный seed) генератором `_scratch/layout_align/synthetic/gen_synth.py`:
горизонтальные магистрали, вертикальные стояки, арматура на трубах, гребёнки
параллельной арматуры, крупные аппараты, джиттер растра. Числовой эталон
корпуса тут не проверяется — это `tools/layout_bench.py` и
`tools/cmp_bitexact.py`, они гоняются руками по данным, которых нет в git.

Главное правило этих проверок (стоило стенду многих итераций): **любой запрет
сравнивается с БАЗОЙ ВХОДА, а не с нулём.** Поэтому `_gate.verify(...)["ok"]`
здесь не утверждается: у базы `new_diagonals` уже 8 — это диагонали исходной
схемы, а не брак раскладки. Сравнивается «после» с «после расстановки».
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("shapely",
                    reason="раскладка судит наложения через shapely (worker.txt)")
pytest.importorskip("numpy")

FIXTURE = Path(__file__).parent / "fixtures" / "layout" / "synth_med.json"
TIME_BUDGET_S = 60.0     # смоук, не бенчмарк: на dev-машине ~4 с


@pytest.fixture(scope="module")
def run():
    """Одна раскладка на весь модуль: она не бесплатная."""
    from modules.graph.core.layout import LayoutParams, layout
    from modules.graph.core.canvas_input import to_canvas

    src = json.loads(FIXTURE.read_text(encoding="utf-8"))
    src_copy = deepcopy(src)
    graph, transform = to_canvas(src)
    assert src == src_copy, "to_canvas обязан не мутировать вход"

    stages = {}
    t0 = time.perf_counter()
    out, stats = layout(graph, LayoutParams(), stages=stages)
    return {"src": src, "out": out, "stats": stats, "stages": stages,
            "transform": transform, "elapsed": time.perf_counter() - t0}


def test_canvas_transform_is_recorded(run):
    """Связь с исходным растром не теряется — без неё не развернуть координаты."""
    tr = run["out"]["graph"]["canvas_transform"]
    assert tr["canvas"] == [1920, 1080]
    assert tr["orig_image_size"] == run["src"]["graph"]["image_size"]
    assert tr["s"] > 0


def test_runs_in_seconds(run):
    assert run["elapsed"] < TIME_BUDGET_S, (
        f"раскладка синтетики на 208 узлов заняла {run['elapsed']:.1f} с")


def test_defects_reduced(run):
    st = run["stats"]
    assert st["defects_before"] > 0, "фикстура обязана содержать дефекты"
    assert st["defects_after"] <= st["defects_before"]


def test_topology_untouched(run):
    """Раскладка меняет геометрию. Связность, классы и id — не её дело."""
    before, after = run["stages"]["orig"], run["out"]
    assert ([n["id"] for n in before["nodes"]]
            == [n["id"] for n in after["nodes"]])
    assert ([(n["id"], n.get("class_name"), n.get("type")) for n in before["nodes"]]
            == [(n["id"], n.get("class_name"), n.get("type")) for n in after["nodes"]])
    assert ([(e["source"], e["target"]) for e in before["links"]]
            == [(e["source"], e["target"]) for e in after["links"]])


def test_geometry_actually_changed(run):
    """Иначе «отработала» означало бы «ничего не сделала»."""
    before, after = run["stages"]["orig"], run["out"]
    moved = sum(1 for a, b in zip(before["nodes"], after["nodes"])
                if a.get("centroid") != b.get("centroid"))
    assert moved > len(after["nodes"]) // 2


def test_invariants_not_worse_than_base(run):
    """Приёмка: сравнение с базой входа, а не с нулём."""
    from modules.graph.core.layout import _gate

    orig, placed, after = (run["stages"]["orig"], run["stages"]["placed"],
                           run["out"])
    base = _gate.verify(placed, orig, placed)
    now = _gate.verify(after, orig, placed)

    assert now["new_diagonals"] <= base["new_diagonals"], "новые диагонали"
    assert now["overlaps"] <= base["overlaps"], "новые наложения боксов"
    assert now["box_on_magistral"] <= 0, "бокс выехал на чужую магистраль"
    assert now["side_changed"] == 0, "сменилась сторона входа трубы"
    assert now["straight_broken"] == 0, "сломана прямая"
    assert not now["connectivity_changed"], "изменилась связность"


def test_no_local_order_swaps(run):
    """Узнаваемость: локальных перестановок нет.

    Судья именно локальный: глобальный `order_broken` из гейта на этой же
    фикстуре даёт 25, потому что считает парами узлов, разнесёнными на
    пол-листа (SOLUTION.md §6.3).
    """
    from modules.graph.core.layout import spread

    assert spread.order_broken_local(run["out"], run["stages"]["placed"]) == 0


def test_stays_inside_canvas_with_margin(run):
    """Рамка листа — требование заказчика («плохо читается ровно к краям»)."""
    from modules.graph.core.layout import LayoutParams

    m = LayoutParams().margin
    w, h = LayoutParams().canvas
    for n in run["out"]["nodes"]:
        bb = n.get("bbox")
        if not bb:
            continue
        assert bb[0] >= -0.5 and bb[1] >= -0.5, f"{n['id']} вышел за холст"
        assert bb[2] <= w + 0.5 and bb[3] <= h + 0.5, f"{n['id']} вышел за холст"
    ys = [n["bbox"][1] for n in run["out"]["nodes"] if n.get("bbox")]
    assert min(ys) >= m - 0.5, "рамка сверху не выдержана"


def test_frozen_params_fail_loudly():
    """Порог, захваченный дефолтом аргумента, обязан падать, а не игнорироваться."""
    from modules.graph.core.layout import LayoutParams, layout

    with pytest.raises(ValueError, match="дефолтами аргументов"):
        layout({"nodes": [], "links": []}, LayoutParams(pen_tol=20.0))
