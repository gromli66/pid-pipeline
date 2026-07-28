# -*- coding: utf-8 -*-
"""_axes.py — осевая модель графа: 1D-кластеризация узлов в ряды и колонки.

Источник: стенд `_scratch/layout_align/axes.py` (строки 61-150).
Перенесено без изменения логики — см. docs/planning/AUTO_LAYOUT_INTEGRATION.md, Э2.
Перенесена только модель осей; анализ выживаемости, рендеры и CLI стенда —
диагностика, в прод не идут.
"""
from __future__ import annotations

import statistics

from ._graph import edge_ends, edges, node_cxy

TOL = 2.0          # допуск кластеризации, px (обоснование — режим --calib)
MODE = "chain"     # single-linkage: разрыв > tol открывает новую ось
ORTHO_TOL = 1.0    # сегмент считается осевым, если поперечный размер <= 1 px
MAJORITY = 0.8     # ось «выжила частично», если удержалось >= 80 % её узлов


# ───────────────────────── 1D-кластеризация ─────────────────────────

def cluster_1d(items, tol, mode=MODE):
    """items: [(coord, key), ...] -> [[(coord, key), ...], ...], отсортировано.

    mode='chain'  — single-linkage: новый кластер, если разрыв с предыдущей
                    точкой > tol (ось может «протянуться» шире tol);
    mode='width'  — ширина кластера ограничена tol (leader-кластеризация).
    """
    pts = sorted(items, key=lambda t: (t[0], str(t[1])))
    out, cur = [], []
    for p in pts:
        if not cur:
            cur = [p]
            continue
        ref = cur[-1][0] if mode == "chain" else cur[0][0]
        if p[0] - ref <= tol:
            cur.append(p)
        else:
            out.append(cur)
            cur = [p]
    if cur:
        out.append(cur)
    return out


def _largest_cluster(coords, tol, mode=MODE):
    """Размер наибольшей группы соосных значений (детерминированно)."""
    if not coords:
        return 0
    cl = cluster_1d([(c, i) for i, c in enumerate(coords)], tol, mode)
    return max(len(c) for c in cl)


def _spread(coords):
    return float(statistics.pstdev(coords)) if len(coords) > 1 else 0.0


# ───────────────────────── модель осей графа ─────────────────────────

def node_coords(graph):
    """{node_id: (cx, cy)} по всем узлам с centroid."""
    return {n["id"]: node_cxy(n)
            for n in graph.get("nodes", []) if "centroid" in n}


def dup_edge_keys(graph):
    """Пары узлов, соединённые >1 ребром (артефакт распознавания)."""
    seen = {}
    for e in edges(graph):
        s, t = edge_ends(e)
        if s is None or t is None:
            continue
        k = tuple(sorted((str(s), str(t))))
        seen[k] = seen.get(k, 0) + 1
    return {k: v for k, v in seen.items() if v > 1}


def build_axes(graph, tol=TOL, mode=MODE):
    """Оси узлов: {'x': [axis...], 'y': [axis...]}.

    axis = {coord (медиана), members [node_id...], n, span [min,max] по
    перпендикулярной координате, extent}.
    """
    xy = node_coords(graph)
    res = {}
    for ax, idx in (("x", 0), ("y", 1)):
        other = 1 - idx
        axes = []
        for cl in cluster_1d([(v[idx], k) for k, v in sorted(xy.items())],
                             tol, mode):
            members = sorted(k for _c, k in cl)
            cs = [c for c, _k in cl]
            perp = sorted(xy[m][other] for m in members)
            axes.append({
                "coord": round(float(statistics.median(cs)), 3),
                "n": len(members),
                "members": members,
                "span": [round(perp[0], 1), round(perp[-1], 1)],
                "extent": round(perp[-1] - perp[0], 1),
                "width": round(cs[-1] - cs[0], 3),
            })
        res[ax] = sorted(axes, key=lambda a: a["coord"])
    return res
