# -*- coding: utf-8 -*-
"""edit_render_probe.py — HTML-подсветка находок судьи edit_checks (Э0).

Для каждого холста строит страницу: сводка счётчиков, карта листа с
подсветкой дефектов, зумы на очаги. Цвета:
    красный  — концы в углах рамки посадки;
    оранжевый — диагональные сегменты (пунктир — почти-оси);
    жёлтый   — пробег вдоль границы ближе клиренса;
    фиолетовый — ребро сквозь нутро чужого узла (+ контур узла).

Запуск: python -X utf8 tools/edit_render_probe.py --all --outdir C:/tmp
        python -X utf8 tools/edit_render_probe.py tools/bench/edit_corpus/graph_edited_star.json
Ничего не пишет в storage; выход — HTML в --outdir (по умолчанию %TEMP%).
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CORPUS = REPO / "tools" / "bench" / "edit_corpus"

from modules.graph.core import edit_checks  # noqa: E402
from modules.graph.core.graph_access import edge_polyline, edges  # noqa: E402

PAL = {"corner": "#D8232A", "diag": "#E07020", "near": "#E07020",
       "along": "#D9B200", "through": "#8E44AD"}


def _views(res, radius=170.0, limit=8):
    """Очаги: кластеры точек-находок -> [(x1, y1, x2, y2, подпись)]."""
    pts = []
    for d in res["findings"]["corner"]:
        pts.append((d["x"], d["y"], "угол"))
    for d in res["findings"]["diag"]:
        pts.append(((d["a"][0] + d["b"][0]) / 2,
                    (d["a"][1] + d["b"][1]) / 2, "диагональ"))
    for d in res["findings"]["near"]:
        pts.append(((d["a"][0] + d["b"][0]) / 2,
                    (d["a"][1] + d["b"][1]) / 2, "почти-ось"))
    for d in res["findings"]["along"]:
        pts.append(((d["a"][0] + d["b"][0]) / 2,
                    (d["a"][1] + d["b"][1]) / 2, "вдоль"))
    views = []
    pool = list(pts)
    while pool and len(views) < limit:
        sx, sy, _ = pool[0]
        cl = [p for p in pool if abs(p[0] - sx) < radius and abs(p[1] - sy) < radius]
        pool = [p for p in pool if p not in cl]
        xs = [p[0] for p in cl]
        ys = [p[1] for p in cl]
        pad = 70
        x1, x2 = min(xs) - pad, max(xs) + pad
        y1, y2 = min(ys) - pad, max(ys) + pad
        if x2 - x1 < 320:
            cx = (x1 + x2) / 2
            x1, x2 = cx - 160, cx + 160
        if y2 - y1 < 240:
            cy = (y1 + y2) / 2
            y1, y2 = cy - 120, cy + 120
        kinds = sorted({p[2] for p in cl})
        views.append((x1, y1, x2, y2, f"{len(cl)} наход.: {', '.join(kinds)}"))
    return views


def _svg(graph, res, view, width=900):
    x1, y1, x2, y2 = view
    w, h = x2 - x1, y2 - y1
    disp_h = int(width * h / w)
    out = [f'<svg viewBox="{x1:.0f} {y1:.0f} {w:.0f} {h:.0f}" width="{width}" '
           f'height="{disp_h}" style="background:#fbfaf7;border:1px solid '
           f'#d8d6cd;border-radius:8px">']

    def vis(px, py, m=80):
        return x1 - m < px < x2 + m and y1 - m < py < y2 + m

    thru_nodes = {d["node"] for d in res["findings"]["through"]}
    thru_edges = {d["edge"] for d in res["findings"]["through"]}
    for n in graph.get("nodes", []):
        c = n.get("centroid")
        if not c or not vis(float(c[1]), float(c[0])):
            continue
        cont = edit_checks.poly_contour(n)
        hot = n["id"] in thru_nodes
        stroke = PAL["through"] if hot else "#b4b2a9"
        sw = "2" if hot else "1"
        if cont:
            p = " ".join(f"{px:.1f},{py:.1f}" for px, py in cont)
            out.append(f'<polygon points="{p}" fill="#f0eee6" fill-opacity="0.6" '
                       f'stroke="{stroke}" stroke-width="{sw}" '
                       f'vector-effect="non-scaling-stroke"/>')
        else:
            bb = n.get("bbox")
            if bb and len(bb) == 4:
                out.append(f'<rect x="{bb[0]:.1f}" y="{bb[1]:.1f}" '
                           f'width="{bb[2]-bb[0]:.1f}" height="{bb[3]-bb[1]:.1f}" '
                           f'fill="#efede4" stroke="{stroke}" stroke-width="{sw}" '
                           f'vector-effect="non-scaling-stroke" rx="2"/>')
            else:
                out.append(f'<circle cx="{c[1]:.1f}" cy="{c[0]:.1f}" r="2.4" '
                           f'fill="#888780"/>')
    for e in edges(graph):
        pts = edge_polyline(e)
        if len(pts) < 2 or not any(vis(px, py) for px, py in pts):
            continue
        hot = e.get("id") in thru_edges
        col = PAL["through"] if hot else "#9a9890"
        sw = "2.4" if hot else "1.2"
        p = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        out.append(f'<polyline points="{p}" fill="none" stroke="{col}" '
                   f'stroke-width="{sw}" vector-effect="non-scaling-stroke"/>')
    # подсветки поверх
    for d in res["findings"]["along"]:
        (ax, ay), (bx, by) = d["a"], d["b"]
        if vis(ax, ay) or vis(bx, by):
            out.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                       f'stroke="{PAL["along"]}" stroke-width="6" '
                       f'stroke-opacity="0.55" vector-effect="non-scaling-stroke"/>')
    for key, dash in (("diag", ""), ("near", ' stroke-dasharray="6 4"')):
        for d in res["findings"][key]:
            (ax, ay), (bx, by) = d["a"], d["b"]
            if vis(ax, ay) or vis(bx, by):
                out.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                           f'stroke="{PAL["diag"]}" stroke-width="2.6"{dash} '
                           f'vector-effect="non-scaling-stroke"/>')
    for d in res["findings"]["corner_near"]:
        if vis(d["x"], d["y"]):
            out.append(f'<circle cx="{d["x"]}" cy="{d["y"]}" r="6" fill="none" '
                       f'stroke="{PAL["corner"]}" stroke-width="2" '
                       f'vector-effect="non-scaling-stroke"/>'
                       f'<circle cx="{d["x"]}" cy="{d["y"]}" r="1.6" '
                       f'fill="{PAL["corner"]}"/>')
    out.append("</svg>")
    return "".join(out)


def render(path: Path, outdir: Path) -> Path:
    graph = json.loads(path.read_text(encoding="utf-8"))
    res = edit_checks.check_canvas(graph)
    c = res["counts"]

    xs, ys = [], []
    for n in graph.get("nodes", []):
        if n.get("centroid"):
            xs.append(float(n["centroid"][1]))
            ys.append(float(n["centroid"][0]))
    full = (min(xs) - 40, min(ys) - 40, max(xs) + 40, max(ys) + 40)

    parts = [
        '<meta charset="utf-8">',
        f'<title>{path.name}: судья ручной правки</title>',
        '<style>body{font-family:system-ui,sans-serif;margin:20px;'
        'color:#2c2c2a;max-width:1240px}h1{font-size:20px}h2{font-size:15px;'
        'margin:24px 0 6px}table{border-collapse:collapse;font-size:13.5px}'
        'td,th{border:1px solid #d8d6cd;padding:3px 10px;text-align:right}'
        '.leg{font-size:13px;color:#5f5e5a;margin:8px 0}'
        '.leg b{padding:0 6px;border-radius:3px;color:#fff}</style>',
        f'<h1>{path.name} — находки судьи</h1>',
        '<table><tr><th>диагонали</th><th>почти-оси</th><th>вдоль (свой)</th>'
        '<th>вдоль (чужой)</th><th>сквозь узел</th><th>концы у углов</th></tr>',
        f'<tr><td>{c["diag"]}</td><td>{c["near_ortho"]}</td>'
        f'<td>{c["along_own"]}</td><td>{c["along_foreign"]}</td>'
        f'<td>{c["through"]}</td><td>{c["corner_near"]}</td></tr></table>',
        '<div class="leg">'
        f'<b style="background:{PAL["corner"]}">кольцо</b> конец в/у угла рамки '
        f'<b style="background:{PAL["diag"]}">линия</b> диагональ '
        '(пунктир — почти-ось) '
        f'<b style="background:{PAL["along"]};color:#333">полоса</b> вдоль '
        'границы: вышла и не отошла от своего бокса (или пробег ближе 6px '
        'у чужого) '
        f'<b style="background:{PAL["through"]}">контур</b> сквозь чужой узел'
        '</div>',
        '<h2>Карта листа</h2>', _svg(graph, res, full, width=1200)]
    for i, v in enumerate(_views(res), 1):
        parts.append(f'<h2>Очаг {i}: {v[4]}</h2>')
        parts.append(_svg(graph, res, v[:4], width=640))

    out = outdir / f"edit_report_{path.stem}.html"
    out.write_text("".join(parts), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--outdir", type=Path,
                    default=Path(tempfile.gettempdir()) / "edit_reports")
    args = ap.parse_args()
    paths = [Path(f) for f in args.files]
    if args.all:
        paths += [Path(p) for p in
                  sorted(glob.glob(str(CORPUS / "graph_edited*.json")))]
    if not paths:
        ap.error("нет входных файлов")
    args.outdir.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p.exists():
            print("написан", render(p, args.outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
