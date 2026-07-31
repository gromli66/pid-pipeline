# -*- coding: utf-8 -*-
"""corner_probe.py — пробник Э-A: концы рёбер на УГЛАХ прямоугольной посадки.

Угол — обе координаты конца на границах рамки посадки (`seating._anchor_rect`,
tol 1 px). Ray-канон сажает пару «бокс -> крупный контур» лучом в угол бокса
(жалоба заказчика: edge_52 в graph_edited_33, пин [1022.2, 340.6] на углу
node_36) — портовая посадка пинов роутинга обязана свести такие концы к ~0.

Полигонные узлы без скина не меряются: их конец сидит на контуре, у контура
нет «угла рамки». Соосные строгие прямые могут легально прижиматься к углу
(кламп замка в край грани) — такие случаи перечисляются поимённо.

Запуск (из корня репо): python -X utf8 tools/corner_probe.py [--uid c2f79462]
Меряется ВЫХОД раскладки (to_canvas + layout()), как в layout_bench.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.graph.core.layout import LayoutParams, layout          # noqa: E402
from modules.graph.core.graph_access import (edge_ends, edges,      # noqa: E402
                                             is_connector, nodes_by_id)
from modules.graph.core.canvas_input import to_canvas               # noqa: E402
from modules.graph.core.pretransform import FIXED_SIZES             # noqa: E402
from modules.graph.core.seating import _anchor_rect                 # noqa: E402

from layout_bench import CORPUS                                     # noqa: E402

# заказчицкий граф вне бенч-корпуса — диагноз этапа A снят на нём
EXTRA = {"c2f79462": "c2f79462-02cc-4141-93ad-5d2275aac63c"}

TOL = 1.0


def _rect_seated(node):
    """Конец узла сидит на рамке (`_anchor_rect`), а не на контуре."""
    if node is None or is_connector(node):
        return False
    seg = node.get("segmentation")
    if seg and isinstance(seg, list) and len(seg) >= 6 \
            and node.get("class_name") not in FIXED_SIZES \
            and not node.get("_axis"):
        return False                    # посадка на контур — угла рамки нет
    return _anchor_rect(node) is not None


def corner_ends(graph, tol=TOL):
    """[(edge_id, node_id, key, x, y)] — концы на углах рамки посадки."""
    byid = nodes_by_id(graph)
    out = []
    for e in edges(graph):
        s, t = edge_ends(e)
        for nid, key in ((s, "source_point"), (t, "target_point")):
            p = e.get(key)
            node = byid.get(nid)
            if p is None or not _rect_seated(node):
                continue
            x1, y1, x2, y2 = _anchor_rect(node)
            x, y = float(p[1]), float(p[0])
            if min(abs(x - x1), abs(x - x2)) <= tol \
                    and min(abs(y - y1), abs(y - y2)) <= tol:
                out.append((e.get("id"), nid, key, round(x, 1), round(y, 1)))
    return out


def _load_input(uid8):
    full = CORPUS.get(uid8) or EXTRA.get(uid8)
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
    args = ap.parse_args()

    uids = args.uid or (list(CORPUS) + list(EXTRA))
    total = 0
    for uid8 in uids:
        g = _load_input(uid8)
        if g is None:
            print(f"{uid8}: ПРОПУЩЕН — входа нет", flush=True)
            continue
        aft, _st = layout(g, LayoutParams())
        hits = corner_ends(aft)
        total += len(hits)
        print(f"{uid8}: углов {len(hits)}", flush=True)
        for eid, nid, key, x, y in hits:
            print(f"    {eid} {key} @ {nid} ({x}, {y})", flush=True)
    print(f"\nИТОГО углов: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
