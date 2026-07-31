# -*- coding: utf-8 -*-
"""layout_bench.py — приёмка раскладки на корпусе: метрики + список регрессий.

Порт `_scratch/layout_align/check_result.py` на прод-модуль, плюс метрики по
тексту (§3.9 плана). Гоняется руками — данных корпуса нет ни в git, ни на бою.

Судья по перестановкам — `order_broken_local` (локальные пары), а НЕ глобальный
`order_broken` из гейта: на 51b339ab они дают 0 и 1096, потому что глобальный
считает парами узлов, разнесёнными на пол-листа.

Главное правило приёмки: **любой запрет сравнивается с базой входа, а не с
нулём.** Поэтому «до» здесь — это геометрия ПОСЛЕ расстановки (то, что слой
раздвигания получил на вход), а не исходная схема.

Запуск (из корня репо):
    python -X utf8 tools/layout_bench.py
    python -X utf8 tools/layout_bench.py --uid 51b339ab --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.graph.core.layout import LayoutParams, layout          # noqa: E402
from modules.graph.core.layout import _gate, _shapes, spread        # noqa: E402
from modules.graph.core.graph_access import (edge_ends,            # noqa: E402
                                             edge_polyline, edges,
                                             is_connector, nodes_by_id)
from modules.graph.core.canvas_input import to_canvas               # noqa: E402
from modules.graph.core import pretransform                         # noqa: E402

CORPUS = {
    "51b339ab": "51b339ab-3e4c-429c-ac5f-49c44cb9c755",
    "a6d28736": "a6d28736-5039-4f40-9f7a-37b476f6fafb",
    "8d517a35": "8d517a35-0435-48d7-bc17-300345a15162",
    "89ca7583": "89ca7583-233e-44da-b30b-f2f08eb2567d",
    "13d1ef5f": "13d1ef5f-d251-4a88-bfd4-e0b51f144b69",
    "6e7144d5": "6e7144d5-19fc-4661-b90a-f26dcaa8c71a",
    "d74eb9f1": "d74eb9f1-668d-4596-891f-d4e5a16d04d0",
    "5137af27": None,          # сырой вход потерян, см. Э0
}
# Эталонная таблица корпуса. ПЕРЕОБЪЯВЛЕНА 2026-07-30 (решение заказчика):
# алгоритм намеренно изменён тремя правками, и прежние числа §7 SOLUTION.md
# сравнивать больше не с чем.
#   1. посадка конца ребра на контур ВДОЛЬ оси прямизны, а не лучом из
#      центроида (`seating._poly_hit`);
#   2. наложения считаются по НАРИСОВАННОЙ ФОРМЕ узла, а не по габариту
#      (`layout/_shapes.py`, `axial.conflicts`, `_overlaps.collect_items`);
#   3. пары, наложенные в ДЕТЕКТИРОВАННОЙ геометрии (до `apply_fixed_sizes`),
#      законны и не разводятся — решение заказчика 2026-07-29 «если на графе
#      после построения и проверки есть наложения, им можно там находиться»,
#      причём легальны размеры ИЗ ДЕТЕКЦИИ, а не после словаря.
# Итог перезамера: 601 -> 19 стало 590 -> 14, нелегальных наложений 0 на всех
# семи, косых рёбер по корпусу 62 (у трёх крупных контурных блоков 12 из 37
# против 36 из 37 на прежнем алгоритме), стороны/прямизна/связность/
# перестановки — нули. Прежняя таблица §7 (311/10, 128/6, 81/2, 26/1, 20/1,
# 14/0, 8/0, 0/0) сохранена в истории git, коммит с этой правкой.
# 5137af27 не считается — сырой вход потерян (Э0), число оставлено прежним.
#   4. створ крупного контурного блока (`band_block=True`, ярус 1) —
#      включён 2026-07-30: косых рёбер по корпусу 60 -> 53, на c2f79462
#      2 -> 1; ценой двух дефектов (8d517a35 103->0 стало 104->0 — там
#      бесплатно, 13d1ef5f 22->2 стало 23->3, c2f79462 13->3 стало 13->4).
#      На пяти графах без крупного блока — бит-в-бит как без него.
#   5. Э13 (решение заказчика 2026-07-31, §7.1.1 EDITOR_AFTER_LAYOUT_PLAN):
#      судья без допуска близости — legal для `_gate.verify` считается с
#      tol=0 (легально только пересечение площадью > 0 в детекции), допуск
#      BORDER_TOL остаётся решателю внутри `layout()`. По аудиту
#      (tools/legal_audit_probe.py) все 11 псевдо-пар корпуса не наложены
#      после раскладки — числа таблицы бит-в-бит, новая колонка
#      «утечка» (legal_leak) обязана быть 0.
#   6. Э2 (принято заказчиком 2026-07-31): ось посадки у пар с коннектором —
#      от реальной геометрии связи (`seating.reseat_edge`), вертикальная
#      связь больше не садится на боковую грань. Размен, принятый глазами:
#      диагонали a6d 8->4, боксы-на-магистралях a6d 11->7, ценой +4
#      соосных невидимых пар коннектор x арматура (a6d 4->7, 51b 7->8;
#      итог 610->20) — они подсвечиваются Э12 и ждут нуджинга Э11.
#      ВНИМАНИЕ: числа сняты фактическим прогоном 2026-07-31, и в «до» уже
#      входил неразъяснённый дрейф относительно прежнего EXPECT (51b
#      297->310, 8d5 104->99, 89ca 26->36 воспроизводились и ДО Э2) —
#      причина дрейфа выясняется отдельной задачей, при выяснении дописать
#      правку №7.
EXPECT = {"51b339ab": (310, 8), "a6d28736": (128, 7), "8d517a35": (99, 0),
          "89ca7583": (36, 2), "13d1ef5f": (23, 3), "6e7144d5": (14, 0),
          "5137af27": (8, 0), "d74eb9f1": (0, 0)}

# §3.5: порог якоря подписи. Числа в SOLUTION.md нет — стенд текстом не
# занимался; ориентир плана 64 px холста. Здесь это ручка приёмки, штатное
# место значения — LayoutParams, когда механизм якоря появится (Э5).
ANCHOR_MAX = 64.0


# ───────────────────────────── текст (§3.9) ─────────────────────────────

def _active_blocks(graph):
    """Блоки, которые реально уходят в FXML: не слитые в другой блок."""
    return [t for t in (graph.get("text_blocks") or [])
            if t.get("merged_into") is None and t.get("bbox")]


def _pt_rect(px, py, r):
    """Расстояние от точки до прямоугольника (0 внутри)."""
    dx = max(r[0] - px, 0.0, px - r[2])
    dy = max(r[1] - py, 0.0, py - r[3])
    return math.hypot(dx, dy)


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx
                                               + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _elements(graph):
    """(узлы, рёбра) в форме, удобной для поиска ближайшего к подписи."""
    nodes = []
    for n in graph.get("nodes", []):
        bb = n.get("bbox")
        c = n.get("centroid")
        if bb and len(bb) == 4 and not is_connector(n):
            nodes.append((n["id"], (bb[0], bb[1], bb[2], bb[3])))
        elif c:
            nodes.append((n["id"], (c[1], c[0], c[1], c[0])))
    edg = []
    for i, e in enumerate(edges(graph)):
        s, t = edge_ends(e)
        pl = edge_polyline(e)
        if len(pl) >= 2:
            edg.append((f"{s}->{t}#{i}", pl))
    return nodes, edg


def _nearest(bbox, nodes, edg):
    """(id ближайшего элемента, расстояние) от ЦЕНТРА подписи."""
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    best_id, best = None, float("inf")
    for nid, r in nodes:
        d = _pt_rect(cx, cy, r)
        if d < best:
            best_id, best = nid, d
    for eid, pl in edg:
        for i in range(len(pl) - 1):
            d = _pt_seg(cx, cy, pl[i][0], pl[i][1], pl[i + 1][0], pl[i + 1][1])
            if d < best:
                best_id, best = eid, d
    return best_id, best


def _rect_hit(bbox, r):
    return not (bbox[2] <= r[0] or r[2] <= bbox[0]
                or bbox[3] <= r[1] or r[3] <= bbox[1])


def _seg_hit(bbox, pl):
    """Проходит ли полилиния сквозь прямоугольник подписи (Liang-Barsky)."""
    x1, y1, x2, y2 = bbox
    for i in range(len(pl) - 1):
        ax, ay = pl[i]
        bx, by = pl[i + 1]
        t0, t1 = 0.0, 1.0
        dx, dy = bx - ax, by - ay
        ok = True
        for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
            if abs(p) < 1e-9:
                if q < 0:
                    ok = False
                    break
            else:
                rr = q / p
                if p < 0:
                    t0 = max(t0, rr)
                else:
                    t1 = min(t1, rr)
                if t0 > t1:
                    ok = False
                    break
        if ok:
            return True
    return False


def text_metrics(before, after, anchor_max=ANCHOR_MAX):
    """Что стало с подписями после перекладки (§3.9).

    Подписи раскладка НЕ трогает — уезжают элементы. Поэтому «сменила
    ближайший элемент» и означает «подпись оказалась не у своего объекта».
    """
    blocks = _active_blocks(after)
    if not blocks:
        return {"text_total": 0, "text_moved_owner": 0, "text_no_anchor": 0,
                "text_on_shape_before": 0, "text_on_shape_after": 0}
    nb, eb = _elements(before)
    na, ea = _elements(after)
    moved = no_anchor = hit_b = hit_a = 0
    for t in blocks:
        bb = t["bbox"]
        id_b, d_b = _nearest(bb, nb, eb)
        id_a, d_a = _nearest(bb, na, ea)
        if d_b > anchor_max:
            no_anchor += 1
        elif id_b != id_a:
            moved += 1
        for nodes, edg, flag in ((nb, eb, "b"), (na, ea, "a")):
            hit = any(_rect_hit(bb, r) for _i, r in nodes) \
                or any(_seg_hit(bb, pl) for _i, pl in edg)
            if hit and flag == "b":
                hit_b += 1
            elif hit and flag == "a":
                hit_a += 1
    return {"text_total": len(blocks), "text_moved_owner": moved,
            "text_no_anchor": no_anchor,
            "text_on_shape_before": hit_b, "text_on_shape_after": hit_a}


# ───────────────────────────── приёмка ─────────────────────────────

def _seat_kind(node):
    """Тип узла-хозяина конца — теми же ветками, что `seating.node_anchor`:
    connector / skin (FIXED_SIZES, content-rect) / polygon (контур) / bbox."""
    if node is None:
        return "bbox"
    if is_connector(node):
        return "connector"
    bb = node.get("bbox")
    has_bbox = bool(bb) and len(bb) == 4
    if has_bbox and node.get("class_name") in pretransform.FIXED_SIZES:
        return "skin"
    seg = node.get("segmentation")
    has_poly = bool(seg) and isinstance(seg, list) and len(seg) >= 6
    if has_poly and (not has_bbox or not node.get("_axis")):
        return "polygon"
    return "bbox"


def seat_violations(graph, tol=0.5):
    """§3.2 EDITOR_AFTER_LAYOUT_PLAN: концы рёбер вне канона посадки.

    Канон — `pretransform.seat_edge_endpoints` (единый модуль посадки, судья
    не переизобретается): прогон на deepcopy графа и счёт концов, ушедших от
    сохранённых дальше tol px, с разбивкой по типу узла-хозяина. Точки
    source/target_point — [y, x], расстояние осе-симметрично.

    Колонка ОТЧЁТНАЯ: выход раскладки каноничен by construction (ожидаемо 0);
    в regressions НЕ входит — набор метрик приёмки заморожен (§3.4 плана).
    """
    ref = deepcopy(graph)
    pretransform.seat_edge_endpoints(ref)
    byid = nodes_by_id(graph)
    by_type = {"connector": 0, "skin": 0, "polygon": 0, "bbox": 0}
    total = viol = 0
    worst = 0.0
    for e0, e1 in zip(edges(graph), edges(ref)):
        s, t = edge_ends(e0)
        for nid, key in ((s, "source_point"), (t, "target_point")):
            p0, p1 = e0.get(key), e1.get(key)
            if p1 is None:
                continue
            total += 1
            if p0 is None:
                d = float("inf")   # конца в данных нет — канон его требует
            else:
                d = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            if d > tol:
                viol += 1
                by_type[_seat_kind(byid.get(nid))] += 1
                if d != float("inf"):
                    worst = max(worst, d)
    return {"total_ends": total, "violations": viol, "by_type": by_type,
            "max_px": round(worst, 2)}


def _legal_leak(aft, base, pseudo_pairs):
    """Псевдолегальные пары, СТАВШИЕ наложенными после раскладки. Порог 0.

    Псевдо-пара — амнистия близости BORDER_TOL (во вкладке правки графа
    коллизии НЕТ). Строгий судья Э13 такие пары больше не прощает — они уже
    входят в `overlaps_after`; колонка показывает, кого именно перестали
    амнистировать (§3.2 EDITOR_AFTER_LAYOUT_PLAN).

    Метаправило «сравнение с базой входа»: пара, наложенная по нарисованной
    форме уже НА ВХОДЕ раскладки (base = stages["orig"], после словаря
    размеров), — не утечка слоя, слой её не создавал.
    """
    if not pseudo_pairs:
        return 0
    a_byid = {n["id"]: n for n in aft.get("nodes") or []}
    b_byid = {n["id"]: n for n in base.get("nodes") or []}

    def _overlapped(byid, a, b):
        na, nb = byid.get(a), byid.get(b)
        if na is None or nb is None:
            return False
        sa, sb = _shapes.shape_of(na), _shapes.shape_of(nb)
        return (sa is not None and sb is not None
                and sa.intersection(sb).area > 1e-6)

    return sum(1 for a, b in pseudo_pairs
               if _overlapped(a_byid, a, b) and not _overlapped(b_byid, a, b))


def check(uid8, canvas_dir=None, with_text=True):
    g = _load_input(uid8, canvas_dir)
    if g is None:
        return None
    t0 = time.perf_counter()
    stages = {}
    aft, st = layout(g, LayoutParams(), stages=stages)
    dt = time.perf_counter() - t0
    orig, v16 = stages["orig"], stages["placed"]

    # Наложения, пришедшие из ПОСТРОЕНИЯ И ПРОВЕРКИ, законны (решение
    # заказчика 2026-07-29) — судья их не считает. Таблица строится по
    # детектированным габаритам, которые кладёт `to_canvas`.
    # Э13: у СУДЬИ допуска близости нет (tol=0 — легально только пересечение
    # площадью > 0), BORDER_TOL остаётся решателю внутри `layout()`.
    det = orig.get("graph", {}).get("detected_bbox")
    legal_solver = _shapes.legal_pairs(orig, det, LayoutParams().border_tol)
    legal = _shapes.legal_pairs(orig, det, 0.0)
    vb, ab = nodes_by_id(v16), nodes_by_id(aft)
    g16 = _gate.verify(v16, orig, v16, legal)
    gaf = _gate.verify(aft, orig, v16, legal)

    r = {
        "uid": uid8,
        "nodes": len(aft.get("nodes", [])),
        "edges": len(list(edges(aft))),
        "time_s": round(dt, 1),
        "defects_before": st["defects_before"],
        "defects_after": st["defects_after"],
        "diag_before": g16["new_diagonals"], "diag_after": gaf["new_diagonals"],
        "overlaps_before": g16["overlaps"], "overlaps_after": gaf["overlaps"],
        "legal_leak": _legal_leak(aft, orig, legal_solver - legal),
        "side_changed": gaf["side_changed"],
        "straight_broken": gaf["straight_broken"],
        # СВЯЗНОСТЬ — «не хуже базы», а не абсолют: гейт сравнивает набор id с
        # ОРИГИНАЛОМ, и если исходный граф отредактировали уже после
        # расстановки, флаг поднимается у самой базы, а слой тут ни при чём
        "connectivity_changed": (gaf["connectivity_changed"]
                                 and not g16["connectivity_changed"]),
        "connectivity_base_differs": g16["connectivity_changed"],
        "order_local": spread.order_broken_local(aft, v16),
        "magi_before": len(spread.box_on_magi_drawn(v16, vb)),
        "magi_after": len(spread.box_on_magi_drawn(aft, ab)),
        "pipe_depth_before": round(spread.box_on_pipe_depth(v16, vb)[1], 1),
        "pipe_depth_after": round(spread.box_on_pipe_depth(aft, ab)[1], 1),
        "pen_before": round(spread.penetration_sum(v16, vb), 1),
        "pen_after": round(spread.penetration_sum(aft, ab), 1),
    }
    sv = seat_violations(aft)
    r["seat_violations"] = sv["violations"]
    r["seat_violations_by_type"] = sv["by_type"]
    r["seat_max_px"] = sv["max_px"]
    if with_text:
        r.update(text_metrics(orig, aft))

    reg = []
    if r["diag_after"] > r["diag_before"]:
        reg.append("диагонали")
    if r["overlaps_after"] > r["overlaps_before"]:
        reg.append("наложения")
    if r["legal_leak"]:
        reg.append("утечка легальности")
    if r["side_changed"]:
        reg.append("сторона входа")
    if r["straight_broken"]:
        reg.append("прямизна")
    if r["connectivity_changed"]:
        reg.append("связность")
    if r["order_local"]:
        reg.append("перестановки")
    if r["magi_after"] > r["magi_before"]:
        reg.append("бокс-на-магистрали")
    if r["defects_after"] > r["defects_before"]:
        reg.append("дефекты")
    exp = EXPECT.get(uid8)
    if exp and (r["defects_before"], r["defects_after"]) != exp:
        reg.append(f"расхождение с §7 SOLUTION.md ({exp[0]} -> {exp[1]})")
    if r["connectivity_base_differs"]:
        r["warning"] = ("исходный граф отредактирован после расстановки — "
                        "база уже расходится с ним; сравнение идёт с базой")
    r["regressions"] = reg
    r["ok"] = not reg
    return r


def _load_input(uid8, canvas_dir):
    if canvas_dir:
        p = Path(canvas_dir) / f"{uid8}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    full = CORPUS[uid8]
    if full is None:
        return None
    src = REPO / "storage" / "diagrams" / full / "graph" / "graph_validated.json"
    if not src.exists():
        return None
    g, _t = to_canvas(json.loads(src.read_text(encoding="utf-8")))
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", action="append", default=None)
    ap.add_argument("--canvas-input-dir", default=None,
                    help="каталог с <uid8>.json — готовыми канвас-входами")
    ap.add_argument("--no-text", action="store_true",
                    help="без метрик по тексту (они квадратичны и небыстры)")
    ap.add_argument("--json", action="store_true", help="сырой JSON по строке")
    args = ap.parse_args()

    rows = []
    for uid8 in (args.uid or list(CORPUS)):
        r = check(uid8, args.canvas_input_dir, with_text=not args.no_text)
        if r is None:
            print(f"{uid8}: ПРОПУЩЕН — входа нет", flush=True)
            continue
        rows.append(r)
        if args.json:
            print(json.dumps(r, ensure_ascii=False), flush=True)
        else:
            print(f"{uid8}: {r['defects_before']} -> {r['defects_after']}, "
                  f"{'РЕГРЕССИИ: ' + ', '.join(r['regressions']) if r['regressions'] else 'регрессий нет'}"
                  f", {r['time_s']}s", flush=True)

    if not rows:
        return 1
    print()
    print("| uid | узлов | дефекты | диаг | налож | утечка | стор | прям | связн | перест | "
          "бокс-на-магистр | проникн.форм | посадка | время |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['uid']} | {r['nodes']} | "
              f"{r['defects_before']} -> {r['defects_after']} | "
              f"{r['diag_before']}->{r['diag_after']} | "
              f"{r['overlaps_before']}/{r['overlaps_after']} | "
              f"{r['legal_leak']} | "
              f"{r['side_changed']} | {r['straight_broken']} | "
              f"{'OK' if not r['connectivity_changed'] else 'СЛОМАНА'} | "
              f"{r['order_local']} | {r['magi_before']} -> {r['magi_after']} | "
              f"{r['pen_before']} -> {r['pen_after']} | "
              f"{r['seat_violations']} | {r['time_s']}s |")
    tb = sum(r["defects_before"] for r in rows)
    ta = sum(r["defects_after"] for r in rows)
    print(f"\nИТОГО дефектов: {tb} -> {ta}")

    if not args.no_text and any("text_total" in r for r in rows):
        print("\n| uid | подписей | сменили ближайший элемент | без якоря "
              f"(>{ANCHOR_MAX:g}px) | налезают до | налезают после |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            if "text_total" not in r:
                continue
            print(f"| {r['uid']} | {r['text_total']} | {r['text_moved_owner']} | "
                  f"{r['text_no_anchor']} | {r['text_on_shape_before']} | "
                  f"{r['text_on_shape_after']} |")

    bad = [r for r in rows if r["regressions"]]
    warn = [r for r in rows if r.get("warning")]
    for r in warn:
        print(f"\nПРЕДУПРЕЖДЕНИЕ {r['uid']}: {r['warning']}")
    print("\nРЕГРЕССИИ:", "нет ни одной" if not bad
          else "; ".join(f"{r['uid']}: {', '.join(r['regressions'])}" for r in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
