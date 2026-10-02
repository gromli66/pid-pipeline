# -*- coding: utf-8 -*-
"""Осевая расстановка: трубы клапана вместо обрыва attempt(0.0).

Синтетика без данных заказчика. Дефект, который здесь сторожится
(c78e1f02: 25 пар из 79): клапан стоит талией на трубе, его коннектор — в
той же колонке и на входе ВНУТРИ рамки клапана. Если ни одна доля порогов
> 0 не влезает в холст, `place` раньше брал attempt(0.0): без труб вовсе,
и коннектор оставался в рамке клапана — раздвигание пару уже не разводит.

Кирпичи: `valve_pipes` (какие трубы — «клапан на своей оси»), `valve_floor`
(такая труба не наследует входную длину короче VALVE_PIPE), `prune_pipes`
(ослабляется только труба на критическом пути, сепарация блок-блок —
никогда) и сама ступень «только трубы клапанов» в `place`.
"""
from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from modules.graph.core.contact import CLASS_NAME_TO_SKIN
from modules.graph.core.layout import axial

VALVE = sorted(c for c, s in CLASS_NAME_TO_SKIN.items()
               if s[0] == "ValveControl")[0]


def _it(key, hw=0.0, hh=0.0):
    return {"key": key, "hw": hw, "hh": hh, "ox": 0.0, "oy": 0.0}


def _node(nid, cx, cy, hw=0.0, hh=0.0, cls="connector", **kw):
    n = {"id": nid, "class_name": cls, "centroid": [cy, cx]}     # [y, x]
    if cls == "connector":
        n["type"] = "connector"
        n["bbox"] = [cx, cy, cx, cy]
    else:
        n["bbox"] = [cx - hw, cy - hh, cx + hw, cy + hh]           # [x1,y1,x2,y2]
    n.update(kw)
    return n


def _column(height, room):
    """Колонка x=100: коннекторы u и c на входе внутри рамки клапана v
    (hh=12, ось V). Высокие блоки A (ряд v) и B (ряд ниже) почти во весь
    холст: их ряды расходятся не больше чем на `room` px. Рядом блок P с
    коннектором q в своей рамке: при любой доле > 0 труба P–q требует
    касания (+10 px) на той же цепочке рядов — при room=16 не влезает ни
    одна доля > 0, а касание трубы клапана (12 px) влезает."""
    hh = (height - 1 - room) / 2.0
    return {"nodes": [_node("A", 300, 90, 20, hh, "apparat"),
                      _node("B", 450, 110, 20, hh, "apparat"),
                      _node("v", 100, 90, 8, 12, VALVE, _axis="V"),
                      _node("u", 100, 84), _node("c", 100, 96),
                      _node("P", 200, 96, 8, 10, "apparat"),
                      _node("q", 200, 104)],
            "edges": [{"id": "e1", "source": "v", "target": "u"},
                      {"id": "e2", "source": "v", "target": "c"},
                      {"id": "e3", "source": "P", "target": "q"}]}


def _ys(g):
    return {n["id"]: n["centroid"][0] for n in g["nodes"]}


def test_cliff_keeps_valve_pipes_connector_leaves_frame():
    """Ни одна доля > 0 не влезла -> ступень «только трубы клапанов»:
    трубы не исчезают, коннектор не остаётся внутри рамки клапана
    (минимальная труба влезает в холст — она и держится)."""
    g = _column(200, 16)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["over"] <= axial.PRUNE_SLACK
    assert st["pipes_y"] == 2                      # v–u и v–c, не 0
    assert st["pruned"]["y"] == {"touch": 1, "drop": 0}
    # c — на критическом пути (к ряду B): до минимальной трубы — снаружи
    # рамки на клиренс VALVE_CLEAR (решение 01.10), а не на кромке
    assert y["c"] - y["v"] == pytest.approx(12.0 + axial.VALVE_CLEAR)
    # u — вне пути: полный пол VALVE_PIPE сверх полугабарита
    assert y["v"] - y["u"] >= 12.0 + axial.VALVE_PIPE - 1e-6


@pytest.mark.parametrize("room", [4, 8, 10])
def test_cliff_tight_sheet_canvas_first(room):
    """Тесный лист: минимальные трубы клапана не влезают, а прежняя ступень
    со снятием влезает. «Холст первым» (решение Максима 01.10): лист не
    выходит за холст — берётся прежняя ступень (труба клапана на
    критическом пути снята, коннектор в рамке v), а не запасной attempt с
    минимальными трубами ценой вылета (d729fb9: c78e1f02 0 -> 71 px)."""
    g = _column(200, room)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["over"] <= axial.PRUNE_SLACK
    assert st["pruned"]["y"]["drop"] >= 1          # прежняя ступень, не пол
    assert y["c"] - y["v"] < 12.0                  # плата: c в рамке v


@pytest.mark.parametrize("room", [-2, -10])
def test_cliff_no_step_fits_fallback_without_pipes(room):
    """Не влезает и прежняя ступень (сами блоки выше холста на -room px):
    запасной attempt — без труб вовсе, как до d729fb9 (решение Максима
    01.10: лист, который не влезает ни в одном варианте, держит меньший
    выход за лист). Вылет — только сами блоки, а не + полугабарит v и
    VALVE_CLEAR с минимальной трубой клапана на критическом пути (первая
    редакция; 0aea61c0: рамки за 1080 по Y 215.5 px при 129.0 без труб);
    плата — коннектор c в рамке v."""
    g = _column(200, room)
    st = axial.place(g, canvas=(600.0, 200.0))
    y = _ys(g)
    assert st["scale_eff"] == 0.0 and st["pruned"] == {}   # запасной attempt
    assert st["pipes_y"] == 0 and st["valve_pipe_eff"] == 0.0
    with_min_pipe = -room + 12.0 + axial.VALVE_CLEAR       # c снаружи рамки v
    assert st["over"] == pytest.approx(-room)              # только блоки
    assert st["over"] < with_min_pipe - 1.0
    assert abs(y["c"] - y["v"]) < 12.0                     # плата: c в рамке v


def test_no_cliff_no_valve_only_step():
    """Холст с запасом: доля 1 влезает, ступень не включается, а трубы
    клапана всё равно не короче VALVE_PIPE (вход — след рамки, не труба)."""
    g = _column(400, 60)
    st = axial.place(g, canvas=(600.0, 400.0))
    y = _ys(g)
    assert st["scale_eff"] == 1.0 and st["pruned"] == {}
    assert y["v"] - y["u"] >= 12.0 + axial.VALVE_PIPE - 1e-6
    assert y["c"] - y["v"] >= 12.0 + axial.VALVE_PIPE - 1e-6
    assert y["q"] - y["P"] == pytest.approx(10.0)   # не клапан: кап входа


def test_valve_only_step_stats_split_pipe_and_valve_floor():
    """Статистика ступени: прочие трубы сняты (pipe_eff 0), пол клапана —
    отдельным полем; на обычной доле pipe_eff = доля·12, пол = min(., 6)."""
    st = axial.place(_column(200, 16), canvas=(600.0, 200.0))
    assert (st["pipe_eff"], st["valve_pipe_eff"]) == (0.0, axial.VALVE_PIPE)
    st = axial.place(_column(400, 60), canvas=(600.0, 400.0))
    assert (st["pipe_eff"], st["valve_pipe_eff"]) == (12.0, axial.VALVE_PIPE)


def test_valve_pipe_gap_not_monotone_at_step_edge():
    """Объявленный разрыв (AUTO_LAYOUT): запас холста больше — труба клапана
    короче. room 22 — ступень, пол VALVE_PIPE; room 23 — влезла доля > 0,
    пол min(доля·12, 6) < 6, зато чужой коннектор q выходит на кромку P;
    room 28 — доля 1, снова 6. Сторож: менять — только с замером корпуса."""
    def gaps(room):
        g = _column(200, room)
        st = axial.place(g, canvas=(600.0, 200.0))
        y = _ys(g)
        return st["scale_eff"], y["c"] - y["v"] - 12.0, y["q"] - y["P"] - 10.0
    s22, c22, q22 = gaps(22)
    s23, c23, q23 = gaps(23)
    s28, c28, _ = gaps(28)
    assert s22 == 0.0 and c22 == pytest.approx(axial.VALVE_PIPE)
    assert q22 < -1.0                      # ступень: q остаётся в рамке P
    assert 0.0 < s23 < 1.0 and 0.0 < c23 < axial.VALVE_PIPE - 1.0
    assert q23 == pytest.approx(0.0, abs=1e-6)
    assert s28 == 1.0 and c28 == pytest.approx(axial.VALVE_PIPE)


def test_valve_pipes_only_along_valve_axis():
    g = {"nodes": [{"id": "v", "class_name": VALVE, "_axis": "V"},
                   {"id": "a", "class_name": "connector"},
                   {"id": "b", "class_name": "connector"},
                   {"id": "p", "class_name": "nasos"}],
         "edges": [{"id": "e1", "source": "v", "target": "a"},   # вдоль V
                   {"id": "e2", "source": "v", "target": "b"},   # поперёк
                   {"id": "e3", "source": "p", "target": "a"}]}  # не клапан
    xa = {"v": 0, "a": 0, "b": 1, "p": 0}
    ya = {"v": 1, "a": 0, "b": 1, "p": 2}
    assert axial.valve_pipes(g, xa, ya) == {("v", "a")}


def test_valve_floor_lifts_only_valve_pipes():
    by_key = {"v": _it("v", 10, 20), "a": _it("a"), "p": _it("p", 5, 5)}
    pipes = {"x": [], "y": [("v", "a", 20.0 + 1.0), ("p", "a", 5.0 + 1.0)]}
    out = axial.valve_floor(pipes, {("v", "a")}, by_key, 6.0)
    assert out["y"] == [("v", "a", 26.0), ("p", "a", 6.0)]
    assert axial.valve_floor(pipes, {("v", "a")}, by_key, 0.0) is pipes


def test_prune_touches_critical_pipe_first():
    by_key = {k: _it(k) for k in "abc"}
    idx = {"a": 0, "b": 1, "c": 2}
    pipes = [("a", "b", 10.0), ("b", "c", 10.0)]
    kept, st = axial.prune_pipes(3, [(0.0, 0.0)] * 3, [], pipes, by_key, idx,
                                 "oy", "hh", 12.0)
    assert st == {"touch": 1, "drop": 0}
    assert sorted(kept) == [("a", "b", 10.0), ("b", "c", 0.0)]


def test_prune_never_touches_block_separation():
    by_key = {"a": _it("a", 0, 10), "b": _it("b", 0, 10), "c": _it("c")}
    idx = {"a": 0, "c": 1, "b": 2}
    seps = [("a", "b", 40.0)]              # блок-блок сам шире холста
    pipes = [("a", "c", 10.0 + 6.0)]       # труба мимо критического пути
    kept, st = axial.prune_pipes(3, [(10.0, 10.0), (0.0, 0.0), (10.0, 10.0)],
                                 seps, pipes, by_key, idx, "oy", "hh", 30.0)
    assert st == {"touch": 0, "drop": 0} and kept == pipes


def test_prune_drops_after_touch_when_touch_not_enough():
    by_key = {"a": _it("a", 0, 10), "b": _it("b", 0, 10)}
    idx = {"a": 0, "b": 1}
    kept, st = axial.prune_pipes(2, [(10.0, 10.0)] * 2, [],
                                 [("a", "b", 26.0)], by_key, idx, "oy", "hh",
                                 30.0)
    assert st == {"touch": 0, "drop": 1} and kept == []


def test_prune_with_floor_stops_at_min_pipe():
    """floor: ослабление до полугабаритов + floor, труба не снимается, даже
    если и так не влезает (ступень «только трубы клапанов», VALVE_CLEAR)."""
    by_key = {"a": _it("a", 0, 10), "b": _it("b", 0, 10)}
    idx = {"a": 0, "b": 1}
    kept, st = axial.prune_pipes(2, [(10.0, 10.0)] * 2, [],
                                 [("a", "b", 26.0)], by_key, idx, "oy", "hh",
                                 30.0, floor=axial.VALVE_CLEAR)
    assert st == {"touch": 1, "drop": 0}
    assert kept == [("a", "b", 20.0 + axial.VALVE_CLEAR)]
    kept, st = axial.prune_pipes(2, [(10.0, 10.0)] * 2, [],
                                 [("a", "b", 26.0)], by_key, idx, "oy", "hh",
                                 60.0, floor=axial.VALVE_CLEAR)
    assert st == {"touch": 0, "drop": 0} and kept == [("a", "b", 26.0)]


# ───────────────────── переброска на свободную ось (E) ─────────────────────

def _flip_sheet(hh_t):
    """Обрыв по Y при свободной оси X. Колонка x=100: высокий T над U —
    общая колонка, разводятся только по Y. V — в другой колонке и другом
    ряду, наложен на U на входе: правило долей холста (ov_x/600 > ov_y/200)
    отдаёт паре U–V ось Y, и цепочка T–U–V не влезает в 199 px ни при какой
    доле (hh_t=50: 50 + 71 + 51 + 30 = 202). Цена перевода U–V на X (по
    координатам попытки) <= FLIP_COST: растяжка X уже развела их колонки.
    Поодаль — клапан w с трубой к k и блок P с трубой к q: их снимают
    ступень и запасной attempt."""
    return {"nodes": [_node("T", 100, 100, 5, hh_t, "apparat"),
                      _node("U", 100, 150, 40, 20, "apparat"),
                      _node("V", 145, 198, 20, 30, "apparat"),
                      _node("w", 400, 100, 8, 8, VALVE, _axis="V"),
                      _node("k", 400, 150),
                      _node("P", 300, 100, 10, 8, "apparat"),
                      _node("q", 300, 150)],
            "edges": [{"id": "e1", "source": "w", "target": "k"},
                      {"id": "e2", "source": "P", "target": "q"}]}


def _xy(g):
    return {n["id"]: (n["centroid"][1], n["centroid"][0]) for n in g["nodes"]}


def test_cliff_flips_pair_to_free_axis_pipes_kept(monkeypatch):
    """Обрыв по Y, X свободен: пара U–V переведена на X, доля > 0 влезла —
    трубы на месте, лист в холсте. Без переброски (как 5b00dd5) — запасной
    attempt: вылет 3 px и ни одной трубы."""
    g = _flip_sheet(50)
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["flips"] == 1 and st["over"] == 0.0 and st["pruned"] == {}
    assert st["scale_eff"] > 0.9
    assert st["pipes_y"] == 2 and st["pipe_eff"] > 11.0
    assert st["valve_pipe_eff"] == axial.VALVE_PIPE
    # U–V разведены по X, по Y — нет: как на входе (48 < 20 + 30 + 1)
    assert p["V"][0] - 20.0 - (p["U"][0] + 40.0) >= axial.GAP_MIN - 1e-6
    assert p["V"][1] - p["U"][1] == pytest.approx(48.0)
    assert max(p["V"][1] + 30.0, p["U"][1] + 20.0) <= 200.0 - axial.INSET

    monkeypatch.setattr(axial, "FLIP_ROUNDS", 0)    # переброска выключена
    g0 = _flip_sheet(50)
    st0 = axial.place(g0, canvas=(600.0, 200.0))
    assert st0["flips"] == 0 and st0["scale_eff"] == 0.0
    assert st0["over"] == pytest.approx(3.0)
    assert st0["pipes_y"] == 0 and st0["pruned"] == {}


@pytest.mark.parametrize("hh_t, width, over, ys", [
    # переброска помогает наполовину (вылет 83 -> 32), ступени с ней не
    # влезают -> сброс
    (90, 600.0, 83.0, (7.5, 118.5, 169.5)),
    # свободной оси нет (холст 100 px): круг переброски откатывается
    (50, 100.0, 3.0, (47.5, 118.5, 169.5)),
])
def test_cliff_flip_not_enough_resets_bitexact(monkeypatch, hh_t, width,
                                               over, ys):
    """Перевод не помогает: переброска сбрасывается, раскладка бит-в-бит как
    без неё (5b00dd5) — запасной attempt без труб, тот же вылет, те же ряды.
    Вывод узла из общей колонки выключен: с ним лист hh_t=90 влезает
    (test_fallback_split_common_column_sheet_fits)."""
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    g = _flip_sheet(hh_t)
    st = axial.place(g, canvas=(width, 200.0))
    p = _xy(g)
    assert st["flips"] == 0 and st["scale_eff"] == 0.0
    assert st["over"] == pytest.approx(over)
    assert st["pipes_y"] == 0 and st["pruned"] == {}
    assert (p["T"][1], p["U"][1], p["V"][1]) == pytest.approx(ys)
    if hh_t == 90:                  # переброска была: вылет с ней 32 px
        assert [t[2] for t in st["tried"]] == pytest.approx([83.0, 32.0])

    monkeypatch.setattr(axial, "FLIP_ROUNDS", 0)    # переброска выключена
    g0 = _flip_sheet(hh_t)
    axial.place(g0, canvas=(width, 200.0))
    assert g == g0


def test_no_cliff_no_flip():
    """Переброска — только на обрыве. V — клапан с трубой к r (вход 2 px):
    пол VALVE_PIPE fit_scale не видит, доля 1 не влезает (вылет 3 px), а
    бисекция находит долю 0.25 > 0 — обрыва нет, пары не переводятся, U–V
    разведены по Y, как без переброски (с ней влезла бы доля 1)."""
    g = _flip_sheet(50)
    for n in g["nodes"]:
        if n["id"] == "V":
            n["class_name"], n["_axis"] = VALVE, "V"
    g["nodes"].append(_node("r", 145, 230))
    g["edges"].append({"id": "e3", "source": "V", "target": "r"})
    st = axial.place(g, canvas=(600.0, 206.0))
    p = _xy(g)
    assert st["tried"][0] == (1.0, 0.0, 3.0)        # доля 1 не влезла
    assert st["scale_eff"] == 0.25 and st["flips"] == 0
    assert p["V"][1] - p["U"][1] == pytest.approx(51.0)


def test_cliff_flip_round_rollback_keeps_earlier_round():
    """Откат круга переброски снимает только этот круг. Круг 1 переводит
    A = U1–V1 (51 px по Y), круг 2 — B = U2–V2 (49 px), с ней X не влезает
    в 200 px — круг 2 откатывается, A остаётся. С одной A лист влезает при
    трубе V1–r1 до 2 px (12·доля <= 2): бисекция вверх находит долю 0.156.
    Без отката B оставалась бы в переброске: X вылезает на любой доле,
    переброска сбрасывается, лист — запасной attempt с вылетом 1 px."""
    g = {"nodes": [_node("T1", 100, 100, 5, 50, "apparat"),
                   _node("U1", 100, 150, 40, 20, "apparat"),
                   _node("V1", 145, 198, 20, 30, "apparat"),
                   _node("r1", 145, 240),
                   _node("U2", 300, 150, 40, 17, "apparat"),
                   _node("V2", 345, 198, 20, 31, "apparat")],
         "edges": [{"id": "e1", "source": "V1", "target": "r1"}]}
    st = axial.place(g, canvas=(200.0, 203.0))
    p = _xy(g)
    assert st["flips"] == 1 and st["over"] == 0.0 and st["pruned"] == {}
    assert st["scale_eff"] == pytest.approx(0.156, abs=1e-3)
    # A переведена: U1–V1 по Y ближе 51; B — нет: U2–V2 по Y не ближе 49
    assert p["V1"][1] - p["U1"][1] < 51.0 - 1.0
    assert p["V2"][1] - p["U2"][1] >= 49.0 - 1e-6


# ──────────── запасной attempt: вывод узла из общей колонки ────────────

def _shift_x(g, nid, dx):
    for n in g["nodes"]:
        if n["id"] == nid:
            n["centroid"][1] += dx
            n["bbox"][0] += dx
            n["bbox"][2] += dx
    return g


@pytest.mark.parametrize("dx", [1.0, -1.0])
def test_fallback_split_common_column_sheet_fits(monkeypatch, dx):
    """Не влезает ни одна ступень, переброски мало (U–V: вылет 83 -> 32,
    сброс). T над U в общей колонке (на входе x 100±1 — допуск оси 2 px),
    ребра между ними нет — их трубы не идут вдоль колонки. Запасной attempt
    выводит меньший из пары, T, в свою колонку с той же координатой — по ту
    сторону от U, где T стоял на входе, — и разводит пару по свободной оси X:
    ступень «только трубы клапанов» влезает, все рамки в холсте, труба
    клапана w–k на месте. Без вывода (как 1b2dc1e) — вылет 83 px, труб 0."""
    g = _shift_x(_flip_sheet(90), "T", dx)
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["splits"] == 1 and st["over"] == 0.0
    assert st["scale_eff"] == 0.0 and st["pipes_y"] == 1      # w–k
    # T — на своей стороне от U, пара разведена по X (5 + 40 + GAP_MIN)
    assert (p["T"][0] - p["U"][0]) * dx >= 46.0 - 1e-6
    for n in g["nodes"]:
        x1, y1, x2, y2 = n["bbox"]
        assert min(x1, y1) >= 0.0 and x2 <= 600.0 and y2 <= 200.0

    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)     # вывод выключен
    g0 = _shift_x(_flip_sheet(90), "T", dx)
    st0 = axial.place(g0, canvas=(600.0, 200.0))
    assert st0["splits"] == 0 and st0["over"] == pytest.approx(83.0)
    assert st0["pipes_y"] == 0


@pytest.mark.parametrize("edge_tu, width", [
    (True, 600.0),     # вывести нечего: труба T–U идёт вдоль колонки
    (False, 100.0),    # вывести некуда: X кончается — круг с выводом откатан
])
def test_fallback_nothing_to_split_bitexact(monkeypatch, edge_tu, width):
    """Вывода нет — запасной attempt бит-в-бит как до него (1b2dc1e):
    переброска, найденная в запасном attempt заново (U–V, с ней вылет 32),
    сбрасывается — вылет 83 px и ряды те же, что без вывода."""
    def sheet():
        g = _flip_sheet(90)
        if edge_tu:
            g["edges"].append({"id": "e3", "source": "T", "target": "U"})
        return g

    g = sheet()
    st = axial.place(g, canvas=(width, 200.0))
    p = _xy(g)
    assert st["splits"] == 0 and st["flips"] == 0
    assert st["over"] == pytest.approx(83.0)
    assert st["pipes_y"] == 0 and st["pruned"] == {}
    assert (p["T"][1], p["U"][1], p["V"][1]) == pytest.approx(
        (7.5, 118.5, 169.5))

    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)     # вывод выключен
    g0 = sheet()
    axial.place(g0, canvas=(width, 200.0))
    assert g == g0


# ──── «та же колонка» — по координате; пара с ребром внутри (red-team) ────

def _d1(cell):
    """_flip_sheet(90), T на 1 px левее U (общая колонка), над T — zz с
    ВЕРТИКАЛЬНОЙ трубой T–zz. cell: zz делит ячейку (колонка, ряд) с za —
    `split_cells` даёт zz свою колонку той же координаты."""
    g = _shift_x(_flip_sheet(90), "T", -1.0)
    if cell:
        g["nodes"].append(_node("za", 100, 4))
    g["nodes"].append(_node("zz", 99, 5))
    g["edges"].append({"id": "ez", "source": "T", "target": "zz"})
    return g


def test_fallback_split_pipe_to_split_cell_column_kept(monkeypatch):
    """Колонка `split_cells` стоит на той же координате, что исходная: труба
    T–zz идёт вдоль колонки. По индексу (bfcc301) T считался свободным и
    выводился — T–zz косая (dx 46); вывод U разводил бы колонку T с колонкой
    zz (её осевая модель не держит). Вывода нет: граф = с выключенным
    выводом, T и zz в одной колонке."""
    g = _d1(True)
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["splits"] == 0 and p["T"][0] == p["zz"][0]
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    g0 = _d1(True)
    axial.place(g0, canvas=(600.0, 200.0))
    assert g == g0


def test_fallback_split_pipe_along_column_control():
    """Контроль: zz в колонке T (ячейки нет) — T не свободен, выводится U
    (без труб), T–zz остаётся вертикальной, лист влезает."""
    g = _d1(False)
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["splits"] == 1 and st["over"] == 0.0
    assert p["T"][0] == p["zz"][0] and p["U"][0] - p["T"][0] >= 46.0 - 1e-6


def _c3(edge):
    """_flip_sheet(50), V в соседней колонке (3 px правее U); edge — почти
    вертикальная труба U–V, пара U–V на критическом пути."""
    g = _shift_x(_flip_sheet(50), "V", -42.0)
    if edge:
        g["edges"].append({"id": "eUV", "source": "U", "target": "V"})
    return g


def test_cliff_no_flip_pair_with_pipe_inside(monkeypatch):
    """Пара с трубой между членами не переводится на свободную ось: U–V
    разведены по X — труба U–V косая (bf9cb7a: dx 61). Вывод выключен —
    граф = с выключенной переброской (вылет 3, труба почти вертикальна)."""
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    g = _c3(True)
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["flips"] == 0 and st["over"] == pytest.approx(3.0)
    assert abs(p["V"][0] - p["U"][0]) <= 5.5
    monkeypatch.setattr(axial, "FLIP_ROUNDS", 0)
    g0 = _c3(True)
    axial.place(g0, canvas=(600.0, 200.0))
    assert g == g0
    # контроль: без трубы U–V пара переводится, доля 0.984
    monkeypatch.setattr(axial, "FLIP_ROUNDS", 6)
    st1 = axial.place(_c3(False), canvas=(600.0, 200.0))
    assert st1["flips"] == 1 and st1["scale_eff"] == pytest.approx(0.984)


def _sib():
    """_flip_sheet(50), V на координате колонки U и в ячейке с A1 —
    `split_cells` даёт V свою колонку той же координаты; труба V–r вниз
    вдоль колонки (r в колонке U). U уже (hw 30): цена перевода U–V <= 60."""
    g = _flip_sheet(50)
    for n in g["nodes"]:
        if n["id"] == "U":
            n["bbox"] = [70.0, 130.0, 130.0, 170.0]
    _shift_x(g, "V", -45.0)
    g["nodes"] += [_node("A1", 100, 198), _node("r", 100, 240)]
    g["edges"].append({"id": "eVr", "source": "V", "target": "r"})
    return g


def test_cliff_no_flip_pair_on_same_coordinate(monkeypatch):
    """Пара в колонках одной координаты (колонка `split_cells`) — общая
    колонка, на свободную ось не переводится: по индексу (bfcc301) U–V
    переводилась, колонка V уезжала — труба V–r косая (dx 51). Граф = с
    выключенной переброской, V–r вертикальна."""
    g = _sib()
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["flips"] == 0 and st["splits"] == 0
    assert p["V"][0] == p["r"][0]
    monkeypatch.setattr(axial, "FLIP_ROUNDS", 0)
    g0 = _sib()
    axial.place(g0, canvas=(600.0, 200.0))
    assert g == g0


# ──────────── сторона вывода — по трубам (red-team bfcc301, (1)) ────────────

def _a4(xs):
    """_flip_sheet(90), T на 1 px правее U (общая колонка) и в своём ряду (на
    5 px выше U); xs — коннекторы в ряду T с трубой к T (поперёк колонки)."""
    g = _shift_x(_flip_sheet(90), "T", 1.0)
    for n in g["nodes"]:
        if n["id"] == "T":
            n["centroid"][0] -= 5.0
            n["bbox"][1] -= 5.0
            n["bbox"][3] -= 5.0
    for i, x in enumerate(xs):
        g["nodes"].append(_node("L%d" % i, x, 145))
        g["edges"].append({"id": "eL%d" % i, "source": "L%d" % i,
                           "target": "T"})
    return g


def _crosses(g, a, b, box_id):
    from shapely.geometry import LineString, box
    p = _xy(g)
    n = {m["id"]: m for m in g["nodes"]}[box_id]
    x1, y1, x2, y2 = n["bbox"]
    return LineString([p[a], p[b]]).intersects(
        box(x1 + 0.5, y1 + 0.5, x2 - 0.5, y2 - 0.5))


@pytest.mark.parametrize("lx", [30.0, 250.0])
def test_fallback_split_side_toward_pipe(lx):
    """T выводится туда, где его сосед L, а не по входной координате (T на
    1 px правее U — bfcc301 выводил вправо): труба L–T не идёт сквозь U."""
    g = _a4([lx])
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["splits"] == 1 and st["over"] == 0.0
    assert (p["T"][0] - p["U"][0]) * (lx - 100.0) > 0.0
    assert not _crosses(g, "L0", "T", "U")


def test_fallback_split_side_conflict_not_split(monkeypatch):
    """Соседи T по обе стороны колонки: любой вывод T ведёт одну из труб
    сквозь U, вывод U — сквозь U трубу T. Вывода нет: граф = с выключенным
    выводом (bfcc301 выводил T вправо — L–T сквозь U)."""
    g = _a4([30.0, 250.0])
    st = axial.place(g, canvas=(600.0, 200.0))
    assert st["splits"] == 0
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    g0 = _a4([30.0, 250.0])
    axial.place(g0, canvas=(600.0, 200.0))
    assert g == g0


def test_fallback_split_partner_column_follows_near_vertical_pipe():
    """C3 (U–V с трубой — не переводится): выводится T, колонка U уходит в
    сторону V (почти вертикальная труба U–V к колонке в 3 px), T — в
    обратную: труба U–V остаётся почти вертикальной (bf9cb7a: переброска
    U–V, dx 61; вывод T по входу вправо — dx 49)."""
    g = _c3(True)
    st = axial.place(g, canvas=(600.0, 200.0))
    p = _xy(g)
    assert st["splits"] == 1 and st["over"] == 0.0
    assert p["T"][0] < p["U"][0] and abs(p["V"][0] - p["U"][0]) <= 5.5


# ──── вывод — только при уменьшении вылета (red-team bfcc301, (2) и (4)) ────
# Листы генератора red-team (SCR/trt/s/gen.py, сид -> лист): (id, вид, cx, cy
# [, hw, hh [, ось]]) — «a» блок, «v» клапан, «c» коннектор.

_SEEDS = {
    1383: ((400.0, 400.0), [
        ("n0", "a", 82, 35.07, 34.0, 20.48),
        ("n1", "v", 82, 64.93, 10.88, 10.88, "V"),
        ("n2", "a", 81, 71.27, 70.48, 12.63),
        ("n3", "v", 140, 52.83, 17.74, 17.74, "H"),
        ("n4", "a", 140, 189.21, 108.15, 90.0),
        ("n5", "a", 139, 304.1, 64.22, 24.9), ("n6", "c", 140, 335.78),
        ("n7", "v", 140, 377.24, 13.6, 13.6, "H"), ("n8", "c", 218, 29.38),
        ("n9", "a", 217, 100.55, 53.22, 74.28), ("n10", "c", 218, 168.34),
        ("n11", "a", 272, 55.88, 62.32, 36.31),
        ("n12", "v", 272, 95.39, 11.95, 11.95, "V"),
        ("n13", "a", 272, 101.93, 35.81, 6.14), ("c14", "c", 69.79, 71.27),
        ("c15", "c", 343.51, 377.24), ("c16", "c", 321.86, 101.93)],
        [("n0", "n1"), ("n4", "n5"), ("n5", "n6"), ("n6", "n7"), ("n9", "n10"),
         ("n2", "c14"), ("n7", "c15"), ("n13", "c16")]),
    1083: ((1920.0, 1080.0), [
        ("n0", "a", 559, 261.5, 209.02, 142.42),
        ("n1", "v", 559, 450.93, 28.87, 28.87, "H"), ("n2", "c", 560, 467.39),
        ("n3", "a", 560, 534.75, 210.78, 38.5),
        ("n4", "v", 645, 143.8, 28.34, 28.34, "V"), ("n5", "c", 644, 279.39),
        ("n6", "a", 645, 277.18, 129.95, 50.16),
        ("n7", "v", 643, 426.8, 28.51, 28.51, "V"), ("n8", "c", 645, 435.4),
        ("n9", "v", 657, 98.37, 29.41, 29.41, "V"),
        ("n10", "a", 656, 164.04, 179.15, 37.77),
        ("n11", "a", 656, 345.49, 120.14, 58.21),
        ("n12", "a", 658, 692.9, 57.26, 168.51),
        ("n13", "a", 869, 370.17, 197.7, 223.53),
        ("n14", "a", 869, 609.09, 213.68, 86.42), ("n15", "c", 869, 705.68),
        ("n16", "c", 870, 767.58), ("n17", "a", 868, 1127.69, 186.01, 287.1),
        ("n18", "a", 928, 305.28, 136.09, 228.19),
        ("n19", "a", 928, 559.24, 17.98, 88.43),
        ("n20", "a", 929, 723.53, 219.7, 154.96),
        ("c21", "c", 1695.04, 450.93), ("c22", "c", 1914.19, 277.18),
        ("c23", "c", 1695.68, 426.8), ("c24", "c", 590.52, 98.37),
        ("c25", "c", 1323.09, 705.68), ("c26", "c", 963.6, 767.58)],
        [("n0", "n1"), ("n11", "n12"), ("n13", "n14"), ("n15", "n16"),
         ("n16", "n17"), ("n1", "c21"), ("n6", "c22"), ("n7", "c23"),
         ("n9", "c24"), ("n15", "c25"), ("n16", "c26")]),
    1307: ((1920.0, 200.0), [
        ("n0", "c", 686, 25.25), ("n1", "v", 685, 29.64, 6.11, 6.11, "V"),
        ("n2", "a", 788, 57.32, 9.55, 37.69),
        ("n3", "a", 789, 122.8, 33.31, 34.2),
        ("n4", "a", 818, 37.67, 25.46, 30.07),
        ("n5", "a", 819, 102.19, 37.52, 16.28),
        ("n6", "v", 818, 141.57, 8.9, 8.9, "V"),
        ("n7", "a", 914, 104.82, 90.0, 90.0),
        ("n8", "v", 915, 219.28, 6.75, 6.75, "V"),
        ("n9", "v", 914, 232.39, 6.49, 6.49, "V"),
        ("n10", "a", 914, 279.34, 37.06, 28.22),
        ("n11", "a", 944, 103.75, 90.0, 90.0),
        ("n12", "a", 944, 208.27, 14.71, 22.88),
        ("n13", "v", 1682, 35.74, 5.77, 5.77, "H"),
        ("n14", "a", 1682, 51.61, 33.93, 24.67),
        ("n15", "a", 1682, 129.93, 9.94, 42.14), ("n16", "c", 1681, 171.26),
        ("c17", "c", 1160.1, 25.25), ("c18", "c", 1829.96, 29.64),
        ("c19", "c", 1862.0, 122.8), ("c20", "c", 119.43, 37.67),
        ("c21", "c", 338.1, 102.19), ("c22", "c", 914.08, 141.57),
        ("c23", "c", 651.66, 104.82), ("c24", "c", 181.28, 232.39),
        ("c25", "c", 1409.32, 103.75)],
        [("n0", "n1"), ("n4", "n5"), ("n14", "n15"), ("n0", "c17"),
         ("n1", "c18"), ("n3", "c19"), ("n4", "c20"), ("n5", "c21"),
         ("n6", "c22"), ("n7", "c23"), ("n9", "c24"), ("n11", "c25")]),
    649: ((1920.0, 1080.0), [
        ("n0", "a", 397, 339.94, 192.52, 221.78),
        ("n1", "a", 398, 963.75, 236.65, 286.69),
        ("n2", "v", 397, 1296.05, 47.69, 47.69, "V"),
        ("n3", "a", 397, 1358.72, 165.75, 66.19),
        ("n4", "v", 1140, 133.52, 35.88, 35.88, "V"),
        ("n5", "a", 1139, 164.52, 172.27, 69.69),
        ("n6", "v", 1446, 183.47, 29.63, 29.63, "V"),
        ("n7", "a", 1446, 184.6, 103.65, 41.43),
        ("n8", "a", 1446, 526.57, 236.42, 284.47),
        ("n9", "a", 1445, 995.22, 102.45, 137.4),
        ("n10", "a", 1483, 151.38, 191.84, 88.06),
        ("n11", "v", 1483, 401.31, 27.01, 27.01, "V"),
        ("n12", "c", 1483, 501.98), ("n13", "a", 1484, 865.61, 139.98, 224.83),
        ("n14", "a", 1483, 1309.85, 126.53, 247.53),
        ("c15", "c", 1281.27, 1296.05), ("c16", "c", 567.06, 183.47),
        ("c17", "c", 53.77, 995.22), ("c18", "c", 1552.53, 151.38),
        ("c19", "c", 1881.96, 401.31), ("c20", "c", 506.35, 1309.85)],
        [("n0", "n1"), ("n7", "n8"), ("n11", "n12"), ("n2", "c15"),
         ("n6", "c16"), ("n9", "c17"), ("n10", "c18"), ("n11", "c19"),
         ("n14", "c20")]),
    771: ((800.0, 200.0), [
        ("n0", "v", 117, 20.89, 8.29, 8.29, "V"),
        ("n1", "a", 116, 81.96, 5.91, 39.81),
        ("n2", "a", 117, 132.27, 18.86, 13.19), ("n3", "c", 431, 18.92),
        ("n4", "a", 430, 135.53, 90.0, 90.0),
        ("n5", "a", 515, 30.47, 38.61, 4.75),
        ("n6", "a", 517, 126.6, 90.0, 90.0), ("n7", "c", 516, 220.03),
        ("n8", "a", 516, 250.14, 29.32, 4.81), ("c9", "c", 491.37, 132.27),
        ("c10", "c", 589.53, 18.92), ("c11", "c", 738.15, 135.53),
        ("c12", "c", 735.14, 30.47), ("c13", "c", 196.52, 250.14)],
        [("n1", "n2"), ("n3", "n4"), ("n5", "n6"), ("n6", "n7"), ("n2", "c9"),
         ("n3", "c10"), ("n4", "c11"), ("n5", "c12"), ("n8", "c13")]),
    803: ((400.0, 200.0), [
        ("n0", "v", 96, 20.25, 9.25, 9.25, "H"),
        ("n1", "a", 97, 32.58, 30.18, 12.36), ("n2", "c", 97, 51.62),
        ("n3", "a", 118, 63.19, 21.99, 41.74),
        ("n4", "a", 118, 165.55, 13.85, 37.34),
        ("n5", "v", 119, 199.14, 6.59, 6.59, "V"),
        ("n6", "v", 166, 33.89, 6.6, 6.6, "V"), ("n7", "c", 165, 47.61),
        ("n8", "a", 165, 93.03, 20.17, 46.18),
        ("n9", "v", 248, 39.54, 9.93, 9.93, "V"), ("n10", "c", 248, 76.54),
        ("n11", "c", 248, 104.27), ("n12", "a", 249, 191.96, 90.0, 90.0),
        ("n13", "c", 308, 11.61), ("n14", "v", 309, 39.23, 7.89, 7.89, "V"),
        ("c15", "c", 298.14, 32.58), ("c16", "c", 310.55, 63.19),
        ("c17", "c", 389.81, 33.89), ("c18", "c", 131.98, 47.61),
        ("c19", "c", 206.8, 39.54), ("c20", "c", 226.82, 39.23)],
        [("n1", "n2"), ("n3", "n4"), ("n6", "n7"), ("n7", "n8"), ("n9", "n10"),
         ("n11", "n12"), ("n13", "n14"), ("n1", "c15"), ("n3", "c16"),
         ("n6", "c17"), ("n7", "c18"), ("n9", "c19"), ("n14", "c20")]),
    1001: ((400.0, 200.0), [
        ("n0", "v", 232, 32.89, 8.25, 8.25, "V"),
        ("n1", "a", 233, 105.0, 4.13, 47.62),
        ("n2", "v", 232, 176.18, 8.69, 8.69, "V"),
        ("n3", "v", 233, 211.33, 5.09, 5.09, "V"),
        ("n4", "a", 233, 272.79, 19.08, 45.92),
        ("n5", "a", 324, 14.69, 8.52, 7.46),
        ("n6", "a", 324, 62.27, 10.42, 46.68),
        ("n7", "a", 324, 217.64, 90.0, 90.0),
        ("n8", "a", 324, 399.99, 90.0, 90.0), ("c9", "c", 285.89, 62.27)],
        [("n0", "n1"), ("n2", "n3"), ("n7", "n8"), ("n6", "c9")]),
    652: ((400.0, 1080.0), [
        ("n0", "c", 200.0, 154.18), ("n1", "a", 200.0, 308.2, 115.42, 203.67),
        ("n2", "a", 200.0, 804.89, 27.39, 178.61),
        ("n3", "a", 200.0, 1101.98, 168.4, 134.64),
        ("n4", "a", 199.0, 1318.53, 65.99, 52.4), ("c5", "c", 373.42, 154.18),
        ("c6", "c", 142.68, 1101.98), ("c7", "c", 392.97, 1318.53)],
        [("n0", "c5"), ("n3", "c6"), ("n4", "c7")]),
    936: ((400.0, 400.0), [
        ("n0", "c", 90, 22.16), ("n1", "a", 89, 61.91, 55.53, 30.94),
        ("n2", "a", 89, 184.13, 25.37, 64.65), ("n3", "c", 130, 60.5),
        ("n4", "a", 131, 116.94, 54.58, 27.39),
        ("n5", "a", 132, 253.27, 90.0, 101.14),
        ("n6", "a", 131, 454.73, 8.79, 66.97), ("n7", "c", 238, 26.81),
        ("n8", "v", 240, 20.56, 13.3, 13.3, "H"), ("n9", "c", 239, 20.53),
        ("n10", "a", 239, 55.81, 47.54, 31.4), ("n11", "c", 239, 109.2),
        ("c12", "c", 24.23, 61.91), ("c13", "c", 214.48, 253.27),
        ("c14", "c", 328.02, 454.73), ("c15", "c", 20.13, 26.81),
        ("c16", "c", 120.78, 20.53), ("c17", "c", 183.73, 55.81)],
        [("n4", "n5"), ("n7", "n8"), ("n8", "n9"), ("n9", "n10"),
         ("n1", "c12"), ("n5", "c13"), ("n6", "c14"), ("n7", "c15"),
         ("n9", "c16"), ("n10", "c17")]),
}


def _seed_sheet(seed):
    canvas, rows, eds = _SEEDS[seed]
    nodes = []
    for r in rows:
        nid, kind, cx, cy = r[:4]
        if kind == "c":
            nodes.append(_node(nid, cx, cy))
        else:
            kw = {"_axis": r[6]} if len(r) > 6 else {}
            nodes.append(_node(nid, cx, cy, r[4], r[5],
                               VALVE if kind == "v" else "apparat", **kw))
    return ({"nodes": nodes, "edges": [{"id": "e%d" % i, "source": s,
                                        "target": t}
                                       for i, (s, t) in enumerate(eds)]},
            canvas)


@pytest.mark.parametrize("seed, over", [
    # круг с двумя выводами поднимал вылет 39.9 -> 88.0 и принимался (X не
    # вылез); прочие круги откатывались — запасной attempt с вылетом 88
    (1383, 39.94),
    # 1920x1080: вариант с выводом — 683.8 против 622.1 без него
    (1083, 622.06),
    # вариант с выводом: вылет 685.8 -> 436.7, но клапан n6 наложен на блок
    # n7 (наложений 0 -> 1)
    (649, 685.84),
    # вариант с выводом: вылет 23.8 -> 12.8, но узлов за листом 5 -> 6
    (803, 23.79),
    # вариант с выводами: вылет 272.3 -> 190.8, но наложение блоков 0 -> 1
    (1001, 272.28),
    # вариант с выводом: вылет 84.9 -> 0, но труба n9–n10 вдоль колонки
    # косая — колонка split_cells n9 (ячейка с n8) отстала от колонки n10
    (936, 84.94),
])
def test_fallback_split_not_better_is_plain(monkeypatch, seed, over):
    """Вывод не уменьшил вылет (или добавил наложение, узел за листом) —
    запасной attempt бит-в-бит как без вывода (bfcc301: 1383 88.0, 1083
    683.8, 649 436.7 с наложением, 803 12.8 с узлом за листом; 1001 —
    наложение при сравнении без наложений; 936 — 0 с косой трубой)."""
    g, canvas = _seed_sheet(seed)
    st = axial.place(g, canvas=canvas)
    assert st["splits"] == 0 and st["flips"] == 0
    assert st["over"] == pytest.approx(over)
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    g0, _c = _seed_sheet(seed)
    axial.place(g0, canvas=canvas)
    assert g == g0


def test_fallback_split_fits_without_overlaps_1307(monkeypatch):
    """Сид 1307: выводы, принятые круг за кругом только при уменьшении
    вылета, укладывают лист (вылет 83 -> 0) без наложений и тесных пар
    (bfcc301: тоже 0, но 2 клапана наложены на блок 180x180)."""
    g, canvas = _seed_sheet(1307)
    st = axial.place(g, canvas=canvas)
    assert st["splits"] > 0 and st["over"] == 0.0
    assert st["overlaps_left"] == 0 and st["residual_tight_pairs"] == 0
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    st0 = axial.place(_seed_sheet(1307)[0], canvas=canvas)
    assert st0["over"] == pytest.approx(83.05)


def test_fallback_split_round_growing_over_rolled_back(monkeypatch):
    """Сид 771: круг с выводом, после которого вылет по тесной оси вырос,
    откатывается и повторяется без этого вывода — следующий круг выводит
    другой узел: вылет 5.66 -> 2.39, узлов за листом 2 -> 1 (без отката —
    вывод, поднявший вылет, оставался, и итог = запасной attempt без
    выводов, 5.66)."""
    g, canvas = _seed_sheet(771)
    st = axial.place(g, canvas=canvas)
    assert st["splits"] == 1 and st["over"] == pytest.approx(2.39)
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    st0 = axial.place(_seed_sheet(771)[0], canvas=canvas)
    assert st0["over"] == pytest.approx(5.66)


def test_fallback_split_round_free_axis_retried_without_split(monkeypatch):
    """Сид 652: круг с выводом, после которого вылезла свободная ось,
    откатывается и повторяется без этого вывода (а не обрывает переброску,
    как bfcc301) — следующий вывод укладывает лист: вылет 62.64 -> 0."""
    g, canvas = _seed_sheet(652)
    st = axial.place(g, canvas=canvas)
    assert st["splits"] == 1 and st["over"] == 0.0
    monkeypatch.setattr(axial, "SPLIT_PER_ROUND", 0)
    st0 = axial.place(_seed_sheet(652)[0], canvas=canvas)
    assert st0["over"] == pytest.approx(62.64)


def test_flips_counts_only_pairs_used_by_winner():
    """flips — пары, ось которых в проходе-победителе задала переброска, а
    не всё, что накопилось в flip: сид 1307 — 5 выводов, пара n10–n9 в flip,
    но в итоговом проходе не конфликтует (n9 выведен раньше) — flips 4
    (bfcc301 считал len(flip) — 5)."""
    g, canvas = _seed_sheet(1307)
    st = axial.place(g, canvas=canvas)
    assert st["splits"] == 5 and st["flips"] == 4
