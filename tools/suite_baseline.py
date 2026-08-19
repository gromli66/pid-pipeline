# -*- coding: utf-8 -*-
"""suite_baseline.py — базовая линия набора тестов: что падает СЕГОДНЯ (пункт 0.3).

Зачем. Набор красный не весь: часть тестов падает своими причинами, не связанными
с текущей работой. Пока этот список не зафиксирован, любой рефакторинг получает
чужие падения на свой счёт. Поэтому список красных лежит в git отдельным файлом
(`tools/bench/suite_baseline.json`), а гейт проверяет не «ноль красных», а
«ни одного НОВОГО красного».

Контракт как у остальных стендов проекта (`edit_bench.py`, `layout_bench.py`):
    python -X utf8 tools/suite_baseline.py --check           # сверка, exit 1 при регрессии
    python -X utf8 tools/suite_baseline.py --write-baseline  # пересъём базы (Д6: отдельный коммит)

Что считается провалом (`--check` → exit 1):
    * НОВЫЙ красный — тест, который в базе зелёный, а сейчас упал;
    * усыхание набора — тесты пропали у файлов, которые в диффе не менялись
      (`importorskip` без установленной зависимости, `collect_ignore`,
      сломанный `conftest`). Считается по КАРТЕ ФАЙЛОВ из базы, а не одним
      числом (пункт 1-27): одно число не отличало тихую пропажу от штатного
      отката пункта, и `git revert` по тегу красил гейт законным возвратом,
      то есть тег переставал быть точкой возврата (`PROTOCOL §6`). Файл,
      который правили, и файл, снятый вместе с записью о нём в git, — это
      не усыхание: их видно в диффе и судит ревизор, а стенд их печатает
      и вычитает из счёта. В карту идёт git-видимая часть сбора — без
      параметров по корпусу, которого нет в git (пункт 0.3y): иначе счёт
      зависел бы от числа диаграмм в локальном `storage/`;
    * прогон не состоялся — pytest вернул код вне {0, 1}, итоговой строки нет,
      или разобранных идентификаторов меньше, чем красных в счётчиках. Убитый
      прогон (`os._exit`, access violation §24.6) даёт ПУСТОЙ список красных,
      то есть без этих проверок читается как «всё починилось»;
    * массовое «позеленение» (> `MAX_FIXED`) и рост `skipped` на том же
      прогоне, где что-то позеленело (красный превратили в skip).
Единичный позеленевший тест провалом НЕ считается — печатается и требует
пересъёма базы.

Что считается провалом (`--write-baseline` → отказ, exit 1, пункт 1-26):
    * множество красных ВЫРОСЛО против текущей базы. Пересъём идёт отдельным
      коммитом (так требует Д6), поэтому красный флаг №4 протокола («эталон
      изменён тем же коммитом, что и код») его не видит: пока сверка жила
      только в `--check`, пересъём легализовал любой новый красный;
    * вывод неполон (счётчики против числа разобранных id) — на оборванном
      хвосте рост состава невидим.
Законны обе стороны: красные ушли и состав тот же. Первый снимок (базы в дереве
нет) сверять не с чем — он проходит, и стенд об этом говорит.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import corpus                                      # noqa: E402

BASELINE = REPO / "tools" / "bench" / "suite_baseline.json"

PYTEST_RUN = ["-q", "--tb=no", "-rEf", "-p", "no:cacheprovider"]
PYTEST_COLLECT = ["-q", "--collect-only", "-p", "no:cacheprovider"]

# Прогон вправе вернуть только «всё зелено» (0) или «есть упавшие» (1). 2 —
# прерван, 3 — внутренняя ошибка, 4 — ошибка вызова, 5 — не собрано ни одного
# теста, всё прочее (77 от os._exit, 0xC0000005 от access violation) — смерть
# процесса. Всё это раньше читалось как «красных нет».
RUN_RC_OK = (0, 1)
# Позеленело больше этого — не починка, а обрезанный прогон: настоящая починка
# такого объёма обязана пересъёмкой базы объяснить себя (Д6).
MAX_FIXED = 10

# Допуск на ТИХУЮ потерю тестов — у файлов, которые в диффе не менялись.
# Замер 2026-08-18 (пункт 0.3y, MEASUREMENTS §38): git-видимая часть — 855
# и здесь, и в CI, разница 0; 5 — запас на дребезг окружения, и он вчетверо
# уже 22 тестов раскладки, которые молча уходят без shapely (855 → 833).
# ⛔ Расширять этот допуск, чтобы «пропустить откат пункта», нельзя: пункты
# дороги приносят по 5–53 теста, и запас, перекрывающий откат, ослепил бы
# гейт ровно на ту величину (пункт 1-27). Откат считается отдельно — по
# карте файлов, см. shrinkage().
# До 0.3y запас был 18 и включал в себя корпус (пункт 0.8): пол ехал вверх от
# каждой новой диаграммы в storage/, и к 0.3x от него оставалось 2 теста.
FLOOR_MARGIN = 5

# Нежадно до « - » (после него причина) — идентификатор может содержать
# пробелы и кириллицу: в storage/ уже лежит «Новая папка», и параметр теста
# по корпусу приезжает в id как есть.
_RED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |\s*$)")
_TOTAL = re.compile(r"(\d+) (failed|passed|skipped|errors|error|xfailed|xpassed)")
_COLLECTED = re.compile(r"^(\d+) tests? collected")
# Хвостовой параметр идентификатора: `…::test_x[6e7144d5]`, у многопараметрных —
# `…[6e7144d5-case]`. uid8 шестнадцатеричный, дефиса внутри быть не может.
_PARAM_TAIL = re.compile(r"\[([^\[\]]+)\]\s*$")
# Строка сбора `--collect-only -q`: `tests/ui/test_x.py::TestY::test_z[param]`.
# Путь нежадно до первого `::` — дальше в идентификаторе бывает что угодно.
_TEST_ID = re.compile(r"^(.+?\.py)::")


def _pytest(args: list[str]) -> tuple[str, int]:
    # -X utf8 обязателен: без него режим кодировки решает консоль, а результат
    # набора от неё зависит. Замерено 2026-08-17: test_refactoring.py:697
    # читает файл с кириллицей через read_text() без encoding — под cp1251
    # это UnicodeDecodeError и красный тест, под UTF-8 тест зелёный. База
    # должна быть одна и та же на любой машине, поэтому режим задаём здесь.
    #
    # PYTEST_ADDOPTS у родителя выпалывается: `-x` или `-k` из окружения
    # обрезали бы прогон, а обрезанный прогон — это ложное «позеленело».
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        env=env,
    )
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def parse_red(text: str) -> set[str]:
    """Идентификаторы красных из хвоста `-rEf` (FAILED/ERROR ...)."""
    return {m.group(1) for line in text.splitlines() if (m := _RED.match(line))}


def parse_totals(text: str) -> dict[str, int]:
    """Счётчики из итоговой строки pytest («38 failed, 661 passed, ...»)."""
    tail = [ln for ln in text.splitlines() if " in " in ln and ("passed" in ln or "failed" in ln)]
    if not tail:
        return {}
    totals: dict[str, int] = {}
    for count, kind in _TOTAL.findall(tail[-1]):
        totals["errors" if kind == "error" else kind] = int(count)
    return totals


def parse_collected(text: str) -> int:
    """Число собранных тестов из хвоста `--collect-only -q`."""
    for line in text.splitlines():
        if m := _COLLECTED.match(line.strip()):
            return int(m.group(1))
    return -1


def local_corpus_uids() -> set[str]:
    """uid8 корпуса, которых нет в git: их параметры видит только эта машина."""
    return set(corpus.corpus_paths()) - set(corpus.corpus_paths(include_storage=False))


def split_collected(collect_text: str, local_uids: set[str]) -> tuple[dict[str, int], int]:
    """Разбор `--collect-only -q`: (git-видимые тесты по файлам, параметров по корпусу вне git).

    Набор обязан считаться одинаково на машине разработки и на раннере, иначе
    счёт ловит не потерю тестов, а число диаграмм в `storage/`. Поэтому та
    часть сбора, которой на чистом дереве не бывает, в карту файлов не идёт
    и считается отдельным числом.
    """
    per_file: dict[str, int] = {}
    local = 0
    for raw in collect_text.splitlines():
        line = raw.rstrip()
        m = _TEST_ID.match(line)
        if not m:
            continue
        tail = _PARAM_TAIL.search(line)
        if local_uids and tail and any(part in local_uids for part in tail.group(1).split("-")):
            local += 1
        else:
            per_file[m.group(1)] = per_file.get(m.group(1), 0) + 1
    return per_file, local


def count_local_corpus(collect_text: str, local_uids: set[str] | None = None) -> int:
    """Сколько собранных тестов параметризованы корпусом вне git."""
    uids = local_corpus_uids() if local_uids is None else local_uids
    return split_collected(collect_text, uids)[1]


def file_digest(path: Path) -> str:
    """Отпечаток файла с нормализованными переводами строк.

    Нормализация обязательна: у машины разработки checkout с CRLF, у раннера
    с LF. Без неё каждый файл выглядел бы изменённым, и пол ослеп бы в CI
    целиком — «изменённому» файлу потеря тестов прощается.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def tracked_by_git(paths: set[str]) -> tuple[set[str], str]:
    """Какие из исчезнувших файлов git всё ещё числит за деревом (+ ошибка git).

    Индекс — ответ самого git на вопрос «этот файл ещё часть дерева?».
    `git revert` пункта снимает файл вместе с записью о нём, а стёртый,
    переименованный или забытый мимо git в индексе остаётся: первое — откат,
    второе — тихая пропажа. Git не ответил — считаем числящимися всеми
    (осторожная сторона: потеря пойдёт в счёт) и говорим об этом вслух.
    """
    if not paths:
        return set(), ""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", *sorted(paths)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return set(paths), (proc.stderr or "").strip() or f"git ls-files вернул {proc.returncode}"
    return {p for p in proc.stdout.split("\0") if p}, ""


def shrinkage(base: dict, per_file: dict[str, int]) -> tuple[dict[str, int], dict[str, int], str]:
    """Разложить потерю тестов на тихую и объяснённую диффом.

    Гейт умеет судить ровно об одном: файл байт-в-байт тот же, что в базе,
    а тестов из него выходит меньше. В диффе такого не видно ничем — так
    уходят `importorskip` без установленной зависимости, `collect_ignore`,
    сломанный `conftest`, потерянная зависимость окружения.

    Всё остальное в диффе видно, и судит это ревизор, а не пол набора:
    файл правили (отпечаток разошёлся) или файл сняли вместе с записью о нём
    в git. Иначе штатный откат пункта — `git revert` по тегу, `PROTOCOL §6` —
    красил гейт законным возвратом: пункты дороги приносят по 5–53 теста
    при допуске 5 (пункт 1-27).

    Возвращает ({файл: тихо потеряно}, {файл: потеряно явно}, ошибка git).
    """
    base_per: dict[str, list] = base["per_file"]
    absent = {path for path in base_per if not (REPO / path).exists()}
    tracked, git_error = tracked_by_git(absent)

    silent: dict[str, int] = {}
    explained: dict[str, int] = {}
    for path, (was, digest) in base_per.items():
        delta = was - per_file.get(path, 0)
        if delta <= 0:
            continue
        if path in absent:
            # снят вместе с записью в git — откат; остался в индексе — пропажа
            (silent if path in tracked else explained)[path] = delta
        else:
            (silent if file_digest(REPO / path) == digest else explained)[path] = delta
    return silent, explained, git_error


def floor_problems(base: dict, per_file: dict[str, int]) -> tuple[list[str], list[str]]:
    """(причины провала по усыханию набора, что сказать вслух при зелёном).

    Пол один на двоих: второй его потребитель — сторож сбора
    `tests/test_collection_clean.py`. Считается здесь, чтобы они не разъехались.
    """
    if "per_file" not in base:                     # база снята до пункта 1-27
        git_visible = sum(per_file.values())
        if git_visible < base["min_collected"]:
            return ([
                f"набор усох: в git-видимой части {git_visible} < {base['min_collected']} "
                "(база без карты файлов — карта появится с ближайшим пересъёмом)"
            ], [])
        return ([], [])

    silent, explained, git_error = shrinkage(base, per_file)
    problems: list[str] = []
    if git_error:
        problems.append(f"git не ответил про исчезнувшие файлы ({git_error}) — об откате судить нечем")

    def _lines(where: dict[str, int]) -> str:
        return "\n".join(
            f"    {path}: {base['per_file'][path][0]} → {per_file.get(path, 0)}"
            for path in sorted(where)
        )

    lost = sum(silent.values())
    if lost > FLOOR_MARGIN:
        problems.append(
            f"набор усох: тихо потеряно {lost} тестов (допуск {FLOOR_MARGIN}) — "
            f"эти файлы в диффе не менялись, а тестов из них выходит меньше:\n{_lines(silent)}"
        )
    notes: list[str] = []
    if silent and not problems:
        notes.append(f"[в допуске] тихо потеряно {lost} из {FLOOR_MARGIN}:\n{_lines(silent)}")
    if explained:
        notes.append(
            f"[в диффе] {len(explained)} файлов эталона отдали меньше тестов "
            f"(−{sum(explained.values())}) — файл правили или сняли вместе с записью в git:\n"
            f"{_lines(explained)}"
        )
    return problems, notes


def compare(base_red: set[str], cur_red: set[str]) -> tuple[list[str], list[str]]:
    """(новые красные — это регрессия, позеленевшие — повод пересъёмки базы)."""
    return sorted(cur_red - base_red), sorted(base_red - cur_red)


def verdict(red: set[str], totals: dict[str, int], run_rc: int) -> list[str]:
    """Причины провала самого прогона (пустой список = прогон состоялся).

    Порядок проверок важен: сначала «прогон вообще состоялся», потом уже
    сравнение с базой. Убитый прогон даёт пустой список красных, и без
    этих проверок он читается как «всё починилось».

    Состав набора считает `floor_problems` — у него второй потребитель.
    """
    fail: list[str] = []

    if run_rc not in RUN_RC_OK:
        fail.append(
            f"прогон не завершился штатно: pytest вернул {run_rc} "
            f"(штатные — {RUN_RC_OK}: 0 всё зелено, 1 есть упавшие). "
            "Обрыв, смерть процесса или ошибка вызова — сравнивать не с чем"
        )
    if not totals:
        fail.append("не разобрал итоговую строку pytest — вывод оборван, счётчиков нет")
    else:
        counted = totals.get("failed", 0) + totals.get("errors", 0)
        if counted != len(red):
            fail.append(
                f"вывод неполон: в итоговой строке {counted} красных, "
                f"а идентификаторов разобрано {len(red)}"
            )
    return fail


def write_blocked(base: dict | None, red: set[str], totals: dict[str, int]) -> list[str]:
    """Причины отказать в пересъёме базы (пустой список = пересъём законен).

    Пересъём — единственный путь, которым состав красных вообще меняется, и по
    Д6 он идёт ОТДЕЛЬНЫМ коммитом; поэтому красный флаг №4 протокола («эталон
    изменён тем же коммитом, что и код») его не видит. До пункта 1-26 сверка с
    базой жила только в `--check`, а `--write-baseline` перезаписывал `red` тем,
    что красно сейчас, — то есть любой Д6-пересъём легализовал выросшее
    множество красных.

    Законны обе стороны: красные ушли (тесты починили) и состав тот же
    (пересъём ради `collected`/`totals` после новых зелёных тестов). Незаконен
    ровно рост: идентификатор, которого в базе нет.
    """
    fail: list[str] = []

    # Сверка на неполном списке ничего не значит: оборванный хвост даёт красных
    # меньше, чем их было, и рост состава становится невидим.
    counted = totals.get("failed", 0) + totals.get("errors", 0)
    if counted != len(red):
        fail.append(
            f"вывод неполон: в итоговой строке {counted} красных, а идентификаторов "
            f"разобрано {len(red)} — сверять состав с базой не на чем"
        )
    if base is None:
        return fail

    new, _ = compare(set(base["red"]), red)
    if new:
        fail.append(
            f"множество красных выросло против базы ({base['recorded']}): "
            f"{len(base['red'])} → {len(red)}, новых {len(new)}:\n"
            + "\n".join(f"    + {nid}" for nid in new)
            + "\n  Новый красный чинят или откатывают — пересъём его не легализует."
        )
    return fail


def skip_conversion_suspected(base: dict, totals: dict[str, int], fixed: list[str]) -> bool:
    """Красный превратили в skip — тест «позеленел», не будучи починенным.

    Порознь оба признака законны (тест починили; появился новый skip), вместе
    на одном прогоне — почти всегда конверсия. Разбирается пересъёмкой базы.
    """
    grew = totals.get("skipped", 0) - base["totals"].get("skipped", 0)
    return bool(fixed) and grew > 0


def _load_baseline() -> dict:
    if not BASELINE.exists():
        sys.exit(f"нет базы {BASELINE.relative_to(REPO)} — сначала --write-baseline")
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _measure() -> tuple[set[str], dict[str, int], int, dict[str, int], int, str, int]:
    run_text, run_rc = _pytest(PYTEST_RUN)
    collect_text, collect_rc = _pytest(PYTEST_COLLECT)
    if collect_rc != 0:
        sys.exit(f"сбор pytest сломан (exit {collect_rc}) — это пункт 0.0, а не база")
    collected = parse_collected(collect_text)
    per_file, local = split_collected(collect_text, local_corpus_uids())
    return parse_red(run_text), parse_totals(run_text), collected, per_file, local, run_text, run_rc


def cmd_write() -> int:
    red, totals, collected, per_file, local, run_text, run_rc = _measure()
    if run_rc not in RUN_RC_OK:
        sys.exit(f"прогон вернул {run_rc} — снимать базу с оборванного прогона нельзя:\n" + run_text[-2000:])
    if not totals:
        sys.exit("не разобрал итоговую строку pytest:\n" + run_text[-2000:])
    base = _load_baseline() if BASELINE.exists() else None
    if base is None:
        print(f"базы {BASELINE.name} нет — первый снимок, сверять не с чем")
    if blocked := write_blocked(base, red, totals):
        sys.exit("\n".join(f"[ОТКАЗ] {msg}" for msg in blocked))
    git_visible = sum(per_file.values())
    BASELINE.write_text(
        json.dumps(
            {
                "recorded": datetime.date.today().isoformat(),
                "command": "python -m pytest " + " ".join(PYTEST_RUN),
                "platform": f"{sys.platform} · CPython {platform.python_version()}",
                "env": "requirements/api.txt + ui.txt + dev.txt + shapely==2.1.2",
                "totals": totals,
                "collected": collected,
                "corpus_local": local,
                "collected_git_visible": git_visible,
                "min_collected": git_visible - FLOOR_MARGIN,
                # {файл: [сколько git-видимых тестов, отпечаток файла]} — по этой
                # карте `--check` отличает тихую пропажу от видимой в диффе.
                "per_file": {
                    path: [count, file_digest(REPO / path)]
                    for path, count in sorted(per_file.items())
                },
                "red": sorted(red),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"база записана: {len(red)} красных, собрано {collected} "
          f"(git-видимых {git_visible} в {len(per_file)} файлах, корпус вне git {local}), {totals}")
    return 0


def cmd_check() -> int:
    base = _load_baseline()
    red, totals, collected, per_file, local, run_text, run_rc = _measure()
    new, fixed = compare(set(base["red"]), red)
    git_visible = sum(per_file.values())

    print(f"сейчас:  собрано {collected} (git-видимых {git_visible} в {len(per_file)} файлах, "
          f"корпус вне git {local}), {totals}, pytest exit {run_rc}")
    print(f"база ({base['recorded']}, {base['platform']}): собрано {base['collected']}, "
          f"git-видимых {base.get('collected_git_visible', '?')} "
          f"в {len(base.get('per_file') or ())} файлах, {base['totals']}")

    problems = verdict(red, totals, run_rc)
    floor, notes = floor_problems(base, per_file)
    problems += floor
    for note in notes:
        print(f"\n{note}")
    if len(fixed) > MAX_FIXED:
        problems.append(
            f"позеленело сразу {len(fixed)} тестов (порог {MAX_FIXED}) — так выглядит "
            "обрезанный прогон; если починка настоящая, пересними базу и объясни её"
        )
    if skip_conversion_suspected(base, totals, fixed):
        problems.append(
            f"skipped вырос ({base['totals'].get('skipped', 0)} → {totals.get('skipped', 0)}) "
            "на том же прогоне, где что-то позеленело: похоже, красный тест превратили в skip, "
            "а не починили"
        )

    bad = bool(problems)
    for msg in problems:
        print(f"\n[ПРОВАЛ] {msg}")
    if new:
        print(f"\n[ПРОВАЛ] новые красные ({len(new)}) — их не было в базе:")
        for nid in new:
            print(f"  + {nid}")
        bad = True
    if fixed:
        print(f"\n[позеленело] {len(fixed)} — пересними базу отдельным коммитом (--write-baseline):")
        for nid in fixed:
            print(f"  - {nid}")
    if not bad:
        print("\n[OK] новых красных нет")
    else:
        print("\nхвост прогона:\n" + "\n".join(run_text.splitlines()[-25:]))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="сверить прогон с базой, exit 1 при регрессии")
    g.add_argument("--write-baseline", action="store_true", help="пересъём базы")
    args = ap.parse_args()
    return cmd_write() if args.write_baseline else cmd_check()


if __name__ == "__main__":
    sys.exit(main())
