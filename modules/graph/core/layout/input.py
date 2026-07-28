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
"""
from __future__ import annotations

from copy import deepcopy

from ..pretransform import (
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
    """
    g = deepcopy(validated_graph)
    if image_hw is None:
        size = g.get("graph", {}).get("image_size")
        if not size:
            raise ValueError("image_size отсутствует — передай image_hw=(h, w)")
        image_hw = (size[0], size[1])
    transform = transform_to_canvas(g, image_hw)
    g.setdefault("graph", {})["canvas_transform"] = transform
    apply_fixed_sizes(g, _build_adjacency(g))
    reproject_edge_endpoints(g)
    return g, transform
