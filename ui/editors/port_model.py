# -*- coding: utf-8 -*-
"""port_model.py — портовая модель посадки концов рёбер (этап A): реэкспорт.

Чистая логика переехала в `modules/graph/core/layout/ports.py` (финал этапа
A): серверный libavoid-роутинг сажает пины в те же порты тем же судьёй
(`choose_port`), что и drag редактора — сторож == судья. Здесь остаётся
только точка входа для UI-кода и тестов; API и поведение — байт-в-байт.
"""
from __future__ import annotations

from modules.graph.core.ports import (  # noqa: F401
    BACKSIDE_EPS,
    MIN_POLY_EDGE,
    PIN_KEYS,
    PORT_MATCH_TOL,
    RADICAL_LEN_FACTOR,
    STRAIGHT_TOL,
    _backside,
    _bends,
    _l1,
    _node_cxy,
    _poly_contour,
    _poly_ports,
    _pt_in_poly,
    all_ports,
    candidate_ports,
    choose_port,
    clear_edge_pin,
    edge_pin,
    edge_ref,
    is_on_port,
    lock_respected,
    manual_ports,
    nearest_port,
    pin_role,
    pinned_on_node,
    pinned_port,
    rescale_edge_pins,
    set_edge_pin,
)
