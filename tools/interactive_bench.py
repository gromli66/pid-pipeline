# -*- coding: utf-8 -*-
"""interactive_bench.py — стабильность интерактива редактора (§5.2 плана
EDITOR_AFTER_LAYOUT_PLAN): протокол скриптовых возмущений Bridgeman–Tamassia.

СКЕЛЕТ (Э0). Реализована одна операция — 'reseat' (headless-проба протокола):
сдвиг узла в данных (`graph_access.move_node`) + канон посадки
`pretransform.seat_edge_endpoints`. Операции drag / autofix / optimize —
заглушки с NotImplementedError: редакторская логика
(ui/editors/advanced_graph_editor.py, autofix_chains.py, graph_geometry.py)
живёт на QGraphicsScene и без сцены не вызывается — headless-вызов появится
после рефакторинга Э1/Э3 (само по себе полезное требование, §5.2 п.3).

Протокол:
  1. корпусный uid → раскладка (тем же путём, что layout_bench) → базовый
     холст; корпус — CORPUS/_load_input из layout_bench (импорт, не копия);
  2. репрезентативные узлы (эвристики простые, по одному на роль):
     оборудование с bbox, коннектор deg2, коннектор deg3, узел на магистрали;
  3. сдвиги: малый (полшага сетки редактора; сетка — формула
     advanced_graph_editor._compute_grid_size), средний ~25px, большой ~100px;
     по осям и по диагонали;
  4. операция headless, метрики §3.3, считаемые уже сейчас:
     far_end_moved, node_disp_mean/max (кроме сдвинутого), side_changed
     (судья — `_gate._side_changed`: сторож == судья, не переизобретать).

Координаты (CODING_GUIDE §6): centroid / source_point / target_point = [y, x],
bbox = [x1, y1, x2, y2]. Сдвиги здесь задаются в (dx, dy) экрана.

Запуск (из корня репо):
    python -X utf8 tools/interactive_bench.py --uid d74eb9f1
    python -X utf8 tools/interactive_bench.py --uid d74eb9f1 --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.graph.core.layout import LayoutParams, layout, _gate    # noqa: E402
from modules.graph.core import pretransform                          # noqa: E402
from modules.graph.core.graph_access import (edge_ends, edges,       # noqa: E402
                                             is_connector, move_node,
                                             node_cxy, nodes_by_id)
from tools.layout_bench import CORPUS, _load_input                   # noqa: E402

EPS = 0.5   # px: порог «конец сдвинулся» — тот же, что у канона посадки


# ───────────────────────────── операции ─────────────────────────────
# Контракт операции: op(graph, node_id, dx, dy) — мутирует graph на месте.

def op_reseat(graph, node_id, dx, dy):
    """Headless-проба протокола: сдвиг узла в данных + канон посадки."""
    n = nodes_by_id(graph).get(node_id)
    if n is None:
        raise KeyError(node_id)
    move_node(n, dx, dy)
    pretransform.seat_edge_endpoints(graph)


def op_drag(graph, node_id, dx, dy):
    """Drag узла редакторским путём (_recalculate_edge + distribute_...)."""
    # Логика живёт в ui/editors/advanced_graph_editor.py на QGraphicsScene и
    # без сцены не вызывается — headless-вызов = требование к Э1/Э3.
    raise NotImplementedError("drag: редакторская посадка требует сцены (Э1/Э3)")


def op_autofix(graph, node_id, dx, dy):
    """Автовыравнивание (auto_fix_graph) после сдвига узла."""
    # ui/editors/autofix_chains.py работает от editor/сцены; headless-вызова
    # нет — появится после рефакторинга Э1/Э3 (либо замена на доразклад Э4).
    raise NotImplementedError("autofix: редакторская логика требует сцены (Э1/Э3)")


def op_optimize(graph, node_id, dx, dy):
    """Оптимизация рёбер (optimize_edge) после сдвига узла."""
    # ui/editors/advanced_graph_editor.py + graph_geometry.py — те же условия.
    raise NotImplementedError("optimize: редакторская логика требует сцены (Э1/Э3)")


OPS = {"reseat": op_reseat, "drag": op_drag,
       "autofix": op_autofix, "optimize": op_optimize}


# ───────────────────────── выбор узлов и сдвигов ─────────────────────────

def _degrees(graph):
    deg = {}
    for e in edges(graph):
        s, t = edge_ends(e)
        deg[s] = deg.get(s, 0) + 1
        deg[t] = deg.get(t, 0) + 1
    return deg


def pick_nodes(graph):
    """Репрезентативные узлы §5.2 (эвристики простые): {роль: node_id}."""
    byid = nodes_by_id(graph)
    deg = _degrees(graph)
    picked = {}

    # оборудование с bbox — не-коннектор с bbox, самой высокой степени
    equip = [n for n in graph.get("nodes", [])
             if not is_connector(n) and n.get("bbox") and deg.get(n["id"])]
    if equip:
        picked["equipment_bbox"] = max(equip, key=lambda n: deg[n["id"]])["id"]

    for role, d in (("connector_deg2", 2), ("connector_deg3", 3)):
        for n in graph.get("nodes", []):
            if is_connector(n) and deg.get(n["id"]) == d \
                    and n["id"] not in picked.values():
                picked[role] = n["id"]
                break

    # узел на магистрали: инцидентен ортогональному ребру длиннее _gate.MAGI
    # (порог и геометрия — как у арбитра, не переизобретаются)
    for e in edges(graph):
        if _gate._drawn_len(e) > _gate.MAGI and _gate._edge_ortho(e):
            s, t = edge_ends(e)
            for nid in (s, t):
                if nid in byid and nid not in picked.values():
                    picked["magistral"] = nid
                    break
            if "magistral" in picked:
                break
    return picked


def grid_size(graph):
    """Сетка редактора — формула advanced_graph_editor._compute_grid_size:
    max(8, медиана ширин bbox / 2); без bbox — дефолт 24."""
    ws = [n["bbox"][2] - n["bbox"][0] for n in graph.get("nodes", [])
          if n.get("bbox") and len(n["bbox"]) == 4
          and n["bbox"][2] - n["bbox"][0] > 0]
    if not ws:
        return 24
    return max(8, int(statistics.median(ws) / 2))


# ───────────────────────────── метрики §3.3 ─────────────────────────────

def measure(before, after, moved_id, eps=EPS):
    """far_end_moved, node_disp_mean/max (кроме сдвинутого), side_changed.

    before — вход операции (базовый холст), after — её выход. Скелет:
    смещения узлов сырые, без снятия глобальной трансляции (Procrustes §3.3)
    — для 'reseat' узлы, кроме сдвинутого, не двигаются by construction.
    """
    b_byid, a_byid = nodes_by_id(before), nodes_by_id(after)
    b_e = {e["id"]: e for e in edges(before)}
    a_e = {e["id"]: e for e in edges(after)}

    # far_end_moved (§3.2): конец ребра у НЕтронутого узла сдвинулся > eps.
    # Точки [y, x] — расстояние осе-симметрично.
    far = 0
    for eid, e in a_e.items():
        oe = b_e.get(eid)
        if oe is None:
            continue
        s, t = edge_ends(e)
        for nid, key in ((s, "source_point"), (t, "target_point")):
            if nid == moved_id:
                continue
            p0, p1 = oe.get(key), e.get(key)
            if p0 is None or p1 is None:
                continue
            if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) > eps:
                far += 1

    disps = []
    for nid, n in a_byid.items():
        if nid == moved_id or nid not in b_byid:
            continue
        ax, ay = node_cxy(n)
        bx, by = node_cxy(b_byid[nid])
        disps.append(math.hypot(ax - bx, ay - by))
    mean = sum(disps) / len(disps) if disps else 0.0

    # side_changed — судья _gate._side_changed (orig и v16 = вход операции).
    # Считает и концы у самого сдвинутого узла — как в гейте; уточнение
    # «своя сторона у тронутого узла легальна» — вопрос Э3, не скелета.
    side = _gate._side_changed(a_e, b_e, b_e, a_byid, b_byid, b_byid)

    return {"far_end_moved": far,
            "node_disp_mean": round(mean, 2),
            "node_disp_max": round(max(disps), 2) if disps else 0.0,
            "side_changed": side}


# ───────────────────────────── прогон ─────────────────────────────

def run_uid(uid8, op_name, as_json=False):
    g = _load_input(uid8, None)
    if g is None:
        print(f"{uid8}: ПРОПУЩЕН — входа нет", flush=True)
        return []
    base, _st = layout(g, LayoutParams())

    picked = pick_nodes(base)
    gs = grid_size(base)
    deltas = [("малый", max(1.0, gs / 2.0)), ("средний", 25.0),
              ("большой", 100.0)]
    dirs = [("+x", 1, 0), ("+y", 0, 1), ("диаг", 1, 1)]

    if not as_json:
        print(f"\n{uid8}: узлов {len(base.get('nodes', []))}, "
              f"рёбер {len(list(edges(base)))}, сетка {gs}px, "
              f"операция '{op_name}'")
        print("узлы:", ", ".join(f"{k}={v}" for k, v in picked.items())
              or "не найдены")

    rows = []
    for role, nid in picked.items():
        for dname, mag in deltas:
            for dl, ux, uy in dirs:
                work = deepcopy(base)
                try:
                    OPS[op_name](work, nid, ux * mag, uy * mag)
                except NotImplementedError as exc:
                    print(f"ОПЕРАЦИЯ НЕДОСТУПНА: {exc}", flush=True)
                    return rows
                m = measure(base, work, nid)
                m.update({"uid": uid8, "op": op_name, "role": role,
                          "node": nid, "delta": dname, "delta_px": mag,
                          "dir": dl})
                rows.append(m)
                if as_json:
                    print(json.dumps(m, ensure_ascii=False), flush=True)

    if not as_json and rows:
        print("\n| роль | узел | сдвиг | напр | дальний конец | "
              "disp узлов mean/max | сторона |")
        print("|---|---|---|---|---|---|---|")
        for m in rows:
            print(f"| {m['role']} | {m['node']} | {m['delta']} "
                  f"({m['delta_px']:g}px) | {m['dir']} | {m['far_end_moved']} | "
                  f"{m['node_disp_mean']}/{m['node_disp_max']} | "
                  f"{m['side_changed']} |")
        print(f"\nИТОГО {uid8}: прогонов {len(rows)}, "
              f"far_end_moved суммарно {sum(m['far_end_moved'] for m in rows)}, "
              f"max disp узлов {max(m['node_disp_max'] for m in rows)}, "
              f"смен стороны {sum(m['side_changed'] for m in rows)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", action="append", default=None,
                    help="uid8 корпуса (по умолчанию d74eb9f1)")
    ap.add_argument("--op", choices=sorted(OPS), default="reseat")
    ap.add_argument("--json", action="store_true", help="сырой JSON по строке")
    args = ap.parse_args()

    uids = args.uid or ["d74eb9f1"]
    for uid8 in uids:
        if uid8 not in CORPUS:
            print(f"{uid8}: не в CORPUS ({', '.join(CORPUS)})", flush=True)
            continue
        run_uid(uid8, args.op, as_json=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
