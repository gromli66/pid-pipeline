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

Те же три исхода у `--write-baseline` (пункт 1-30, четвёртый стенд после
1-25/1-26/1-28): 0 — эталон переснят, 1 — ОТКАЗ (дефекты выросли, пересъём
узаконил бы рост), 2 — СУДИТЬ НЕЧЕМ (корпус усечён, пересъём вычеркнул бы
неизмеренное). См. `write_blocked()`.
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


def _rel(path: Path) -> str:
    """Путь от корня репо, если он внутри. Иначе — как есть.

    Стенд под тестом получает временный корпус и временный эталон вне дерева
    (так проверяются три соседних стенда), и голый `relative_to` там падает
    `ValueError` ещё на сборке справки argparse.
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def read_baseline() -> dict:
    """Эталон или пустой словарь. Имя и форма — как у соседних стендов."""
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


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


def defect_growth(rows: dict[str, dict],
                  base: dict) -> dict[str, list[tuple[str, int, int]]]:
    """{файл: [(колонка, было, стало), ...]} — только там, где дефект вырос.

    Арифметика общая у `--check` и у пересъёма (пункт 1-30): два судьи одного
    корпуса не должны разъехаться — тот же урок, что у `floor_problems`
    в `suite_baseline.py` и у `debt_grown` в `lint_gate.py`.
    Файла нет в эталоне — сравнивать не с чем, это пропуск, а не рост.
    """
    grown: dict[str, list[tuple[str, int, int]]] = {}
    for name, res in rows.items():
        b = base.get(name)
        if b is None:
            continue
        for _h, key, is_defect in COLS:
            if not is_defect:
                continue
            was, now = b.get(key, 0), res["counts"].get(key, 0)
            if now > was:
                grown.setdefault(name, []).append((key, was, now))
    return grown


def unmeasured(rows: dict[str, dict], base: dict) -> list[str]:
    """Файлы эталона, которых в этом замере нет. Общее у чтения и записи."""
    return sorted(set(base) - set(rows))


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
    grown = defect_growth(rows, base)
    for name, res in rows.items():
        b = base.get(name)
        if b is None:
            print(f"{name}: в базе нет — пропуск сравнения")
            continue
        for key, was, now in grown.get(name, ()):
            print(f"ХУЖЕ  {name}: {key} {was} -> {now}")
        for _h, key, is_defect in COLS:
            was, now = b.get(key, 0), res["counts"].get(key, 0)
            if is_defect and now < was:
                print(f"лучше {name}: {key} {was} -> {now}")
    bad = sum(len(cols) for cols in grown.values())
    print(f"\nрост дефектов: {bad}")
    if bad:
        return 1
    if not base:
        print("судить нечем: эталон пуст — сначала --write-baseline")
        return 2
    missing = unmeasured(rows, base)
    if missing:
        print(f"судить нечем: не измерено {len(missing)} файлов эталона "
              f"из {len(base)} — корпус усечён: {', '.join(missing)}")
        return 2
    return 0


def write_blocked(rows: dict[str, dict],
                  base: dict) -> tuple[list[str], list[str]]:
    """-> (причины «судить нечем», причины отказа). Обе пустые = пересъём законен.

    Четвёртый стенд с той же дырой, что 1-25/1-26/1-28 закрыли у трёх соседей
    (находка ревизии 1-28, `tools/edit_bench.py:185`): `--write-baseline`
    не читал эталон ВООБЩЕ — записывал то, что намерилось, и печатал «база
    заморожена». Пересъём идёт отдельным коммитом, как требует Д6, поэтому
    красный флаг №4 протокола («эталон изменён тем же коммитом, что и код»)
    этого не видит: рост дефектов «Ручной правки» становился нормой молча,
    а следующий `--check` на здоровом дереве был зелёным.

    Три исхода (`PROTOCOL §5`), а не два:
    2 — СУДИТЬ НЕЧЕМ: часть файлов эталона не измерена. Корпус лежит вне git
        (`.gitignore:37`), и пересъём на усечённом корпусе вычеркнул бы
        неизмеренные холсты из эталона — ровно то, что 1-28 замерил у ПР1;
    1 — ОТКАЗ: замер годен и говорит, что дефектная колонка выросла;
    0 — законно: измерен весь эталон и ни одна дефектная колонка не выросла.
        Первый снимок (эталона в дереве нет) сверять не с чем — он проходит.
    Доказанный рост сильнее неполноты, как у `_compare` и у трёх соседей.
    """
    if not base:                                    # первый снимок
        return [], []
    unjudged, refused = [], []
    missing = unmeasured(rows, base)
    if missing:
        unjudged.append(
            f"не измерено {len(missing)} файлов эталона из {len(base)} — "
            f"корпус усечён (он вне git, .gitignore:37), пересъём вычеркнул "
            f"бы их из эталона: {', '.join(missing)}")
    grown = defect_growth(rows, base)
    if grown:
        refused.append(
            "дефекты выросли против эталона — пересъём узаконит рост:\n"
            + "\n".join(f"    {name}: {key} {was} -> {now}"
                        for name, cols in sorted(grown.items())
                        for key, was, now in cols)
            + "\n  Выросший дефект чинят или откатывают.")
    return unjudged, refused


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="canvas-json файлы")
    ap.add_argument("--all", action="store_true",
                    help=f"graph_edited*.json из {_rel(CORPUS)}")
    ap.add_argument("--top", type=int, default=0,
                    help="печатать топ-N находок по каждому файлу")
    ap.add_argument("--json", type=Path, default=None,
                    help="выгрузить counts в json")
    ap.add_argument("--write-baseline", action="store_true",
                    help=f"заморозить базу в {_rel(BASELINE)}")
    ap.add_argument("--check", action="store_true",
                    help="сравнить с базой, exit 1 при росте дефектов")
    args = ap.parse_args()

    paths = [Path(f) for f in args.files]
    if args.all:
        found = sorted(glob.glob(str(CORPUS / "graph_edited*.json")))
        if not found and not paths:
            print(f"судить нечем: в {_rel(CORPUS)} нет ни одного "
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
        unjudged, refused = write_blocked(rows, read_baseline())
        for msg in unjudged:
            print(f"[СУДИТЬ НЕЧЕМ] {msg}")
        for msg in refused:
            print(f"[ОТКАЗ] {msg}")
        if refused:                  # доказанный рост сильнее неполноты (1-25)
            return 1
        if unjudged:
            return 2
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(counts_only, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"\nбаза заморожена -> {_rel(BASELINE)}")
    if args.check:
        if not BASELINE.exists():
            print("базы нет — сначала --write-baseline")
            return 2
        return _compare(rows, read_baseline())
    return 0


if __name__ == "__main__":
    sys.exit(main())
