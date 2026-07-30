# -*- coding: utf-8 -*-
"""residual.py — остаточные очаги после раскладки для адресной дочистки (Э12).

Философия «оператор дочистит» работает, только если оператор ВИДИТ, что
дочищать (§7.1 EDITOR_AFTER_LAYOUT_PLAN): 7 очагов на 937 узлов глазами не
найти. Детекторы приёмки уже считают всё нужное — модуль лишь собирает их
выводы в один список с координатами. Воркер пишет его рядом с холстом
(`residual_defects.json`), вкладка «Ручная правка» подсвечивает маркерами.

Три сорта остатка — по триажу §7.1 плана:
  * невидимые трубы (зазор форм < floor) — структурный пол, путь закрытия
    оператор;
  * боксы на чужих нарисованных трубах — транзиты, до Э7 дочищаются руками;
  * нелегальные наложения блоков — судья строгий (Э13, tol=0): наложения из
    детекции ЛЕГАЛЬНЫ и в остаток не входят.

Чистый модуль без Celery/БД — тестируется без воркера (паттерн layout_policy).
"""
from __future__ import annotations

from . import _overlaps, _shapes, spread
from .params import LayoutParams
from ..graph_access import edge_ends, edges, node_cxy, nodes_by_id


def _cxy(byid, nid):
    n = byid.get(nid)
    return list(node_cxy(n)) if n is not None else None


def _mid(a, b):
    if a and b:
        return [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]
    return a or b


def collect_residual(aft, orig, params=None):
    """Очаги на финальном графе. -> dict для `residual_defects.json`.

    aft — граф после раскладки; orig — снимок её входа (`stages["orig"]`),
    по нему считается легальность наложений (детекция + segmentation ещё на
    своих местах). Координаты очагов — [x, y] холста.
    """
    p = params or LayoutParams()
    byid = nodes_by_id(aft)

    invisible = []
    edge_by_id = {str(e.get("id")): e for e in edges(aft)}
    for eid, gap in sorted(spread.defects(aft, byid, p.floor).items()):
        e = edge_by_id.get(eid)
        if e is None:
            continue
        s, t = edge_ends(e)
        sp, tp = e.get("source_point"), e.get("target_point")
        if sp and tp:
            point = [(sp[1] + tp[1]) / 2.0, (sp[0] + tp[0]) / 2.0]  # [y,x]→[x,y]
        else:
            point = _mid(_cxy(byid, s), _cxy(byid, t))
        invisible.append({"edge_id": eid, "nodes": [s, t],
                          "gap": round(float(gap), 2), "point": point})

    box_on_magi = [{"node_id": nid, "point": _cxy(byid, nid)}
                   for nid in sorted(spread.box_on_magi_drawn(aft, byid))]

    det = (orig.get("graph") or {}).get("detected_bbox")
    legal = _shapes.legal_pairs(orig, det, 0.0)
    items, _degenerate = _overlaps.collect_items(aft)
    pos, _touch = _overlaps.strict_pairs(items)
    overlaps = []
    for r in pos:
        if r["kind"] != "block-block":
            continue
        a, b = sorted((r["a"], r["b"]))
        if (a, b) in legal:
            continue
        overlaps.append({"nodes": [a, b], "area_px2": r["area_px2"],
                         "point": _mid(_cxy(byid, a), _cxy(byid, b))})
    overlaps.sort(key=lambda r: tuple(r["nodes"]))

    return {
        "floor": p.floor,
        "invisible_edges": invisible,
        "box_on_magi": box_on_magi,
        "overlaps": overlaps,
        "total": len(invisible) + len(box_on_magi) + len(overlaps),
    }
