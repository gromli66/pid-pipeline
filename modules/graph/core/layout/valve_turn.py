# -*- coding: utf-8 -*-
"""valve_turn.py — поворот одиночного клапана по трубе (Р5, блок 5).

Клапан степени 1 (по рёбрам; `contact.axial_only`) принимает трубу только
в осевую грань, в талию. Если его единственная труба пришла ПОПЕРЁК оси
(у H |dy| > |dx| до опорной точки партнёра, у V наоборот), сервер:
  1. поворачивает клапан по трубе — рамка w↔h вокруг талии
     (`contact.box_around_waist`, центроид-талия на месте, как поворот Р3
     редактора) — и сдвигает его поперёк новой оси на «линия трубы −
     талия», чтобы труба вошла в талию прямой;
  2. сдвиг упирается в соседа — только поворот, роутер строит Z;
  3. повернуть нельзя — ось прежняя, конец уходит в осевую талию, роутер
     строит крючок (до 3 изломов; план граней `seating.axial_faces`).
Гейт каждой попытки: `graph_access.box_hits` (один судья «упирается в
соседа» с редактором) и судьи раскладки на всём листе — наложения форм,
дефекты труб, выход за поле — не растут. Откат — снимком узла.
Пин оператора на конце у клапана — истина: такой клапан не трогается.
predohran (`AXIAL_EXEMPT_SKINS`) и старый холст (`LEGACY_KEY`) — вне правила.
"""
from __future__ import annotations

from .. import contact
from ..graph_access import (box_hits, degrees, edge_ends, edges, move_node,
                            node_cxy, nodes_by_id)
from ..ports import pinned_port
from ..seating import _pref_val
from . import spread
from ._shapes import has_polygon

SHIFT_EPS = 0.25   # px: расхождение талии с линией трубы, которое не сдвигаем


def _judges(graph, legal, floor):
    return (spread.overlaps(graph, legal),
            len(spread.defects(graph, nodes_by_id(graph), floor)),
            spread.margin_violation(graph))


def _candidates(graph, byid):
    """[(узел, ребро)] — клапаны Р5 степени 1 без пина на своём конце."""
    deg = degrees(graph)
    one = {}
    for e in edges(graph):
        for nid in edge_ends(e):
            one.setdefault(nid, e)
    out = []
    for n in graph.get("nodes", []):
        e = one.get(n.get("id"))
        if e is None or deg.get(n["id"]) != 1 or has_polygon(n) \
                or not contact.axial_only(n, 1) or n.get("bbox") is None \
                or pinned_port(n, e) is not None:
            continue
        out.append((n, e))
    return out


def turn_single_valves(graph, legal=None, p=None):
    """Повернуть клапаны степени 1 с трубой поперёк оси. -> stats.

    stats: turned (повёрнуты, со сдвигом или без), turn_shift_blocked
    (повёрнуты без сдвига — сдвиг упёрся), turn_blocked (повернуть нельзя,
    ось прежняя — крючок), shift_px (модули принятых сдвигов), blocked_ids
    (id клапанов turn_blocked — стенд `layout_bench` называет их поимённо).
    """
    floor = p.floor if p is not None else spread.FLOOR
    byid = nodes_by_id(graph)
    stats = {"turned": 0, "turn_shift_blocked": 0, "turn_blocked": 0,
             "shift_px": [], "blocked_ids": []}
    base = None
    for n, e in _candidates(graph, byid):
        s, t = edge_ends(e)
        role = "source" if s == n["id"] else "target"
        wps = e.get("waypoints") or []
        part = byid.get(t if role == "source" else s)
        if wps:
            w = wps[0] if role == "source" else wps[-1]
            rx, ry = float(w[1]), float(w[0])
        elif part is not None:
            rx, ry = _pref_val(part, "V", True), _pref_val(part, "H", True)
        else:
            continue
        cx, cy = node_cxy(n)
        dx, dy = rx - cx, ry - cy
        if (abs(dy) <= abs(dx)) if n["_axis"] == "H" else (abs(dx) <= abs(dy)):
            continue                                  # труба вдоль оси
        if base is None:
            base = _judges(graph, legal, floor)
        axis = "V" if n["_axis"] == "H" else "H"
        x1, y1, x2, y2 = n["bbox"]
        snap = (n["_axis"], list(n["bbox"]), list(n["centroid"]))
        turned = contact.box_around_waist(dict(n, _axis=axis), n["centroid"],
                                          y2 - y1, x2 - x1)
        d = dx if axis == "V" else dy                 # линия трубы − талия
        tries = [(0.0, 0.0)]
        if abs(d) > SHIFT_EPS:
            tries.insert(0, (d, 0.0) if axis == "V" else (0.0, d))
        done = None
        for sx, sy in tries:
            bb = [turned[0] + sx, turned[1] + sy, turned[2] + sx, turned[3] + sy]
            if box_hits(byid, n["id"], bb):
                continue
            n["_axis"], n["bbox"] = axis, list(turned)
            move_node(n, sx, sy)
            now = _judges(graph, legal, floor)
            if all(a <= b + 1e-9 for a, b in zip(now, base)):
                base, done = now, (sx, sy)
                break
            n["_axis"], n["bbox"], n["centroid"] = snap
        if done is None:
            stats["turn_blocked"] += 1
            stats["blocked_ids"].append(n["id"])
            continue
        stats["turned"] += 1
        if done == (0.0, 0.0):
            stats["turn_shift_blocked"] += len(tries) > 1
        else:
            stats["shift_px"].append(round(abs(d), 2))
    return stats
