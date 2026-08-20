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

⛔ Эталон помнит ОТПЕЧАТОК каждого холста (пункт GATE-6, форма `version: 2`):
имя файла говорит только «как называется», а не «что внутри». Отпечаток
разошёлся — «судить нечем» с указанием файла, а не «дефекты выросли»;
на записи он, наоборот, снимает отказ (утверждение о росте — это утверждение
о ТОМ ЖЕ холсте). Старый плоский вид `{файл: counts}` читается как
«отпечатков нет», то есть тоже «судить нечем» — до первого пересъёма.

⛔ Те же три исхода — и на ВХОДНЫХ ДАННЫХ (пункт 1-42): битый холст (не
разобрался json, файл не прочитан, разобрался, но это не холст) и пустой
замер дают «судить нечем», а не трейсбек с кодом 1. Ошибка самого судьи
`edit_checks` при этом НЕ глушится — см. `canvas_problem()`.
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
from tools import corpus  # noqa: E402

BASELINE = REPO / "tools" / "bench" / "edit_baseline.json"
CORPUS = REPO / "tools" / "bench" / "edit_corpus"
BASELINE_VERSION = 2

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


def baseline_parts(base: dict) -> tuple[dict, dict]:
    """-> (counts по файлам, отпечатки входа по файлам).

    Форма v2 (пункт GATE-6) держит рядом с числами отпечаток данных, на
    которых они сняты. Старый плоский вид `{файл: counts}` читается как
    «отпечатков нет»: он не может назвать холст, о котором судит.
    """
    if base.get("version"):
        return base.get("files", {}), base.get("inputs", {})
    return base, {}


def input_fingerprints(paths: list[Path]) -> dict[str, str]:
    """{имя файла: отпечаток входных данных} — чем именно кормили стенд."""
    return {p.name: corpus.data_fingerprint(p) for p in paths}


def input_drift(inputs: dict[str, str], base: dict) -> dict[str, str]:
    """{имя файла: чем именно судить нечем} — холсты, о которых эталон молчит.

    Дыра одна на два стенда (пункт GATE-6): у ПР1 эталон ключевался `uid8`,
    здесь — именем файла, и ни один не помнил СОДЕРЖИМОГО. Холст под тем же
    именем может быть другим, и «дефекты выросли» сказало бы о нём неправду.
    Файла нет в эталоне — сверять нечего, это прежний пропуск, а не двойка.
    """
    files, known = baseline_parts(base)
    drift: dict[str, str] = {}
    for name in sorted(inputs):
        if name not in files:
            continue
        was = known.get(name)
        if was is None:
            drift[name] = ("эталон не помнит отпечатка входных данных — "
                           "он снят до пункта GATE-6")
        elif was != inputs[name]:
            drift[name] = (f"входные данные сменились: {was[:16]} -> "
                           f"{inputs[name][:16]}")
    return drift


def canvas_problem(graph) -> str | None:
    """Почему по этому холсту судить нечем, или None — холст годен.

    Корпус лежит ВНЕ git (`.gitignore:37`), то есть холст — это ДАННЫЕ,
    а не код: битый файл обязан давать «судить нечем» (exit 2), а не трейсбек
    с кодом 1, неотличимый от доказанного роста дефектов (`PROTOCOL §5`).
    Та же дыра, что 1-30 закрыл у дочернего прогона ПР1, только на входных
    данных, а не на среде.

    Проверяется РОВНО то, чего касаются `graph_access.edges()` и
    `nodes_by_id()`; шире — значит ловить своей проверкой ошибки судьи,
    а их прятать нельзя: поломка `edit_checks` должна оставаться красной.
    """
    if not isinstance(graph, dict):
        return f"холст не объект json, а {type(graph).__name__}"
    nodes = graph.get("nodes", [])
    if not isinstance(nodes, list):
        return f"поле nodes не список, а {type(nodes).__name__}"
    for n in nodes:
        if not isinstance(n, dict):
            return f"узел не объект json, а {type(n).__name__}"
        if "id" not in n:
            return "у узла нет поля id"
    links = graph.get("edges") if "edges" in graph else graph.get("links", [])
    if not isinstance(links, list):
        return f"поле рёбер не список, а {type(links).__name__}"
    for e in links:
        if not isinstance(e, dict):
            return f"ребро не объект json, а {type(e).__name__}"
    return None


def measure_file(path: Path) -> dict:
    """Счётчики и находки по холсту.

    `ValueError` — холст не годен для суда (не разобрался или не похож
    на холст); `main()` читает это как «судить нечем», см. `canvas_problem`.
    """
    graph = json.loads(path.read_text(encoding="utf-8"))
    problem = canvas_problem(graph)
    if problem is not None:
        raise ValueError(problem)
    return edit_checks.check_canvas(graph)


def _print_table(rows: dict[str, dict]):
    if not rows:                       # пустой набор: `max()` падал ValueError
        print("таблица пуста: ни один холст не измерен")
        return
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


def defect_growth(rows: dict[str, dict], base: dict,
                  drift: dict[str, str] | None = None,
                  ) -> dict[str, list[tuple[str, int, int]]]:
    """{файл: [(колонка, было, стало), ...]} — только там, где дефект вырос.

    Арифметика общая у `--check` и у пересъёма (пункт 1-30): два судьи одного
    корпуса не должны разъехаться — тот же урок, что у `floor_problems`
    в `suite_baseline.py` и у `debt_grown` в `lint_gate.py`.
    Файла нет в эталоне — сравнивать не с чем, это пропуск, а не рост.
    Холст из `drift` — тот же пропуск: числа эталона сняты не с него
    (пункт GATE-6).
    """
    files, _ = baseline_parts(base)
    drift = drift or {}
    grown: dict[str, list[tuple[str, int, int]]] = {}
    for name, res in rows.items():
        b = None if name in drift else files.get(name)
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
    return sorted(set(baseline_parts(base)[0]) - set(rows))


def _compare(rows: dict[str, dict], base: dict, inputs: dict[str, str]) -> int:
    """Сравнение с базой: дефектные колонки не могут расти.

    Три исхода (`PROTOCOL §5`), а не два:
    0 — не хуже базы, и при этом измерен ВЕСЬ её корпус;
    1 — опровергнуто: дефектная колонка выросла;
    2 — СУДИТЬ НЕЧЕМ: база пуста, часть её файлов не измерена или ХОЛСТ
        НЕ ТОТ, на котором сняты её числа (пункт GATE-6). Корпус лежит вне
        git (`.gitignore:37`), поэтому на чистом дереве мерить нечего —
        а раньше такой прогон печатал «рост дефектов: 0» и exit 0.
    Доказанный рост сильнее неполноты: если что-то выросло, это 1.
    """
    files, _ = baseline_parts(base)
    drift = input_drift(inputs, base)
    grown = defect_growth(rows, base, drift)
    for name, res in rows.items():
        if name in drift:
            print(f"судить нечем  {name}: {drift[name]}")
            continue
        b = files.get(name)
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
    if not files:
        print("судить нечем: эталон пуст — сначала --write-baseline")
        return 2
    if drift:
        print(f"судить нечем: по {len(drift)} файлам эталон не отвечает за свои "
              f"числа — отпечаток входа не сходится или его нет вовсе: "
              f"{', '.join(sorted(drift))}")
        return 2
    missing = unmeasured(rows, base)
    if missing:
        print(f"судить нечем: не измерено {len(missing)} файлов эталона "
              f"из {len(files)} — корпус усечён: {', '.join(missing)}")
        return 2
    return 0


def write_blocked(rows: dict[str, dict], base: dict,
                  inputs: dict[str, str]) -> tuple[list[str], list[str]]:
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

    ⭐ Отпечаток входа (GATE-6) отказ СНИМАЕТ, а не ставит: «дефекты выросли» —
    утверждение о ТОМ ЖЕ холсте, и при другом холсте его просто нет. Пересъём
    подменённых данных законен, но не молчалив: причину печатает `main()`
    строкой `[ВХОД НЕ ТОТ]`.
    """
    files, _ = baseline_parts(base)
    if not files:                                   # первый снимок
        return [], []
    unjudged, refused = [], []
    missing = unmeasured(rows, base)
    if missing:
        unjudged.append(
            f"не измерено {len(missing)} файлов эталона из {len(files)} — "
            f"корпус усечён (он вне git, .gitignore:37), пересъём вычеркнул "
            f"бы их из эталона: {', '.join(missing)}")
    grown = defect_growth(rows, base, input_drift(inputs, base))
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

    rows, measured, unjudged_input = {}, [], []
    for p in paths:
        if not p.exists():
            print(f"{p}: нет файла — пропуск")
            continue
        try:
            rows[p.name] = measure_file(p)
        except (OSError, ValueError) as exc:
            # Битый холст — сломанные ДАННЫЕ, а не регресс кода: трейсбек
            # с кодом 1 говорил бы «дефекты выросли» (`PROTOCOL §5`).
            print(f"[СУДИТЬ НЕЧЕМ] {p.name}: холст не разобран — {exc}")
            unjudged_input.append(f"{p.name}: {exc}")
            continue
        measured.append(p)
    if not rows:
        unjudged_input.append("ни один холст не измерен — судить не по чему")
    inputs = input_fingerprints(measured)

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
        for name, why in input_drift(inputs, read_baseline()).items():
            print(f"[ВХОД НЕ ТОТ] {name}: {why}; вердикт эталона об этом "
                  "холсте к нынешним данным не относится")
        unjudged, refused = write_blocked(rows, read_baseline(), inputs)
        if unjudged_input:
            unjudged.append(
                "судить нечем по холстам: " + "; ".join(unjudged_input)
                + " — пересъём записал бы эталон без них")
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
            json.dumps({"version": BASELINE_VERSION, "files": counts_only,
                        "inputs": inputs}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"\nбаза заморожена -> {_rel(BASELINE)}")
    if args.check:
        if not BASELINE.exists():
            print("базы нет — сначала --write-baseline")
            return 2
        code = _compare(rows, read_baseline(), inputs)
        if code == 0 and unjudged_input:
            print(f"судить нечем по холстам: {'; '.join(unjudged_input)}")
            return 2
        return code            # доказанный рост сильнее неполноты (1-25)
    return 2 if unjudged_input else 0


if __name__ == "__main__":
    sys.exit(main())
