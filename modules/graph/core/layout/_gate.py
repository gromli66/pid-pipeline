# -*- coding: utf-8 -*-
"""_gate.py — топологический гейт: что раскладке запрещено сломать.

Источник: стенд `_scratch/layout_align/topo_gate.py` (строки 56-260).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.
Инварианты: новых диагоналей нет, порядок узлов не переставлен, связность
цела, прямые не сломаны, стороны входа сохранены, наложений нет, боксов на
магистрали не прибавилось.
"""
from __future__ import annotations

import math

import numpy as np

from . import _overlaps
from . import _triggers as triggers
from ..graph_access import (edge_ends, edge_polyline, edges, is_connector,
                            node_cxy, nodes_by_id)

TIE_EPS = 2.0      # px: пара считалась «на одной оси» в оригинале
                   # (перенесено из harness/metrics.py стенда)

FLOOR = 6.0        # px: короче — ориентация не определена
MAGI = 60.0        # px: порог магистрали (как в triggers)
DIAG_TOL_DEG = 20.0  # угол от оси, выше которого ребро — диагональ (D)
STRAIGHT_TOL = 1.5   # px: сегмент магистрали ортогонален, если min(|dx|,|dy|)<=
SIDE_EPS = 1.5       # px: конец «на стороне», если ближе этого к её границе


# ───────────────────────── ориентация ─────────────────────────

def _orient(dx, dy, floor=FLOOR, tol_deg=DIAG_TOL_DEG):
    """H / V / D / None(вырожденная) по вектору (dx, dy)."""
    if math.hypot(dx, dy) < floor:
        return None
    ang = math.degrees(math.atan2(min(abs(dx), abs(dy)), max(abs(dx), abs(dy))))
    if ang > tol_deg:
        return "D"
    return "H" if abs(dx) >= abs(dy) else "V"


def _edge_orient(e, byid, floor=FLOOR, tol_deg=DIAG_TOL_DEG):
    """Ориентация ребра по центроидам его узлов."""
    s, t = edge_ends(e)
    a, b = byid.get(s), byid.get(t)
    if not a or not b:
        return None
    ax, ay = node_cxy(a)
    bx, by = node_cxy(b)
    return _orient(bx - ax, by - ay, floor, tol_deg)


def _drawn_len(e):
    pl = edge_polyline(e)
    if len(pl) < 2:
        return 0.0
    return sum(math.hypot(pl[i + 1][0] - pl[i][0], pl[i + 1][1] - pl[i][1])
               for i in range(len(pl) - 1))


def _edge_ortho(e, tol=STRAIGHT_TOL):
    """Ортогональна ли ВСЯ полилиния ребра (каждый сегмент H или V)."""
    pl = edge_polyline(e)
    if len(pl) < 2:
        return True
    for i in range(1, len(pl)):
        dx = abs(pl[i][0] - pl[i - 1][0])
        dy = abs(pl[i][1] - pl[i - 1][1])
        if dx < 1e-9 and dy < 1e-9:
            continue
        if min(dx, dy) > tol:
            return False
    return True


# ───────────────────────── стороны входа ─────────────────────────

def _side_set(px, py, bb, eps=SIDE_EPS):
    """Множество сторон bbox, к которым конец (px,py) прижат (в пределах eps).

    На углу конец попадает в обе смежные стороны -> устраняет ложные флипы
    угловых концов между реперами.
    """
    x1, y1, x2, y2 = bb
    d = {"L": abs(px - x1), "R": abs(px - x2),
         "T": abs(py - y1), "B": abs(py - y2)}
    m = min(d.values())
    return {k for k, v in d.items() if v <= m + eps}


def _block_endpoints(e):
    """[(node_id, x, y)] по концам ребра, x,y из source/target_point."""
    s, t = edge_ends(e)
    out = []
    sp, tp = e.get("source_point"), e.get("target_point")
    if sp:
        out.append((s, float(sp[1]), float(sp[0])))
    if tp:
        out.append((t, float(tp[1]), float(tp[0])))
    return out


# ───────────────────────── инварианты ─────────────────────────

def _new_diagonals(after_e, orig_e, after_b, orig_b):
    """orig был H/V -> after стал D. Считаем по общим рёбрам."""
    n = 0
    for eid, e in after_e.items():
        oe = orig_e.get(eid)
        if oe is None:
            continue
        oo = _edge_orient(oe, orig_b)
        oa = _edge_orient(e, after_b)
        if oo in ("H", "V") and oa == "D":
            n += 1
    return n


def _order_broken(after, base_v16):
    """Число пар узлов, перевернувших порядок по X или Y vs base_v16."""
    sa = {n["id"]: node_cxy(n) for n in after.get("nodes", [])
          if "centroid" in n}
    sb = {n["id"]: node_cxy(n) for n in base_v16.get("nodes", [])
          if "centroid" in n}
    ids = sorted(set(sa) & set(sb))
    if len(ids) < 2:
        return 0
    A = np.array([sb[i] for i in ids], dtype=float)   # опора (v16)
    B = np.array([sa[i] for i in ids], dtype=float)   # after
    iu = np.triu_indices(len(ids), k=1)
    broke = np.zeros(len(iu[0]), dtype=bool)
    for ax in (0, 1):
        da = (A[:, ax][:, None] - A[:, ax][None, :])[iu]
        db = (B[:, ax][:, None] - B[:, ax][None, :])[iu]
        established = (np.abs(da) >= TIE_EPS) & (np.abs(db) >= TIE_EPS)
        flip = established & (np.sign(da) != np.sign(db))
        broke |= flip
    return int(broke.sum())


def _connectivity_changed(after, orig):
    na = {n["id"] for n in after.get("nodes", [])}
    no = {n["id"] for n in orig.get("nodes", [])}
    ea = {e["id"] for e in edges(after)}
    eo = {e["id"] for e in edges(orig)}
    return (na != no) or (ea != eo)


def _straight_broken(after_e, v16_e):
    """Магистрали v16 (len>MAGI, ортогональные), потерявшие прямизну в after."""
    n = 0
    for eid, ve in v16_e.items():
        if _drawn_len(ve) <= MAGI:
            continue
        if not _edge_ortho(ve):
            continue
        ae = after_e.get(eid)
        if ae is None:
            continue
        if not _edge_ortho(ae):
            n += 1
    return n


def _side_changed(after_e, orig_e, v16_e, after_b, orig_b, v16_b):
    """Сторона входа в блок ушла с допустимой {orig} ∪ {v16}."""
    n = 0
    for eid, e in after_e.items():
        oe, ve = orig_e.get(eid), v16_e.get(eid)
        if oe is None or ve is None:
            continue
        epo = {nid: (x, y) for nid, x, y in _block_endpoints(oe)}
        epv = {nid: (x, y) for nid, x, y in _block_endpoints(ve)}
        for nid, x, y in _block_endpoints(e):
            na, no, nv = after_b.get(nid), orig_b.get(nid), v16_b.get(nid)
            if not na or is_connector(na):
                continue
            bb_a = na.get("bbox")
            if not bb_a or nid not in epo or nid not in epv:
                continue
            allowed = set()
            if no and no.get("bbox"):
                allowed |= _side_set(*epo[nid], no["bbox"])
            if nv and nv.get("bbox"):
                allowed |= _side_set(*epv[nid], nv["bbox"])
            cur = _side_set(x, y, bb_a)
            if allowed and not (cur & allowed):
                n += 1
    return n


def verify(after, orig, base_v16, legal=None):
    """Гейт хода. Возвращает dict инвариантов + ok (bool).

    legal: пары, наложенные в ДЕТЕКТИРОВАННОЙ геометрии. Они законны
    (решение заказчика 2026-07-29: наложения, пришедшие из построения и
    проверки, имеют право там быть) и в счётчик `overlaps` не идут —
    иначе инвариант «ноль наложений» краснеет на входе, который сам их
    содержит. Считаются `_shapes.legal_pairs` по `graph.detected_bbox`.
    """
    after_b = nodes_by_id(after)
    orig_b = nodes_by_id(orig)
    v16_b = nodes_by_id(base_v16)
    after_e = {e["id"]: e for e in edges(after)}
    orig_e = {e["id"]: e for e in edges(orig)}
    v16_e = {e["id"]: e for e in edges(base_v16)}

    new_diagonals = _new_diagonals(after_e, orig_e, after_b, orig_b)
    order_broken = _order_broken(after, base_v16)
    connectivity_changed = _connectivity_changed(after, orig)
    straight_broken = _straight_broken(after_e, v16_e)
    side_changed = _side_changed(after_e, orig_e, v16_e, after_b, orig_b, v16_b)

    overlaps = _overlaps.strict_block_pairs(after, legal)

    box_after = len(triggers.detect(after)["box_on_magi"])
    box_v16 = len(triggers.detect(base_v16)["box_on_magi"])
    box_on_magistral = box_after - box_v16   # рост (>0 — брак)

    ok = (new_diagonals == 0 and order_broken == 0
          and not connectivity_changed and straight_broken == 0
          and side_changed == 0 and overlaps == 0
          and box_on_magistral <= 0)

    return {
        "new_diagonals": new_diagonals,
        "order_broken": order_broken,
        "connectivity_changed": connectivity_changed,
        "straight_broken": straight_broken,
        "side_changed": side_changed,
        "overlaps": overlaps,
        "box_on_magistral": box_on_magistral,
        "ok": ok,
    }
