# -*- coding: utf-8 -*-
"""diameter_lines_draw.py — линии Ду поверх оригинала, для глаз оператора.

Тесты проверяют, что правило работает так, как записано. Совпадает ли записанное
с инженерным смыслом чертежа — судит только человек, поэтому картинка.

    python -X utf8 tools/diameter_lines_draw.py <uid> [--corpus PATH] [--out DIR]

Каждая ЛИНИЯ — свой цвет, и соседние линии гарантированно разного цвета
(жадная раскраска по смежности): иначе на листе из 36 линий две соседние ветки
случайно совпадают по цвету и читаются как одна.

Красная точка — узел, на котором линия прервалась. Подписан класс узла, но
только если он НЕ `connector`: врезка без коллинеарного продолжения — самый
частый и самый скучный случай, её подписи забивают лист. Интересны именно
`perehod`, `unknow`, аппараты — по ним разрыв и оспаривается.

Серым тонким — линии, которым Ду не требуется (импульсные тупики к прибору).
"""

import argparse
import collections
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from modules.binding.diameter_lines import LineRules, build_lines  # noqa: E402

PROJECT_YAML = "configs/projects/thermohydraulics/thermohydraulics.yaml"

# Палитра различимых цветов: соседние линии не должны сливаться.
PALETTE = [
    (220, 30, 30), (30, 110, 220), (20, 150, 60), (230, 140, 0),
    (150, 40, 200), (0, 160, 170), (200, 60, 140), (110, 90, 30),
    (60, 60, 220), (0, 130, 90), (190, 100, 40), (120, 30, 120),
]


def polyline(edge):
    pts = [edge.get("source_point")] + list(edge.get("waypoints") or []) \
        + [edge.get("target_point")]
    return [tuple(p) for p in pts if p]


def _colorize(edges, lines):
    """Цвет каждой линии так, чтобы смежные линии не совпали.

    Смежные = сходятся в одном узле. Без этого на листе из 36 линий палитра из
    12 цветов повторяется, и оператор читает две разные ветки как одну.
    """
    touch = collections.defaultdict(set)
    for i, e in enumerate(edges):
        li = lines.line_for(i)
        if li is None:
            continue
        for nid in (e.get("source"), e.get("target")):
            if nid:
                touch[nid].add(li)
    adj = collections.defaultdict(set)
    for shared in touch.values():
        for a in shared:
            adj[a] |= shared - {a}

    # Раскрашиваем КВАДРАТ графа смежности: разными должны быть не только
    # соседние линии, но и разделённые одной чужой. Иначе «красная — синяя —
    # красная» на одной магистрали читается как одна красная линия с врезкой.
    near = {li: adj[li] | {m for n in adj[li] for m in adj[n]} - {li}
            for li in range(len(lines))}

    color_of = {}
    # Сначала самые «людные» линии: у них меньше свободы выбора.
    for li in sorted(range(len(lines)), key=lambda k: (-len(near[k]), k)):
        used = {color_of[n] for n in near[li] if n in color_of}
        for c in range(len(PALETTE)):
            if c not in used:
                color_of[li] = c
                break
        else:
            color_of[li] = li % len(PALETTE)
    return {li: PALETTE[c] for li, c in color_of.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("uid", help="префикс uid диаграммы")
    ap.add_argument("--corpus", default=os.environ.get("PID_CORPUS", "storage/diagrams"))
    ap.add_argument("--out", default=".")
    ap.add_argument("--width", type=int, default=7)
    args = ap.parse_args()

    found = glob.glob(os.path.join(args.corpus, args.uid + "*"))
    if not found:
        print("не нашёл диаграмму %s в %s" % (args.uid, args.corpus))
        return 2
    uid_dir = Path(found[0])

    with open(uid_dir / "graph" / "graph_validated.json", encoding="utf-8") as f:
        g = json.load(f)
    edges, nodes = g.get("links", []), g.get("nodes", [])
    rules = LineRules.from_project_yaml(PROJECT_YAML)
    lines = build_lines(nodes, edges, rules)

    node_class = {n.get("id"): n.get("class_name", "") for n in nodes}
    degree = collections.Counter()
    for e in edges:
        degree[e.get("source")] += 1
        degree[e.get("target")] += 1

    img = Image.open(uid_dir / "original" / "image.png").convert("RGB")
    dr = ImageDraw.Draw(img)

    color_of = _colorize(edges, lines)

    for li, group in enumerate(lines.edges_of_line):
        color = color_of[li]
        needs = lines.requires_diameter[li]
        for i in group:
            pts = polyline(edges[i])
            for a, b in zip(pts, pts[1:]):
                if needs:
                    dr.line([(a[1], a[0]), (b[1], b[0])], fill=color, width=args.width)
                else:
                    dr.line([(a[1], a[0]), (b[1], b[0])], fill=(150, 150, 150),
                            width=max(2, args.width // 2))

    # Узлы, на которых линия прервалась: степень >= 2, но рёбра разошлись по линиям.
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    incident = collections.defaultdict(list)
    for i, e in enumerate(edges):
        if e.get("source") and e.get("target"):
            incident[e["source"]].append(i)
            incident[e["target"]].append(i)
    breaks = 0
    for nid, eids in incident.items():
        if len(eids) < 2:
            continue
        if len({lines.line_for(i) for i in eids}) == 1:
            continue
        pt = None
        for i in eids:
            e = edges[i]
            pt = e.get("source_point") if e.get("source") == nid else e.get("target_point")
            if pt:
                break
        if not pt:
            continue
        breaks += 1
        y, x = pt[0], pt[1]
        cls = node_class.get(nid, "") or "?"
        dr.ellipse([x - 11, y - 11, x + 11, y + 11], fill=(255, 0, 0),
                   outline=(0, 0, 0), width=2)
        if cls != "connector":
            dr.text((x + 14, y - 13), cls, fill=(200, 0, 0), font=font)

    out = Path(args.out) / ("lines_%s.png" % uid_dir.name[:8])
    img.save(out)
    print("%s: рёбер %d, линий %d (требуют Ду %d), разрывов на узлах %d"
          % (uid_dir.name[:8], len(edges), len(lines), lines.needing_diameter, breaks))
    print("картинка: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
