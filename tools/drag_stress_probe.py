# -*- coding: utf-8 -*-
"""drag_stress_probe.py — приёмка Этапа B: стресс-drag и зигзаг-тест.

Два режима (оба headless, offscreen Qt, файлы корпуса НЕ мутируются —
каждый жест откатывается undo с проверкой побайтового восстановления):

1. Стресс «все узлы ±STEP px» (по умолчанию STEP=2):
     python -X utf8 tools/drag_stress_probe.py graph_edited971.json
   Каждый узел таскается жестом start/drag/end на ±STEP по каждой оси.
   Метрика приёмки — «труба на бокс МОЛЧА»: новые пары (ребро, узел)
   классов through/along судьи edit_checks ПОСЛЕ жеста, у чьего ребра НЕТ
   пометки _route_defect (несведённый маршрут виден оператору — это не
   «молча»). База сравнения — вход файла (жесты откатываются).

2. Покадровый зигзаг (именной кейс node_96/971, шаги 5px):
     python -X utf8 tools/drag_stress_probe.py graph_edited971.json --zigzag node_96
   На каждом кадре протяжки: (а) полные пути инцидентных рёбер строго
   ортогональны (max_dev == 0), (б) авто-колени едут за узлом (waypoints
   меняются между кадрами), (в) предпросмотр == итог (снимок последнего
   кадра байт-в-байт равен состоянию после отпускания).

Рычаг A/B: --avoid off ставит PID_EDIT_AVOID=0 — жесты идут по самописной
лестнице (база «сейчас 3-4 молча»); --avoid on (дефолт) — оконная
libavoid-сессия Этапа B (приёмка: молча == 0).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from modules.graph.core import edit_checks as ec  # noqa: E402


def _judge_pairs(graph: dict) -> set:
    """Пары (ребро, узел, класс) судьи: through + along."""
    thru = {(i["edge"], i["node"], "thru") for i in ec.through_box(graph)}
    along = {(i["edge"], i["node"], "along") for i in ec.along_border(graph)}
    return thru | along


def _edge_proj(e: dict) -> tuple:
    """Проекция ребра для сверки undo (геометрия + видимые флаги)."""
    return (e.get("source"), e.get("target"),
            tuple(e.get("source_point") or []),
            tuple(e.get("target_point") or []),
            tuple(tuple(w) for w in e.get("waypoints") or []),
            bool(e.get("_auto_route")))


def _graph_proj(graph: dict) -> tuple:
    nodes = tuple(
        (n.get("id"), tuple(n.get("centroid") or []),
         tuple(n.get("bbox") or []),
         tuple(n.get("segmentation") or []))
        for n in graph.get("nodes", []))
    links = tuple(_edge_proj(e) for e in graph.get("links", []))
    return nodes, links


def _make_editor(graph_file: Path):
    """Headless-редактор на КОПИИ файла корпуса (canvas-режим)."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage, QColor
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    QApplication.instance() or QApplication([])
    graph = json.loads(graph_file.read_text(encoding="utf-8"))
    h, w = (graph.get("graph", {}).get("image_size") or [1080, 1920])[:2]
    tmp = Path(tempfile.mkdtemp(prefix="drag_stress_"))
    img = QImage(int(w), int(h), QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    img.save(str(tmp / "raster.png"))
    gp = tmp / graph_file.name
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = AdvancedGraphEditor()
    ed._canvas_mode = True   # как вкладка: флаг ДО load_data
    assert ed.load_data(str(tmp / "raster.png"), str(gp)), "load_data провалился"
    return ed


def _flags_snapshot(links: list) -> list:
    return [(bool(e.get("_route_defect")), bool(e.get("_auto_route")))
            for e in links]


def _restore_flags(links: list, snap: list) -> None:
    """_route_defect не входит в undo-снапшот drag (не в sha) — вернуть
    руками, чтобы жесты не пятнали друг друга в стрессе."""
    for e, (defect, _auto) in zip(links, snap):
        if defect:
            e["_route_defect"] = True
        else:
            e.pop("_route_defect", None)


def run_stress(ed, step: float) -> dict:
    """Стресс: у «молча»-находки два класса с РАЗНЫМ статусом.

    * «труба на бокс» — ребро, которое ЖЕСТ ВЁЛ САМ (routable-инцидентное
      таскаемому узлу: правило _drag_routable_edges), после жеста лежит
      на/вдоль узла без пометки. Приёмка Этапа B: таких 0.
    * «бокс на трубу» — таскаемый узел наехал на ЧУЖУЮ неподвижную трубу
      (или свою ручную). По контракту заказчика 2026-08-01 (третья
      итерация) это ЛЕГАЛЬНОЕ честное наложение: drag чужие рёбра не
      трогает, лечат Э4/оператор. Печатается справочно.
    """
    graph = ed.model.graph_data
    base_pairs = _judge_pairs(graph)
    base_proj = _graph_proj(graph)
    silent_pipe: dict = {}
    silent_box: dict = {}
    marked = 0
    gestures = 0
    t_total = 0.0
    node_ids = [n["id"] for n in graph["nodes"]]
    for nid in node_ids:
        node = ed.nodes.get(nid)
        if node is None or not node.get("centroid"):
            continue
        cy, cx = node["centroid"]                    # centroid = [y, x]!
        for dx, dy in ((step, 0.0), (-step, 0.0), (0.0, step), (0.0, -step)):
            flags = _flags_snapshot(graph["links"])
            # рёбра, которые жест поведёт сам (правило редактора
            # _drag_routable_edges, start_drag_node)
            live_ids = {
                e.get("id") for e in graph["links"]
                if nid in (e.get("source"), e.get("target"))
                and not e.get("_manual_route")
                and (not e.get("waypoints") or e.get("_auto_route"))}
            t0 = time.perf_counter()
            ed.start_drag_node(nid)
            ed.drag_node_to(cx + dx, cy + dy)        # drag ждёт (x, y)
            ed.end_drag_node()
            t_total += time.perf_counter() - t0
            gestures += 1
            cur = _judge_pairs(graph)
            for edge_id, node_id_hit, kind in cur - base_pairs:
                # амнистия «не хуже входа» — ТО ЖЕ правило, что у
                # редактора (_amnesty_ids, раздельная): сквозь-на-входе
                # амнистирует и сквозь, и вдоль по этому узлу; вдоль-на-
                # входе НЕ разрешает сквозь-выход (класс хуже).
                if (edge_id, node_id_hit, "thru") in base_pairs:
                    continue
                e = next((l for l in graph["links"]
                          if l.get("id") == edge_id), None)
                if e is not None and e.get("_route_defect"):
                    marked += 1
                    continue
                bucket = silent_pipe if edge_id in live_ids else silent_box
                bucket.setdefault((edge_id, node_id_hit, kind), []).append(
                    f"{nid}{dx:+g}{dy:+g}")
            ed.undo_mgr.undo()
            _restore_flags(graph["links"], flags)
            if _graph_proj(graph) != base_proj:
                raise SystemExit(
                    f"undo НЕ восстановил граф после жеста {nid} "
                    f"({dx:+g},{dy:+g}) — стресс недействителен")
    return {"gestures": gestures, "silent": silent_pipe,
            "silent_box": silent_box, "marked": marked,
            "base_pairs": len(base_pairs),
            "ms_per_gesture": 1000.0 * t_total / max(gestures, 1)}


def run_zigzag(ed, nid: str, step: float = 5.0, frames: int = 6) -> dict:
    graph = ed.model.graph_data
    node = ed.nodes.get(nid)
    assert node is not None, f"узла {nid} нет в графе"
    cy, cx = node["centroid"]
    incident = [e for e in graph["links"]
                if nid in (e.get("source"), e.get("target"))]

    def dev() -> float:
        worst = 0.0
        for e in incident:
            sp, tp = e.get("source_point"), e.get("target_point")
            if not sp or not tp:
                continue
            pts = [sp] + list(e.get("waypoints") or []) + [tp]
            for a, b in zip(pts, pts[1:]):
                worst = max(worst, min(abs(a[0] - b[0]), abs(a[1] - b[1])))
        return worst

    def wps_snap() -> list:
        return [copy.deepcopy(e.get("waypoints") or []) for e in incident]

    max_dev = 0.0
    knees_moved = 0
    knees_frames = 0
    ed.start_drag_node(nid)
    # туда step*frames, обратно до старта — покадрово
    xs = [cx + step * k for k in range(1, frames + 1)] \
        + [cx + step * k for k in range(frames - 1, -1, -1)]
    prev_wps = wps_snap()
    for x in xs:
        ed.drag_node_to(x, cy)
        max_dev = max(max_dev, dev())
        cur_wps = wps_snap()
        had_auto = any(e.get("_auto_route") and (e.get("waypoints") or [])
                       for e in incident)
        if had_auto:
            knees_frames += 1
            if cur_wps != prev_wps:
                knees_moved += 1
        prev_wps = cur_wps
    last_frame = _graph_proj(graph)
    ed.end_drag_node()
    preview_honest = (_graph_proj(graph) == last_frame)
    ed.undo_mgr.undo()
    return {"max_dev": max_dev, "knees_frames": knees_frames,
            "knees_moved": knees_moved, "preview_honest": preview_honest}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="файлы корпуса graph_edited*.json")
    ap.add_argument("--step", type=float, default=2.0)
    ap.add_argument("--zigzag", metavar="NODE_ID",
                    help="вместо стресса — покадровый зигзаг узла")
    ap.add_argument("--avoid", choices=("on", "off"), default="on",
                    help="off = PID_EDIT_AVOID=0 (лестница, база сравнения)")
    args = ap.parse_args()
    os.environ["PID_EDIT_AVOID"] = "1" if args.avoid == "on" else "0"

    fail = False
    for name in args.files:
        p = Path(name)
        if not p.is_absolute():
            p = REPO / p
        ed = _make_editor(p)
        print(f"\n=== {p.name} (avoid={args.avoid}) ===")
        if args.zigzag:
            r = run_zigzag(ed, args.zigzag, step=5.0)
            print(f"зигзаг {args.zigzag}: max_dev={r['max_dev']:.2f}  "
                  f"колени едут {r['knees_moved']}/{r['knees_frames']} кадров  "
                  f"предпросмотр==итог: {r['preview_honest']}")
            if r["max_dev"] > 0.0 or not r["preview_honest"] \
                    or (r["knees_frames"] and not r["knees_moved"]):
                fail = True
        else:
            r = run_stress(ed, args.step)
            print(f"жестов: {r['gestures']}  базовых пар судьи: "
                  f"{r['base_pairs']}  кадр+жест: {r['ms_per_gesture']:.0f}мс")
            print(f"помечено _route_defect (видно оператору): {r['marked']}")
            print(f"ТРУБА на бокс МОЛЧА (приёмка B, ведёт жест): "
                  f"{len(r['silent'])}")
            for (edge, node_hit, kind), gestures in sorted(r["silent"].items()):
                shown = ", ".join(gestures[:4])
                more = f" (+{len(gestures) - 4})" if len(gestures) > 4 else ""
                print(f"  {edge} x {node_hit} [{kind}]: {shown}{more}")
            print(f"бокс на чужую трубу (легально по контракту 2026-08-01): "
                  f"{len(r['silent_box'])}")
            for (edge, node_hit, kind), gestures in sorted(
                    r["silent_box"].items()):
                shown = ", ".join(gestures[:4])
                more = f" (+{len(gestures) - 4})" if len(gestures) > 4 else ""
                print(f"  [спр] {edge} x {node_hit} [{kind}]: {shown}{more}")
            if r["silent"]:
                fail = True
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
