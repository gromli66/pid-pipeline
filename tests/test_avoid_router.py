# -*- coding: utf-8 -*-
"""Тесты этапа роутинга Э7-b: адаптер libavoid (layout/avoid_router.py).

Синтетика, без данных заказчика. Без vendored-биндинга модуль честно
скипается (`_avoid_binding.avoid_available`): на машинах без бинаря
раскладка живёт без роутинга, и тесты обязаны вести себя так же.

Покрытие лови ловушек разведки (docstring avoid_router):
  * SWIG-GC — маршрут не деградирует при агрессивном gc в процессе транзакции;
  * молчаливый fallback при недостижимом пине — ребру остаётся прежняя
    геометрия (сценарий снят живым пробником: пин с ConnDir в упор к стене
    даёт маршрут СКВОЗЬ стену).
"""
from __future__ import annotations

import gc
from copy import deepcopy

import pytest

pytest.importorskip("shapely",
                    reason="детект прошивания — shapely (worker.txt)")
pytest.importorskip("numpy")

from modules.graph.core.layout._avoid_binding import avoid_available  # noqa: E402

if not avoid_available():
    pytest.skip("vendored-биндинг libavoid недоступен (vendor/adaptagrams)",
                allow_module_level=True)

from modules.graph.core.layout import LayoutParams  # noqa: E402
from modules.graph.core.layout.avoid_router import (  # noqa: E402
    apply_routing, route_graph)
from modules.graph.core.layout import _gate, spread  # noqa: E402
from modules.graph.core.graph_access import (  # noqa: E402
    edge_polyline, edges, nodes_by_id)
from modules.graph.core.seating import reseat_all_endpoints  # noqa: E402

BUF = LayoutParams().route_buffer


# ───────────────────────── синтетика ─────────────────────────

def _block(nid, cx, cy, w=40.0, h=40.0):
    """Блок вне FIXED_SIZES: посадка и препятствие — по bbox."""
    return {"id": nid, "type": "block", "class_name": "testblock",
            "centroid": [cy, cx],
            "bbox": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]}


def _connector(nid, cx, cy):
    return {"id": nid, "type": "connector", "class_name": "connection",
            "centroid": [cy, cx]}


def _edge(eid, s, t, **extra):
    e = {"id": eid, "source": s, "target": t, "waypoints": []}
    e.update(extra)
    return e


def _graph(nodes, links, seat=True):
    g = {"nodes": nodes, "links": links}
    if seat:
        reseat_all_endpoints(g)
    return g


def _polyline_ortho(e, tol=1.5):
    pl = edge_polyline(e)
    return all(min(abs(pl[i][0] - pl[i - 1][0]),
                   abs(pl[i][1] - pl[i - 1][1])) <= tol
               for i in range(1, len(pl)))


def _transit_graph():
    """Транзит: магистраль a -> b прошивает чужой бокс w посередине."""
    return _graph(
        [_block("a", 100, 100), _block("b", 500, 100),
         _block("w", 300, 100, w=40, h=120)],
        [_edge("e1", "a", "b")])


# ───────────────────────── route_graph ─────────────────────────

def test_transit_detours_with_clearance():
    """Транзит сквозь бокс -> обход с клиренсом route_buffer."""
    from shapely.geometry import LineString, box as shp_box

    g = _transit_graph()
    routed = route_graph(g)
    assert "e1" in routed
    wps = routed["e1"]
    assert wps, "обход обязан дать изломы"

    e = next(iter(edges(g)))
    e["waypoints"] = wps
    pl = edge_polyline(e)
    assert _polyline_ortho(e), f"маршрут не ортогонален: {pl}"
    wall = shp_box(280, 40, 320, 160)
    ls = LineString(pl)
    assert not ls.intersects(wall.buffer(-1e-9)), "обход всё ещё прошивает бокс"
    assert ls.distance(wall) >= BUF - 0.5, (
        f"клиренс {ls.distance(wall):.2f} < буфера {BUF}")


def test_waypoints_exclude_endpoints_and_are_yx():
    """Мина терминальной точки (T-A.5): концы в waypoints не дублируются."""
    g = _transit_graph()
    routed = route_graph(g)
    e = next(iter(edges(g)))
    sp, tp = e["source_point"], e["target_point"]
    for wp in routed["e1"]:
        assert len(wp) == 2
        assert abs(wp[0] - sp[0]) > 1e-6 or abs(wp[1] - sp[1]) > 1e-6
        assert abs(wp[0] - tp[0]) > 1e-6 or abs(wp[1] - tp[1]) > 1e-6
    # [y, x]: первый излом обхода уводит по X (горизонталь от правой грани a),
    # то есть Y меняется ПОЗЖЕ. Ошибка осей дала бы обратную картину.
    first = routed["e1"][0]
    assert abs(first[1] - sp[1]) > 1e-6, "первый сегмент обязан уйти по X"


def test_manhattan_path_between_offset_blocks():
    """Смещённые блоки без препятствий -> манхэттен-путь (L, без диагоналей).

    Этап A: маршрут начинается в ПОРТУ, поэтому вместе с waypoints
    применяются и новые концы (`ends_out`) — как это делает apply_routing.
    """
    g = _graph([_block("a", 100, 100), _block("b", 300, 260)],
               [_edge("e1", "a", "b")])
    ends = {}
    routed = route_graph(g, None, ends)
    assert "e1" in routed and routed["e1"], "смещённой паре нужен излом"
    e = next(iter(edges(g)))
    e["waypoints"] = routed["e1"]
    if "e1" in ends:
        e["source_point"], e["target_point"] = ends["e1"]
    assert _polyline_ortho(e), f"диагональ в маршруте: {edge_polyline(e)}"


def test_straight_pair_stays_straight():
    """Соосной паре без препятствий изломы не выдумываются."""
    g = _graph([_block("a", 100, 100), _block("b", 300, 100)],
               [_edge("e1", "a", "b")])
    assert route_graph(g) == {}


def test_manual_route_and_operator_waypoints_untouched():
    """Неприкосновенность: _manual_route и waypoints оператора не роутятся."""
    manual = _edge("m1", "a", "b", _manual_route=True)
    op = _edge("o1", "a", "b")
    g = _graph([_block("a", 100, 100), _block("b", 500, 100),
                _block("w", 300, 100, w=40, h=120)],
               [manual, op])
    op_wps = [[60.0, 300.0]]                      # правка оператора
    op["waypoints"] = deepcopy(op_wps)
    before_manual = deepcopy(manual)
    routed = route_graph(g)
    assert "m1" not in routed and "o1" not in routed
    assert manual == before_manual
    assert op["waypoints"] == op_wps


def test_unreachable_pin_fallback_keeps_input():
    """Молчаливый fallback: пин упёрт в стену -> ребру остаётся вход.

    Сценарий пробника: конец на правой грани `a` (ConnDir вправо), стена
    вплотную к этой грани; libavoid не падает, а рисует маршрут СКВОЗЬ
    стену. Детект прошивания обязан выбросить такой маршрут.
    """
    a = _block("a", 100, 100)
    b = _block("b", 100, 300)
    wall = _block("w", 140, 100, w=40, h=40)      # вплотную справа от a
    e = _edge("e1", "a", "b",
              source_point=[100.0, 120.0],        # [y, x]: правая грань a
              target_point=[280.0, 100.0])        # верхняя грань b
    g = _graph([a, b, wall], [e], seat=False)
    before = deepcopy(e)
    routed = route_graph(g)
    assert "e1" not in routed, "fallback-маршрут сквозь стену принят"
    assert e == before


def test_route_survives_aggressive_gc():
    """SWIG-GC: агрессивный gc в процессе транзакции не крадёт пины/шейпы."""
    baseline = route_graph(_transit_graph())
    assert baseline
    g = _transit_graph()
    gc.collect()
    old = gc.get_threshold()
    gc.set_threshold(1, 1, 1)
    try:
        stressed = route_graph(g)
    finally:
        gc.set_threshold(*old)
    assert stressed == baseline


def test_pin_lands_on_port_not_corner():
    """Этап A: пара «бокс -> крупный полигон», канон-луч сажает конец бокса
    в УГОЛ рамки (жалоба заказчика: edge_52 graph_edited_33). Пин роутинга
    обязан встать в ПОРТ (центр грани), и после apply_routing конец ребра —
    портовый, не угловой."""
    a = _block("a", 100, 100)                       # bbox [80, 80, 120, 120]
    poly = {"id": "p", "type": "block", "class_name": "testpoly",
            "centroid": [300.0, 400.0],             # [y, x]
            "bbox": [300.0, 200.0, 500.0, 400.0],
            "segmentation": [300.0, 200.0, 500.0, 200.0,
                             500.0, 400.0, 300.0, 400.0]}
    g = _graph([a, poly], [_edge("e1", "a", "p")])
    e = next(iter(edges(g)))
    # канон действительно даёт угол бокса: обе координаты на границах рамки
    assert e["source_point"] == [120.0, 120.0], "фикстура не воспроизводит угол"

    base = deepcopy(g)
    stats = apply_routing(g, base, base, LayoutParams())
    assert stats["routed"] >= 1 and not stats["reverted"]
    sp = e["source_point"]
    # конец — в порту (центр правой грани), не в углу
    assert sp == [100.0, 120.0], f"конец не в порту: {sp}"
    x_on = min(abs(sp[1] - 80.0), abs(sp[1] - 120.0)) <= 1.0
    y_on = min(abs(sp[0] - 80.0), abs(sp[0] - 120.0)) <= 1.0
    assert not (x_on and y_on), f"конец остался на углу рамки: {sp}"
    assert _polyline_ortho(e), f"маршрут не ортогонален: {edge_polyline(e)}"


# ───────────────────────── apply_routing ─────────────────────────

def _amnesty_graph():
    """Класс К-10: смена стороны, порождённая угловой амнистией `_side_set`.

    `a` — бокс 40x40 (bbox [80, 80, 120, 120]); канон сажает конец `e1`
    в УГОЛ рамки (120, 120), то есть в стороны {R, B}. Пин роутинга — порт
    в центре ПРАВОЙ грани (120, 100), то есть {R}. В базах (orig/v16) тот же
    конец лежит на середине НИЖНЕЙ грани, поэтому допустимое множество судьи
    `_gate._side_changed` — только {B}: судья считает уход на {R} сменой
    стороны, а пер-рёберный сторож `apply_routing` пропускает ход через
    угловую амнистию ({R} пересекается с {R, B}).

    `e2` — независимый транзит q -> r сквозь бокс `w` в другом углу листа:
    его обход законен и к сторонам отношения не имеет. Он и показывает цену
    полного отката.

    -> (graph, orig, v16)
    """
    a = _block("a", 100, 100)                       # bbox [80, 80, 120, 120]
    poly = {"id": "p", "type": "block", "class_name": "testpoly",
            "centroid": [300.0, 400.0],             # [y, x]
            "bbox": [300.0, 200.0, 500.0, 400.0],
            "segmentation": [300.0, 200.0, 500.0, 200.0,
                             500.0, 400.0, 300.0, 400.0]}
    g = _graph([a, poly, _block("q", 100, 800), _block("r", 500, 800),
                _block("w", 300, 800, w=40, h=120)],
               [_edge("e1", "a", "p"), _edge("e2", "q", "r")])
    e1 = next(e for e in edges(g) if e["id"] == "e1")
    assert e1["source_point"] == [120.0, 120.0], "фикстура не воспроизводит угол"
    orig = deepcopy(g)
    b1 = next(e for e in edges(orig) if e["id"] == "e1")
    b1["source_point"] = [120.0, 100.0]     # [y, x]: середина НИЖНЕЙ грани
    return g, orig, deepcopy(orig)


def test_corner_amnesty_side_change_reverts_only_guilty_edge():
    """К-10: смена стороны снимает ВИНОВНОЕ ребро, а не весь роутинг.

    До правки оба маршрута принимал пер-рёберный сторож, судья прогона видел
    рост `side_changed`, и полный откат забирал вместе с виновным `e1`
    законный обход `e2` (бокс `w` возвращался на магистраль).
    """
    g, orig, v16 = _amnesty_graph()
    e1 = next(e for e in edges(g) if e["id"] == "e1")
    e2 = next(e for e in edges(g) if e["id"] == "e2")
    assert spread.box_on_magi_drawn(g, nodes_by_id(g)) == {"w"}

    stats = apply_routing(g, orig, v16, LayoutParams())
    assert stats == {"routed": 1, "reverted": False, "reasons": [],
                     "magi_before": 1, "magi_after": 0}
    # виновное ребро снято адресно: конец вернулся на канон, обхода нет
    assert e1["source_point"] == [120.0, 120.0] and e1["waypoints"] == []
    # соседний законный обход пережил гейт — ради него правка и делалась
    assert e2["waypoints"], "обход e2 убит вместе с чужой сменой стороны"
    assert spread.box_on_magi_drawn(g, nodes_by_id(g)) == set()
    assert _gate.verify(g, orig, v16, None)["side_changed"] == 0


# ⚠ Сторожа «остальные 8 причин по-прежнему валят ВЕСЬ прогон» здесь НЕТ
# СОЗНАТЕЛЬНО. Он был написан (инъекция в `spread.defects` через monkeypatch,
# ожидание `reasons == ['defects']` и возврат обхода `e2`) и работал, но его
# ИСПОЛНЕНИЕ в полном наборе детонировало access violation в
# `tests/ui/test_tab_close_stops_threads.py`: 3 прогона из 3 против 0 из 2 без
# него, каждый раз на ДРУГОМ тесте того файла (:546, :608, :649) — то есть
# портится состояние процесса, а не конкретный тест. Бисект и числа —
# `MEASUREMENTS §AL4.9`. Ветка полного отката к правке К-10 не относится
# (диффом не тронута), проверена ЗОНДОМ (там же), поэтому набор её не стережёт,
# пока причина детонации не разобрана отдельным пунктом.


def test_apply_routing_clears_transit_and_keeps_nodes():
    """Гейт-обёртка: magi падает, координаты узлов не тронуты вообще."""
    g = _transit_graph()
    base = deepcopy(g)
    nodes_before = deepcopy(g["nodes"])

    byid = nodes_by_id(g)
    assert spread.box_on_magi_drawn(g, byid) == {"w"}, "синтетика без транзита"

    stats = apply_routing(g, base, base, LayoutParams())
    assert stats["routed"] == 1 and not stats["reverted"]
    assert g["nodes"] == nodes_before, "роутинг сдвинул узлы"
    assert spread.box_on_magi_drawn(g, nodes_by_id(g)) == set()

    e = next(iter(edges(g)))
    assert e["waypoints"], "waypoints не применены"
    assert _polyline_ortho(e)
    # посадка концов — по осям подводящих сегментов (канон reseat_edge)
    pl = edge_polyline(e)
    assert min(abs(pl[1][0] - pl[0][0]), abs(pl[1][1] - pl[0][1])) <= 0.5
