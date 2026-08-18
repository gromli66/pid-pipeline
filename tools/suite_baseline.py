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
    * усыхание набора — собрано меньше `min_collected` (тесты молча исчезли:
      `importorskip`, `collect_ignore`, снесённый файл);
    * прогон не состоялся — pytest вернул код вне {0, 1}, итоговой строки нет,
      или разобранных идентификаторов меньше, чем красных в счётчиках. Убитый
      прогон (`os._exit`, access violation §24.6) даёт ПУСТОЙ список красных,
      то есть без этих проверок читается как «всё починилось»;
    * массовое «позеленение» (> `MAX_FIXED`) и рост `skipped` на том же
      прогоне, где что-то позеленело (красный превратили в skip).
Единичный позеленевший тест провалом НЕ считается — печатается и требует
пересъёма базы.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
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

# Запас пола к числу собранных. База снимается на машине разработки, а
# проверяется и на раннере, где корпусных параметров меньше, — запас обязан
# перекрывать эту разницу и быть уже самой мелкой потери, которую ловим.
# Замер 2026-08-18 (пункт 0.8, MEASUREMENTS §31): локально 775, в чистом клоне
# 761, разница 14 (17 графов корпуса против 3 в git). 14 + 4 запаса = 18, и это
# уже ниже 22 тестов раскладки, которые молча уходят без shapely.
FLOOR_MARGIN = 18

# Нежадно до « - » (после него причина) — идентификатор может содержать
# пробелы и кириллицу: в storage/ уже лежит «Новая папка», и параметр теста
# по корпусу приезжает в id как есть.
_RED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |\s*$)")
_TOTAL = re.compile(r"(\d+) (failed|passed|skipped|errors|error|xfailed|xpassed)")
_COLLECTED = re.compile(r"^(\d+) tests? collected")


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


def compare(base_red: set[str], cur_red: set[str]) -> tuple[list[str], list[str]]:
    """(новые красные — это регрессия, позеленевшие — повод пересъёмки базы)."""
    return sorted(cur_red - base_red), sorted(base_red - cur_red)


def verdict(base: dict, red: set[str], totals: dict[str, int], collected: int, run_rc: int) -> list[str]:
    """Причины провала (пустой список = гейт зелёный).

    Порядок проверок важен: сначала «прогон вообще состоялся», потом уже
    сравнение с базой. Убитый прогон даёт пустой список красных, и без
    этих проверок он читается как «всё починилось».
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
    if collected < base["min_collected"]:
        fail.append(f"набор усох: собрано {collected} < {base['min_collected']}")
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


def _measure() -> tuple[set[str], dict[str, int], int, str, int]:
    run_text, run_rc = _pytest(PYTEST_RUN)
    collect_text, collect_rc = _pytest(PYTEST_COLLECT)
    if collect_rc != 0:
        sys.exit(f"сбор pytest сломан (exit {collect_rc}) — это пункт 0.0, а не база")
    return parse_red(run_text), parse_totals(run_text), parse_collected(collect_text), run_text, run_rc


def cmd_write() -> int:
    red, totals, collected, run_text, run_rc = _measure()
    if run_rc not in RUN_RC_OK:
        sys.exit(f"прогон вернул {run_rc} — снимать базу с оборванного прогона нельзя:\n" + run_text[-2000:])
    if not totals:
        sys.exit("не разобрал итоговую строку pytest:\n" + run_text[-2000:])
    BASELINE.write_text(
        json.dumps(
            {
                "recorded": datetime.date.today().isoformat(),
                "command": "python -m pytest " + " ".join(PYTEST_RUN),
                "platform": f"{sys.platform} · CPython {platform.python_version()}",
                "env": "requirements/api.txt + ui.txt + dev.txt + shapely==2.1.2",
                "totals": totals,
                "collected": collected,
                "min_collected": collected - FLOOR_MARGIN,
                "red": sorted(red),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"база записана: {len(red)} красных, собрано {collected}, {totals}")
    return 0


def cmd_check() -> int:
    base = _load_baseline()
    red, totals, collected, run_text, run_rc = _measure()
    new, fixed = compare(set(base["red"]), red)

    print(f"сейчас:  собрано {collected}, {totals}, pytest exit {run_rc}")
    print(f"база ({base['recorded']}, {base['platform']}): собрано {base['collected']}, {base['totals']}")

    problems = verdict(base, red, totals, collected, run_rc)
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
