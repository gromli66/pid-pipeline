# -*- coding: utf-8 -*-
"""Р5, блок 5: поворот одиночного клапана по трубе (`layout.valve_turn`).

Клапан степени 1 с трубой поперёк оси: поворот (рамка w↔h вокруг талии) и
сдвиг поперёк новой оси до линии трубы — прямая в талию; сдвиг упёрся в
соседа — только поворот (Z); поворот упёрся — ось прежняя, крючок в
осевую талию (не больше 3 изломов); predohran не поворачивается.
Координаты: точки данных [y, x], bbox [x1, y1, x2, y2].
"""
from __future__ import annotations

import pytest

pytest.importorskip("shapely", reason="судьи раскладки считаются на shapely")

from modules.graph.core import contact  # noqa: E402
from modules.graph.core.layout import spread  # noqa: E402
from modules.graph.core.layout._avoid_binding import avoid_available  # noqa: E402
from modules.graph.core.layout.valve_turn import turn_single_valves  # noqa: E402
from modules.graph.core.seating import reseat_all_endpoints  # noqa: E402


def _valve(cn, w, h, cx=200.0, cy=100.0, axis="H"):
    n = {"id": "v", "type": "equipment", "class_name": cn, "_axis": axis,
         "centroid": [cy, cx]}
    n["bbox"] = contact.box_around_waist(n, n["centroid"], w, h)
    return n


def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "class_name": "connection",
            "centroid": [cy, cx]}


def _block(nid, bb):
    return {"id": nid, "type": "block", "class_name": "testblock",
            "centroid": [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2],
            "bbox": list(bb)}


def _g(*extra, valve=None):
    return {"nodes": [valve or _valve("armatura_ruchn", 42.0, 21.0),
                      _conn("c", 205.0, 300.0)] + list(extra),
            "links": [{"id": "e", "source": "v", "target": "c",
                       "waypoints": []}]}


def _route(g):
    from modules.graph.core.layout.avoid_router import route_graph
    reseat_all_endpoints(g, model=True, slots=True, axial=True)
    ends = {}
    got = route_graph(g, ends_out=ends)
    e = g["links"][0]
    sp, tp = ends.get("e", (e["source_point"], e["target_point"]))
    return [sp] + got.get("e", []) + [tp]


def _bends(pts):
    return sum(1 for i in range(1, len(pts) - 1)
               if (abs(pts[i][0] - pts[i - 1][0]) > 1e-6)
               != (abs(pts[i + 1][0] - pts[i][0]) > 1e-6))


@pytest.mark.parametrize("cn,w,h,bbox", [
    ("armatura_ruchn", 42.0, 21.0, [194.5, 79.0, 215.5, 121.0]),
    # асимметричная талия (ELECTRIC_VLV V, REVERSE): рамка левее талии
    ("armatura_electro", 42.0, 37.8, [177.7084, 79.0, 215.5084, 121.0]),
])
def test_single_valve_turns_and_waist_lands_on_pipe(cn, w, h, bbox):
    """Труба вниз к коннектору (205, 300) у клапана H: поворот в V и сдвиг
    на 5 px — талия x = 205 на линии трубы, вход прямой в B."""
    g = _g(valve=_valve(cn, w, h))
    st = turn_single_valves(g)
    v = g["nodes"][0]
    assert (st["turned"], st["turn_shift_blocked"], st["turn_blocked"]) \
        == (1, 0, 0)
    assert st["shift_px"] == [5.0]
    assert v["_axis"] == "V"
    assert v["centroid"] == [pytest.approx(100.0), pytest.approx(205.0)]
    assert v["bbox"] == [pytest.approx(x) for x in bbox]
    reseat_all_endpoints(g, model=True, slots=True, axial=True)
    assert g["links"][0]["source_point"] == [121.0, pytest.approx(205.0)]


def test_turn_along_axis_pipe_untouched():
    """Труба вдоль оси — поворота нет, клапан бит-в-бит."""
    g = _g()
    g["nodes"][1]["centroid"] = [104.0, 400.0]
    before = dict(g["nodes"][0])
    st = turn_single_valves(g)
    assert st["turned"] == st["turn_blocked"] == 0
    assert g["nodes"][0] == before


@pytest.mark.parametrize("legal", [None, {("b", "v")}])
def test_turn_shift_blocked_gives_z(legal):
    """Сосед мешает сдвигу (рамка после сдвига задела бы его), но не
    повороту: клапан только повёрнут, ось V, центроид на месте, наложений
    нет; труба к коннектору — ортогональная, с изломом. Пара легальна
    (наложение из построения, судья наложений её не считает) — сдвиг всё
    равно стоит: «упирается в соседа» судит `graph_access.box_hits`."""
    blk = _block("b", [213.0, 40.0, 253.0, 85.0])
    g = _g(blk)
    st = turn_single_valves(g, legal)
    v = g["nodes"][0]
    assert (st["turned"], st["turn_shift_blocked"], st["turn_blocked"]) \
        == (1, 1, 0)
    assert v["_axis"] == "V"
    assert v["centroid"] == [100.0, 200.0]
    assert v["bbox"] == [189.5, 79.0, 210.5, 121.0]
    assert spread.overlaps(g) == 0
    if avoid_available():
        pts = _route(g)
        assert pts[0] == [121.0, 200.0]                 # вход в талию B
        assert 1 <= _bends(pts) <= 3


def test_turn_blocked_keeps_axis_and_hooks_into_waist():
    """Повёрнутая рамка (y 79..121) задела бы соседа сверху: ось прежняя,
    turn_blocked; конец — в осевой талии (L/R на y = 100), крючок не больше
    3 изломов, без косых."""
    blk = _block("b", [180.0, 40.0, 220.0, 85.0])
    g = _g(blk)
    st = turn_single_valves(g)
    v = g["nodes"][0]
    assert (st["turned"], st["turn_blocked"]) == (0, 1)
    assert v["_axis"] == "H" and v["bbox"] == [179.0, 89.5, 221.0, 110.5]
    reseat_all_endpoints(g, model=True, slots=True, axial=True)
    sp = g["links"][0]["source_point"]
    assert sp[0] == pytest.approx(100.0) and sp[1] in (179.0, 221.0)
    if avoid_available():
        pts = _route(g)
        assert pts[0][0] == pytest.approx(100.0) \
            and pts[0][1] in (179.0, 221.0)
        assert all(min(abs(pts[i][0] - pts[i - 1][0]),
                       abs(pts[i][1] - pts[i - 1][1])) <= 1e-6
                   for i in range(1, len(pts)))
        assert 1 <= _bends(pts) <= 3


def test_turn_rejected_when_defects_grow():
    """Судьи раскладки поверх box_hits: коннектор в 14.5 px под клапаном —
    повёрнутая рамка (низ 121) оставила бы трубе 4 px < FLOOR. Дефект
    вырос бы — поворота нет."""
    g = _g()
    g["nodes"][1]["centroid"] = [125.0, 200.0]
    st = turn_single_valves(g)
    assert (st["turned"], st["turn_blocked"]) == (0, 1)
    assert g["nodes"][0]["_axis"] == "H"


def test_relief_valve_not_turned():
    """predohran (RELIEF_VLV, угловой) — вне правила: не поворачивается."""
    g = _g(valve=_valve("predohran", 42.0, 31.5))
    before = dict(g["nodes"][0])
    st = turn_single_valves(g)
    assert st["turned"] == st["turn_blocked"] == 0
    assert g["nodes"][0] == before


def test_pinned_or_legacy_valve_not_turned():
    """Пин оператора на конце у клапана и старый холст — не трогаются."""
    g = _g()
    g["links"][0]["pin_source"] = {"dx": 0.0, "dy": 10.5}   # на B
    before = dict(g["nodes"][0])
    assert turn_single_valves(g)["turned"] == 0
    assert g["nodes"][0] == before
    g = _g()
    g["nodes"][0][contact.LEGACY_KEY] = True
    assert turn_single_valves(g)["turned"] == 0
    assert g["nodes"][0]["_axis"] == "H"
