# -*- coding: utf-8 -*-
"""edit_bench.py — судья вкладки «Ручная правка» на произвольном холсте (Э0).

Меряет холст (canvas-json, координаты холста) предикатами
`modules/graph/core/edit_checks.py` — теми же, что использует движок
редактирования (сторож == судья). Корпус по умолчанию — файлы заказчика
graph_edited*.json в корне репо (7 сохранений одного чертежа с дефектами).

Запуск (из корня репо):
    python -X utf8 tools/edit_bench.py --all                # таблица корпуса
    python -X utf8 tools/edit_bench.py graph_edited_star.json --top 5
    python -X utf8 tools/edit_bench.py --all --write-baseline
    python -X utf8 tools/edit_bench.py --all --check        # против базы, exit 1

Колонки-ДЕФЕКТЫ (в --check не могут расти): diag, near, along_own, along_frn,
through, corner, corner4, conn_off, poly_off, adrift.
Справочные: max_dev, mid, side, manual.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.graph.core import edit_checks  # noqa: E402

BASELINE = REPO / "tools" / "bench" / "edit_baseline.json"

# колонки: (заголовок, ключ counts, дефект?)
COLS = [
    ("diag", "diag", True),
    ("near", "near_ortho", True),
    ("max_dev", "max_dev", False),
    ("al_own", "along_own", True),
    ("al_frn", "along_foreign", True),
    ("thru", "through", True),
    ("corner", "corner", True),
    ("corner4", "corner_near", True),
    ("c_off", "conn_off", True),
    ("p_off", "poly_off", True),
    ("adrift", "adrift", True),
    ("mid", "midpoint", False),
    ("side", "side", False),
    ("manual", "manual", False),
]


def measure_file(path: Path) -> dict:
    graph = json.loads(path.read_text(encoding="utf-8"))
    return edit_checks.check_canvas(graph)


def _print_table(rows: dict[str, dict]):
    name_w = max(len(n) for n in rows) + 2
    head = "файл".ljust(name_w) + " ".join(h.rjust(8) for h, _k, _d in COLS)
    print(head)
    print("-" * len(head))
    for name, res in rows.items():
        c = res["counts"]
        line = name.ljust(name_w) + " ".join(
            str(c.get(k, "")).rjust(8) for _h, k, _d in COLS)
        print(line)


def _print_top(name: str, res: dict, top: int):
    f = res["findings"]
    print(f"\n=== {name}: худшие находки ===")
    for cat, items, fmt in (
        ("диагонали", f["diag"],
         lambda d: f"{d['edge']} seg{d['seg']} dev={d['dev']} len={d['len']}"),
        ("почти-оси", f["near"],
         lambda d: f"{d['edge']} seg{d['seg']} dev={d['dev']} len={d['len']}"),
        ("вдоль границы", f["along"],
         lambda d: f"{d['edge']} у {d['node']}{' (свой)' if d['own'] else ''} "
                   f"gap={d['gap']} пробег={d['overlap']}"),
        ("сквозь узел", f["through"],
         lambda d: f"{d['edge']} сквозь {d['node']} len={d['inside_len']}"),
        ("концы в углах", f["corner"],
         lambda d: f"{d['edge']} {d['key']} @ {d['node']} "
                   f"({d['x']}, {d['y']})"),
    ):
        if not items:
            continue
        print(f"  {cat}: {len(items)}")
        for d in items[:top]:
            print(f"    {fmt(d)}")


def _compare(rows: dict[str, dict], base: dict) -> int:
    """Сравнение с базой: дефектные колонки не могут расти. 0 = ок."""
    bad = 0
    for name, res in rows.items():
        b = base.get(name)
        if b is None:
            print(f"{name}: в базе нет — пропуск сравнения")
            continue
        for _h, key, is_defect in COLS:
            if not is_defect:
                continue
            was, now = b.get(key, 0), res["counts"].get(key, 0)
            if now > was:
                print(f"ХУЖЕ  {name}: {key} {was} -> {now}")
                bad += 1
            elif now < was:
                print(f"лучше {name}: {key} {was} -> {now}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="canvas-json файлы")
    ap.add_argument("--all", action="store_true",
                    help="graph_edited*.json из корня репо")
    ap.add_argument("--top", type=int, default=0,
                    help="печатать топ-N находок по каждому файлу")
    ap.add_argument("--json", type=Path, default=None,
                    help="выгрузить counts в json")
    ap.add_argument("--write-baseline", action="store_true",
                    help=f"заморозить базу в {BASELINE.relative_to(REPO)}")
    ap.add_argument("--check", action="store_true",
                    help="сравнить с базой, exit 1 при росте дефектов")
    args = ap.parse_args()

    paths = [Path(f) for f in args.files]
    if args.all:
        paths += [Path(p) for p in sorted(glob.glob(str(REPO / "graph_edited*.json")))]
    if not paths:
        ap.error("нет входных файлов (--all или список)")

    rows = {}
    for p in paths:
        if not p.exists():
            print(f"{p}: нет файла — пропуск")
            continue
        rows[p.name] = measure_file(p)

    _print_table(rows)
    if args.top:
        for name, res in rows.items():
            _print_top(name, res, args.top)

    counts_only = {n: r["counts"] for n, r in rows.items()}
    if args.json:
        args.json.write_text(
            json.dumps(counts_only, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"\ncounts -> {args.json}")
    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(counts_only, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"\nбаза заморожена -> {BASELINE.relative_to(REPO)}")
    if args.check:
        if not BASELINE.exists():
            print("базы нет — сначала --write-baseline")
            return 2
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        bad = _compare(rows, base)
        print(f"\nрост дефектов: {bad}")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
