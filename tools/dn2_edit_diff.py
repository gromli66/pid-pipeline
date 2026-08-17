# -*- coding: utf-8 -*-
"""dn2_edit_diff.py — ЧТО оператор правит на холсте (ДН2, пункт дороги 0.9).

Читает корпус локальных сохранений «Ручной правки» (`graph_edited*.json`),
группирует файлы по `canvas_transform.source_sha` (один sha = один исходный
`graph_validated`), сортирует группу по времени файла и считает ПОПАРНЫЕ диффы
соседних сохранений с разбором по классам правки: перемещение узла, изменение
размера, добавление/удаление, переделка ребра, перецепка конца.

Исходник («до оператора») у корпуса не сохранён, поэтому дифф только «между
сохранениями» — это ограничение данных, а не инструмента: если для sha нашёлся
`storage/diagrams/*/graph/graph_validated.json` с тем же хешем, файл называется
в шапке группы, но точкой отсчёта не становится (холст из него сегодня уже не
воспроизводится — раскладка другой версии).

⚠ Диффы через границу `layout_version` смешивают правки руками с изменениями
самого движка раскладки; такие пары в таблице помечены `!` и считаются отдельно.

Запуск (из корня репо):
    python -X utf8 tools/dn2_edit_diff.py                 # корпус из корня репо
    python -X utf8 tools/dn2_edit_diff.py --dir path/to   # корпус из папки
    python -X utf8 tools/dn2_edit_diff.py --json          # машинный вывод

Только чтение: ни один файл корпуса не изменяется.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Порог «изменилось» в пикселях холста 1920x1080. Полпикселя: меньше — шум
# сериализации float, больше — потеря аккуратных доводок оператора.
EPS = 0.5

# Классы правки: (ключ, заголовок колонки).
CLASSES = [
    ("node_moved", "n_move"),
    ("node_resized", "n_size"),
    ("node_added", "n_add"),
    ("node_removed", "n_del"),
    ("node_retyped", "n_type"),
    ("edge_added", "e_add"),
    ("edge_removed", "e_del"),
    ("edge_rewired", "e_wire"),
    ("edge_end_own", "e_end_own"),
    ("edge_end_follow", "e_end_flw"),
    ("edge_rerouted", "e_route"),
]

# Намерение оператора против следствия. `e_end_flw` — конец переехал вслед за
# своим узлом (посадка), `e_route` — кэш последнего расчёта маршрута
# (`docs/DATA_FORMATS.md`, «Пины входа»), а не намерение. В доле правок считать
# их наравне с перемещением узла — значит утроить вес одного действия.
INTENT = {"node_moved", "node_resized", "node_added", "node_removed",
          "node_retyped", "edge_added", "edge_removed", "edge_rewired",
          "edge_end_own"}


def _centroid(node):
    """Центроид узла — `[y, x]` (ловушка осей: bbox рядом лежит `[x, y]`)."""
    c = node.get("centroid")
    return (float(c[0]), float(c[1])) if c else None


def _size(node):
    """Размер узла из bbox `[x1, y1, x2, y2]` → (ширина, высота)."""
    b = node.get("bbox")
    if not b or len(b) != 4:
        return None
    return (abs(float(b[2]) - float(b[0])), abs(float(b[3]) - float(b[1])))


def _far(a, b) -> bool:
    if a is None or b is None:
        return a is not b
    return any(abs(x - y) > EPS for x, y in zip(a, b))


def _point(edge, key):
    p = edge.get(key)
    return (float(p[0]), float(p[1])) if p else None


def _route(edge):
    return json.dumps(edge.get("waypoints") or [], sort_keys=True)


def diff_pair(before: dict, after: dict) -> dict:
    """Классифицированный дифф двух холстов. Тождество объектов — по `id`."""
    nb = {n["id"]: n for n in before.get("nodes", [])}
    na = {n["id"]: n for n in after.get("nodes", [])}
    eb = {e["id"]: e for e in before.get("links", [])}
    ea = {e["id"]: e for e in after.get("links", [])}

    res = {k: 0 for k, _ in CLASSES}
    res["node_added"] = len(set(na) - set(nb))
    res["node_removed"] = len(set(nb) - set(na))
    for nid in set(nb) & set(na):
        if _far(_centroid(nb[nid]), _centroid(na[nid])):
            res["node_moved"] += 1
        if _far(_size(nb[nid]), _size(na[nid])):
            res["node_resized"] += 1
        if nb[nid].get("type") != na[nid].get("type"):
            res["node_retyped"] += 1

    moved_nodes = {nid for nid in set(nb) & set(na)
                   if _far(_centroid(nb[nid]), _centroid(na[nid]))
                   or _far(_size(nb[nid]), _size(na[nid]))}

    res["edge_added"] = len(set(ea) - set(eb))
    res["edge_removed"] = len(set(eb) - set(ea))
    for eid in set(eb) & set(ea):
        b, a = eb[eid], ea[eid]
        if (b.get("source"), b.get("target")) != (a.get("source"), a.get("target")):
            res["edge_rewired"] += 1
        if (_far(_point(b, "source_point"), _point(a, "source_point"))
                or _far(_point(b, "target_point"), _point(a, "target_point"))):
            # Конец поехал сам или вслед за узлом — разные события: первое
            # намерение оператора, второе следствие перемещения узла.
            own = not ({a.get("source"), a.get("target")} & moved_nodes)
            res["edge_end_own" if own else "edge_end_follow"] += 1
        if _route(b) != _route(a):
            res["edge_rerouted"] += 1

    res["text_blocks_delta"] = (len(after.get("text_blocks") or [])
                                - len(before.get("text_blocks") or []))
    res["bindings_delta"] = (len(after.get("bindings") or [])
                             - len(before.get("bindings") or []))
    return res


def _tr(graph: dict) -> dict:
    return (graph.get("graph") or {}).get("canvas_transform") or {}


def load_corpus(directory: Path) -> list:
    """Файлы корпуса с меткой холста, отсортированные по времени файла."""
    out = []
    for path in sorted(directory.glob("graph_edited*.json")):
        graph = json.loads(path.read_text(encoding="utf-8"))
        out.append({
            "path": path,
            "graph": graph,
            "tr": _tr(graph),
            "mtime": path.stat().st_mtime,
        })
    return sorted(out, key=lambda item: item["mtime"])


def group_by_source(files: list) -> dict:
    groups: dict = {}
    for item in files:
        groups.setdefault(item["tr"].get("source_sha"), []).append(item)
    return groups


def build_report(directory: Path) -> dict:
    files = load_corpus(directory)
    groups = group_by_source(files)
    report = {"dir": str(directory), "files": len(files), "groups": []}
    for sha, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        pairs = []
        for before, after in zip(items, items[1:]):
            counts = diff_pair(before["graph"], after["graph"])
            counts["from"] = before["path"].name
            counts["to"] = after["path"].name
            counts["cross_layout_version"] = (
                before["tr"].get("layout_version") != after["tr"].get("layout_version"))
            pairs.append(counts)
        report["groups"].append({
            "source_sha": sha,
            "files": [i["path"].name for i in items],
            "operator_saved": sum(1 for i in items if i["tr"].get("operator_saved")),
            "nodes": len(items[0]["graph"].get("nodes", [])),
            "edges": len(items[0]["graph"].get("links", [])),
            "pairs": pairs,
        })
    return report


def _totals(pairs, only_clean=False):
    rows = [p for p in pairs if not (only_clean and p["cross_layout_version"])]
    return {k: sum(p[k] for p in rows) for k, _ in CLASSES}, len(rows)


def print_report(report: dict) -> None:
    print(f"корпус: {report['dir']} — файлов {report['files']}, "
          f"групп по source_sha {len(report['groups'])}")
    all_pairs = []
    for grp in report["groups"]:
        print(f"\n=== source_sha {grp['source_sha']} — файлов {len(grp['files'])}, "
              f"operator_saved {grp['operator_saved']}, "
              f"узлов {grp['nodes']}, рёбер {grp['edges']}")
        head = "пара".ljust(46) + " ".join(h.rjust(7) for _k, h in CLASSES)
        print(head)
        print("-" * len(head))
        for p in grp["pairs"]:
            name = f"{'!' if p['cross_layout_version'] else ' '}{p['from']} → {p['to']}"
            print(name.ljust(46) + " ".join(
                str(p[k]).rjust(7) for k, _h in CLASSES))
        all_pairs.extend(grp["pairs"])

    for label, only_clean in (("ВСЕ пары", False), ("без границ версий", True)):
        totals, n = _totals(all_pairs, only_clean)
        total = sum(totals.values())
        intent = sum(v for k, v in totals.items() if k in INTENT)
        print(f"\n--- {label}: пар {n}, изменений всего {total}, "
              f"из них намерений оператора {intent}")
        if not total:
            continue
        for k, h in CLASSES:
            if not totals[k]:
                continue
            share = f"{100.0 * totals[k] / intent:5.1f} %" if k in INTENT else "следствие"
            print(f"    {h:<10} {totals[k]:>6}  {share}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ДН2: что оператор правит на холсте")
    ap.add_argument("--dir", default=str(REPO), help="папка с graph_edited*.json")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    args = ap.parse_args(argv)

    directory = Path(args.dir)
    report = build_report(directory)
    if not report["files"]:
        print(f"корпус пуст: {directory}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
