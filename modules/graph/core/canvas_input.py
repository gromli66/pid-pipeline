# -*- coding: utf-8 -*-
"""input.py — построение входа раскладки: растр -> холст 1920x1080.

Это `pretransform` БЕЗ шага `declust`. Расклейка наложений вдоль трубы нужна
тому, кто открывает холст без раскладки (сегодняшнее поведение вкладки), а
осевая расстановка ставит узлы с нуля — declust перед ней только сдвинул бы
центроиды, по которым строится сама осевая модель (см. §3.3 плана
`docs/planning/AUTO_LAYOUT_INTEGRATION.md`).

Эталон корпуса стенда посчитан ровно на этом входе
(`_scratch/layout_align/harness/prepare.py`), поэтому шаги и их порядок здесь
менять нельзя — иначе бит-в-бит паритет Э2 перестаёт что-либо доказывать.
Единственное исключение — первый шаг `normalize_parallel_edges` (кратные
рёбра, решение Р7, модуль `parallel_edges`): на графе без кратных пар он ничего
не меняет, поэтому паритет Э2 на git-корпусе (0 пар) сохраняется; на
storage-графах с парами эталоны пересняты (Д6).
"""
from __future__ import annotations

from copy import deepcopy

from .parallel_edges import normalize_parallel_edges
from .pretransform import (
    _build_adjacency,
    apply_fixed_sizes,
    reproject_edge_endpoints,
    transform_to_canvas,
)

__all__ = ["to_canvas"]


def to_canvas(validated_graph, image_hw=None):
    """graph_validated (px растра) -> граф холста. -> (graph, transform).

    Вход не мутируется. `image_hw = (height, width)`; по умолчанию берётся из
    `graph.image_size` самого графа.

    Побочно кладёт в `graph.detected_bbox` габариты узлов ДО подмены словарём
    размеров. Снять их можно только здесь: после `apply_fixed_sizes`
    детектированные габариты утрачены, а они — единственный способ отличить
    наложение, пришедшее из построения и проверки (легальное, решение
    заказчика), от созданного словарём (нелегального). Замер по корпусу:
    на плотных схемах словарь создаёт 95%+ всех наложений (51b339ab 382 из
    387), на c2f79462 наоборот — 11 из 44.

    Здесь лежат ЧИСЛА, а не геометрия: `canvas_input` тянет `text_import`, а
    тот идёт в UI, где shapely нет. Пары считает слой раскладки
    (`layout/_shapes.py`) по этим габаритам. `apply_fixed_sizes` двигает bbox
    и — у клапанов — центроид: он встаёт в точку талии на линии трубы
    (решение заказчика 2026-09-22), остальные центроиды те же.
    """
    g = deepcopy(validated_graph)
    # кратные рёбра (Р7) на копии; степень — только у затронутых узлов: у
    # старых graph_validated поле degree протухло, полный пересчёт сдвинул бы всё
    par = normalize_parallel_edges(g.get("nodes", []), g.get("links", []),
                                   keys=("source", "target"))
    if par["nodes_touched"]:
        deg = dict.fromkeys(par["nodes_touched"], 0)
        for e in g["links"]:
            for nid in (e.get("source"), e.get("target")):
                if nid in deg:
                    deg[nid] += 1
        for n in g["nodes"]:
            if n.get("id") in deg:
                n["degree"] = deg[n["id"]]
    if image_hw is None:
        size = g.get("graph", {}).get("image_size")
        if not size:
            raise ValueError("image_size отсутствует — передай image_hw=(h, w)")
        image_hw = (size[0], size[1])
    transform = transform_to_canvas(g, image_hw)
    g.setdefault("graph", {})["canvas_transform"] = transform
    detected = {n["id"]: list(n["bbox"]) for n in g.get("nodes", [])
                if n.get("bbox") and len(n["bbox"]) == 4}
    apply_fixed_sizes(g, _build_adjacency(g))
    reproject_edge_endpoints(g)
    g["graph"]["detected_bbox"] = detected
    return g, transform
