# -*- coding: utf-8 -*-
"""edit_bench.py — судья вкладки «Ручная правка» на произвольном холсте (Э0).

Меряет холст (canvas-json, координаты холста) предикатами
`modules/graph/core/edit_checks.py` — теми же, что использует движок
редактирования (сторож == судья). Корпус по умолчанию — файлы заказчика
graph_edited*.json в tools/bench/edit_corpus/ (локальные сохранения «Ручной
правки»; вне git, как и остальные данные корпуса).

Запуск (из корня репо):
    python -X utf8 tools/edit_bench.py --all                # таблица корпуса
    python -X utf8 tools/edit_bench.py tools/bench/edit_corpus/graph_edited_star.json --top 5
    python -X utf8 tools/edit_bench.py --all --write-baseline
    python -X utf8 tools/edit_bench.py --all --check        # против базы: 0 · 1 · 2

Колонки-ДЕФЕКТЫ (в --check не могут расти): diag, near, along_own, along_frn,
through, corner, corner4, conn_off, poly_off, adrift.
Справочные: max_dev, mid, side, manual.

⚠ **exit 2 — «судить нечем»** (`PROTOCOL §5`): корпус вне git (`.gitignore:37`),
и на чистом дереве сравнивать не с чем. Замер части корпуса — не «дефекты не
выросли»: судится ровно тот набор файлов, что записан в эталоне.
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
CORPUS = REPO / "tools" / "bench" / "edit_corpus"

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
    """Сравнение с базой: дефектные колонки не могут расти.

    Три исхода (`PROTOCOL §5`), а не два:
    0 — не хуже базы, и при этом измерен ВЕСЬ её корпус;
    1 — опровергнуто: дефектная колонка выросла;
    2 — СУДИТЬ НЕЧЕМ: база пуста или часть её файлов не измерена. Корпус
        лежит вне git (`.gitignore:37`), поэтому на чистом дереве мерить
        нечего — а раньше такой прогон печатал «рост дефектов: 0» и exit 0.
    Доказанный рост сильнее неполноты: если что-то выросло, это 1.
    """
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
    print(f"\nрост дефектов: {bad}")
    if bad:
        return 1
    if not base:
        print("судить нечем: эталон пуст — сначала --write-baseline")
        return 2
    missing = sorted(set(base) - set(rows))
    if missing:
        print(f"судить нечем: не измерено {len(missing)} файлов эталона "
              f"из {len(base)} — корпус усечён: {', '.join(missing)}")
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="canvas-json файлы")
    ap.add_argument("--all", action="store_true",
                    help=f"graph_edited*.json из {CORPUS.relative_to(REPO)}")
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
        found = sorted(glob.glob(str(CORPUS / "graph_edited*.json")))
        if not found and not paths:
            print(f"судить нечем: в {CORPUS.relative_to(REPO)} нет ни одного "
                  f"graph_edited*.json — корпус вне git (.gitignore:37)")
            return 2
        paths += [Path(p) for p in found]
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
        return _compare(rows, base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
