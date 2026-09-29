# -*- coding: utf-8 -*-
"""layout — автоматическая раскладка P&ID-графа на холсте 1920x1080.

Единственный вход: `layout(graph, params) -> (graph, stats)`. Вход — граф уже
В КООРДИНАТАХ ХОЛСТА (выход `pretransform`), не в пикселях растра.

Два слоя:
  1. РАССТАНОВКА (`axial`) — переменные решателя это координаты рядов и
     колонок, а не узлов: узел стоит там, где стоит его ось. Отсюда
     сохраняются ряды и колонки исходной схемы (узнаваемость).
  2. РАЗДВИГАНИЕ (`spread`) — расстановка оставляет часть труб невидимыми
     (зазор между формами < FLOOR); слой чинит их лестницей механизмов, каждый
     ход проходит приёмку из 10 условий, любое нарушение — откат.

Меняются только `centroid`, `bbox`, `segmentation`, `source_point`/
`target_point`, `waypoints`. Топология, связность, классы, id — не трогаются.

Происхождение, замеры и отвергнутые гипотезы — `_scratch/layout_align/
SOLUTION.md`; порядок интеграции — `docs/planning/AUTO_LAYOUT_INTEGRATION.md`.
"""
from __future__ import annotations

from copy import deepcopy

from . import _shapes, axial, spread
from ..graph_access import edges, nodes_by_id
from ._avoid_binding import avoid_available
from .params import LayoutParams
from ..seating import reseat_all_endpoints, straighten_unrouted

__all__ = ["layout", "LayoutParams"]

_AUTO = object()   # сентинел: построить ShapeIndex самому (None — валидное
                   # значение «считать по габаритам», поэтому не годится)


# Пороги, которые слой раздвигания читает из модульных констант в теле
# функций: подмена присвоением работает (так их менял и CLI стенда).
_WIRED = {
    "margin": ("MARGIN", float),
    "skew_max": ("SKEW_MAX", float),
    "compound_max": ("COMPOUND_MAX", float),
    "tail_max": ("TAIL_MAX", int),
    "band_reach": ("BAND_REACH", float),
    "band_tries": ("BAND_TRIES", int),
}
# Пороги, захваченные ДЕФОЛТАМИ АРГУМЕНТОВ на импорте модуля (`def try_shift(
# ..., pen_tol=PEN_TOL)`): присвоение константы на них уже не влияет. Пока
# значение равно перенесённому — всё честно. Как только их понадобится менять
# (Э7, вынос в configs), придётся править сигнатуры в spread.py — и лучше
# узнать об этом исключением здесь, чем получить молча проигнорированный порог.
_FROZEN = {"grow_max": "GROW_MAX", "pen_tol": "PEN_TOL",
           "order_tie": "ORDER_TIE", "order_near": "ORDER_NEAR",
           "band_rounds": "BAND_ROUNDS", "canvas": "CANVAS"}


def _apply(params):
    """Пороги -> модульные константы слоя раздвигания.

    Слой перенесён со стенда как есть, а там пороги живут модульными
    константами. Раскладка считается в prefork-воркере по одной задаче на
    процесс, поэтому подмена безопасна; протаскивать параметры через полторы
    тысячи строк — тот самый «заодно улучшил», который Э2 запрещает.
    """
    stuck = []
    for field, const in _FROZEN.items():
        want, have = getattr(params, field), getattr(spread, const)
        if type(want)(have) != want:
            stuck.append(f"{field}={want!r} (в spread.{const} — {have!r})")
    if stuck:
        raise ValueError(
            "эти пороги нельзя задать через LayoutParams: перенесённый код "
            "захватил их дефолтами аргументов на импорте, присвоение молча не "
            "подействует. Нужна правка сигнатур в layout/spread.py (Э7). "
            "Запрошено: " + "; ".join(stuck))

    for field, (const, cast) in _WIRED.items():
        setattr(spread, const, cast(getattr(params, field)))
    spread.DEBUG_COMP = False


def shape_index(graph, params=None):
    """`_shapes.ShapeIndex` по графу, либо None, если сверка по габаритам.

    Таблица легальных пар считается по ДЕТЕКТИРОВАННЫМ габаритам, которые
    `canvas_input.to_canvas` кладёт в `graph.detected_bbox`. Их нет — значит
    граф пришёл не штатным путём (синтетика, тест, старый артефакт); тогда
    амнистировать нечего и пары считаются пустыми, а форма всё равно
    учитывается.
    """
    p = params or LayoutParams()
    if not p.shape_overlaps:
        return None
    detected = graph.get("graph", {}).get("detected_bbox")
    legal = _shapes.legal_pairs(graph, detected, p.border_tol) if detected \
        else ()
    return _shapes.ShapeIndex(graph, legal)


def place(graph, params=None, shapes=_AUTO):
    """Только расстановка (слой 1). Мутирует graph, возвращает статистику.

    Рёбра становятся прямыми: маршрут раскладка не сохраняет — `waypoints` и
    `path` очищаются, это «пересобрать раскладку», а не «подправить».
    """
    p = params or LayoutParams()
    if shapes is _AUTO:
        shapes = shape_index(graph, p)
    bands = None
    if p.band_block:
        # ось полосы берётся по НАРИСОВАННОЙ ориентации ребра, то есть по
        # холсту, который видит оператор: концы там посажены, а не
        # спроецированы. Вход раскладки для этого пересаживается на копии.
        drawn = deepcopy(graph)
        reseat_all_endpoints(drawn)
        bands = axial.build_bands(graph, drawn)
    for e in edges(graph):
        e["waypoints"] = []
        if "path" in e:
            e["path"] = []
        e.pop("_slack", None)      # метка вынужденной прямой прошлого прогона
    stats = axial.place(graph, canvas=tuple(p.canvas), fill=p.fill,
                        inset=float(p.margin), shapes=shapes, bands=bands)
    reseat_all_endpoints(graph)
    return stats


def layout(graph, params=None, stages=None):
    """Полная раскладка: расстановка + раздвигание. -> (graph, stats).

    graph мутируется на месте и возвращается тем же объектом.

    stages: если передан dict, в него кладутся промежуточные геометрии —
    `orig` (вход, до раскладки) и `placed` (после расстановки, до
    раздвигания). Нужны приёмке `tools/layout_bench.py`: все запреты
    сравниваются с базой входа, а не с нулём, поэтому база должна быть под
    рукой. На результат не влияет.
    """
    p = params or LayoutParams()
    _apply(p)
    shapes = shape_index(graph, p)

    # до-раскладочная геометрия: источник оси «оригинал» в лестнице ходов
    # (§4.5 SOLUTION.md) — у ребра к крупному блоку центроид лежит глубоко
    # внутри, и по центроидам получается диагональ, хотя труба входит по оси.
    orig = deepcopy(graph)

    axial_stats = place(graph, p, shapes=shapes)
    base_v16 = deepcopy(graph)
    if stages is not None:
        # снимки, а не сами объекты: `orig` и `base_v16` уходят в раздвигание
        # как базы сравнения, и приёмка обязана видеть их такими, какими они
        # были на входе слоя
        stages["orig"] = deepcopy(orig)
        stages["placed"] = deepcopy(base_v16)

    byid = nodes_by_id(graph)
    before = len(spread.defects(graph, byid, p.floor))

    spread_stats = spread.spread(
        graph, orig, p.floor, p.target, p.passes,
        base_v16=base_v16, band=p.band, unlock_zero=p.unlock_zero,
        compound=p.compound, comp_push=p.comp_push,
        legal=shapes.legal if shapes is not None else None)
    # Финальная посадка — по модели точек прикрепления (решение заказчика
    # 2026-09-22): выверенный порт клапана, середина рамки с допуском,
    # слоты вокруг опорной точки. Внутренние стадии (расстановка, пробы
    # раздвигания) сажают каноном со слабиной — раскладка не теряет ходов.
    # Сервер — всегда с правилом Р5 (axial): клапан степени 1–2 принимает
    # трубы только на осевых гранях, в талию (`seating.axial_faces`); тот же
    # план граней у пинов роутера и у страховки.
    reseat_all_endpoints(graph, model=True, slots=True, axial=True)

    # Э7-b: этап роутинга — обход чужих форм libavoid. Только waypoints рёбер,
    # узлы не двигаются; хуже входа — полный откат внутри apply_routing.
    # Без биндинга (или routing=False) раскладка работает как раньше.
    routing_stats = None
    if p.routing and avoid_available():
        from . import avoid_router
        routing_stats = avoid_router.apply_routing(
            graph, orig, base_v16, p,
            legal=shapes.legal if shapes is not None else None)

    # Роутер переводит часть концов в порты других граней, страховка
    # выпрямляет неразведённые ступеньки (и выводит их из слотов): занятость
    # граней меняется, разложенные слоты устаревают. Посадка по модели
    # (рёбра с маршрутом — по осям своих стабов, колено едет за слотом) и
    # страховка по кругу, пока страховке нечего выпрямлять — итог неподвижен
    # для той же посадки при открытии холста (`seat_edge_endpoints`).
    # Страховка: ступенька, которую роутер не развёл, выпрямляется, а не
    # остаётся косой (середина рамки уступает на всю грань; порт клапана —
    # только последним рубежом, счётчик ports_yielded).
    straight_stats = {}
    straightened = yielded = 0
    for _round in range(3):
        if routing_stats is not None or _round:
            reseat_all_endpoints(graph, model=True, slots=True, axial=True)
        step = {}
        n = straighten_unrouted(graph, stats=step, axial=True)
        straightened += n
        yielded += step.get("ports_yielded", 0)
        if not n:
            break
    straight_stats["ports_yielded"] = yielded

    after = len(spread.defects(graph, nodes_by_id(graph), p.floor))
    return graph, {"defects_before": before, "defects_after": after,
                   "axial": axial_stats, "spread": spread_stats,
                   "routing": routing_stats, "straightened": straightened,
                   "ports_yielded": straight_stats.get("ports_yielded", 0)}
