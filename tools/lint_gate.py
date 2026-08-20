# -*- coding: utf-8 -*-
"""lint_gate.py — храповик линтеров (ПР5, пункт 0.10 дороги рефакторинга).

Зачем. Правило `ruff` на широкий `except` включено (`pyproject.toml`), но
долг в 200 мест никто не оплатит одним коммитом, а «голый» `ruff check`
на нём красен всегда — то есть бесполезен как гейт. Судит этот стенд:
счётчик нарушений ПО ФАЙЛАМ не имеет права расти. Новый широкий `except` в
чистом файле — провал; новый в грязном — тоже провал (счётчик файла вырос).
Оплата долга (счётчик упал) печатается и гейт не валит.

Второй судья — `mypy` на списке `files` из `pyproject.toml`: там ноль ошибок
не эталон, а требование. Список растёт по включению, автором нового модуля.

Контракт — как у `suite_baseline.py` и `edit_bench.py`:
    python -X utf8 tools/lint_gate.py                  # отчёт
    python -X utf8 tools/lint_gate.py --check          # exit 1 при росте долга
    python -X utf8 tools/lint_gate.py --write-baseline # переснять (Д6)

Гейт валят также: код возврата линтера вне штатного набора, неразбираемый
вывод и пустой результат при непустом эталоне — убитый прогон не имеет права
выглядеть зелёным (урок пункта 0.3).

Три исхода (`PROTOCOL §5`) на ОБОИХ путях: 0 — доказано (эталон переснят /
долг не вырос), 1 — опровергнуто (долг вырос; на записи это ОТКАЗ), 2 —
СУДИТЬ НЕЧЕМ. См. `write_blocked()`.

⛔ «Судить нечем» — это и убитый прогон линтера (пункт 1-30): `ruff`/`mypy`
с кодом вне `{0, 1}`, неразбираемый вывод, молчащий `git ls-files`. До 1-30
такой прогон умирал `RuntimeError` — трейсбеком и кодом 1, то есть тем же
кодом, что доказанный рост долга. Полярность была безопасная (громко и
красным), но перепутанная: тут чинят обстановку, а не код.

⛔ Пункт GATE-8 доделал ту же полярность на пути ЧТЕНИЯ: пустой счёт при
непустом эталоне — тоже убитый прогон, и `--check` печатал по нему `[ПРОВАЛ]`
и отдавал 1, тогда как `--write-baseline` на ТОМ ЖЕ условии говорил «судить
нечем» и отдавал 2. Условие теперь одно на оба пути (`measurement_dead()`)
и стоит ПЕРВЫМ: на мёртвом прогоне сравнивать не с чем.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "tools" / "bench" / "lint_baseline.json"

# Три исхода `PROTOCOL §5`. На пути записи 1 читается как ОТКАЗ.
EXIT_REFUTED = 1
EXIT_UNJUDGED = 2


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)          # родительские ключи детям не наследуем
    return subprocess.run([sys.executable, "-X", "utf8", "-m", *args],
                          cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def tracked() -> set[str]:
    """Файлы под git — только они могут быть в эталоне.

    Иначе счётчик зависит от мусора в рабочем дереве: замерено 2026-08-18 —
    локально 200 нарушений в 74 файлах, на раннере 199 в 73, разница ровно
    в одном нетрекнутом `tools/tz_lint.py`. Хуже того, его строка в эталоне
    заранее прощала бы долг файлу, которого в git ещё нет.
    """
    try:
        proc = subprocess.run(["git", "ls-files", "*.py"], cwd=str(REPO),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    except OSError as exc:                  # git не установлен или недоступен
        raise RuntimeError(f"git ls-files не запустился: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files вернул {proc.returncode}: "
                           f"{(proc.stderr or '').strip()[-200:]}")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def ruff_counts() -> dict[str, int]:
    """{путь от корня репо: сколько нарушений}. RuntimeError на убитом прогоне."""
    proc = _run(["ruff", "check", "--output-format", "json"])
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ruff вернул {proc.returncode}, штатные (0, 1): "
                           f"{(proc.stderr or '').strip()[-400:]}")
    try:
        found = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"вывод ruff не разбирается: {exc}; "
                           f"хвост: {proc.stdout[-200:]!r}") from exc
    under_git = tracked()
    counts: dict[str, int] = {}
    for item in found:
        rel = Path(item["filename"]).resolve().relative_to(REPO).as_posix()
        if rel not in under_git:
            continue
        counts[rel] = counts.get(rel, 0) + 1
    return counts


def mypy_errors() -> tuple[int, str]:
    """(число ошибок, хвост вывода) по списку `files` из pyproject."""
    proc = _run(["mypy"])
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"mypy вернул {proc.returncode}, штатные (0, 1): "
                           f"{(proc.stderr or '').strip()[-400:]}")
    out = proc.stdout or ""
    errors = sum(1 for line in out.splitlines() if ": error:" in line)
    return errors, out.strip()[-800:]


def debt_grown(counts: dict[str, int],
               known: dict[str, int]) -> dict[str, tuple[int, int]]:
    """{файл: (было, стало)} там, где долг вырос.

    Арифметика общая у `--check` и у пересъёма (пункт 1-28): два судьи одного
    долга не должны разъехаться — ровно тот урок, что и у пола набора в
    `suite_baseline.floor_problems`.
    """
    return {f: (known.get(f, 0), n) for f, n in counts.items()
            if n > known.get(f, 0)}


def measurement_dead(counts: dict[str, int], base: dict) -> str | None:
    """Почему замер ruff не годен, или None — замер состоялся.

    Ноль нарушений при непустом эталоне — это не долг в 174 места, оплаченный
    между двумя прогонами, а убитый ruff: пустой вывод, сорванный разбор,
    потерянный конфиг. Арифметика ОДНА на оба пути (пункт GATE-8): до него это
    условие жило в двух местах и два судьи одного стенда разошлись формой —
    `write_blocked()` называл его «судить нечем» и отдавал 2, а `verdict()`
    печатал `[ПРОВАЛ]` и отдавал 1, то есть тот же код, что доказанный рост
    долга. Тот же урок, что у `debt_grown` и у пола набора в
    `suite_baseline.floor_problems`.
    """
    was = base.get("ruff", {}).get("total")
    if was and not counts:
        return (f"ноль нарушений при непустом эталоне ({was}) — прогон ruff убит: "
                "долг такого размера не оплачивается между двумя прогонами")
    return None


def verdict(counts: dict[str, int], mypy_bad: int,
            base: dict) -> tuple[int, list[str]]:
    """-> (код вердикта, строки отчёта). Отделено от печати ради теста.

    Три исхода (`PROTOCOL §5`), а не два — как на пути записи: 0 — долг не
    вырос, 1 — опровергнуто (вырос долг ruff или грязен mypy), 2 — СУДИТЬ
    НЕЧЕМ. ⛔ Ветка «замер не состоялся» стоит ПЕРВОЙ и КОРОТИТ вердикт
    (пункт GATE-8): на мёртвом прогоне сравнивать не с чем, и `[долг оплачен]`
    по каждому файлу эталона был бы не наблюдением, а следом обрыва.
    """
    known = base.get("ruff", {}).get("per_file", {})
    grown = debt_grown(counts, known)
    paid = {f: (n, counts.get(f, 0)) for f, n in known.items()
            if counts.get(f, 0) < n}

    total, was = sum(counts.values()), base.get("ruff", {}).get("total")
    lines = [f"ruff {'/'.join(base.get('ruff', {}).get('select', ['?']))}: "
             f"{total} нарушений в {len(counts)} файлах"
             + (f" (эталон {was})" if was is not None else "")]
    dead = measurement_dead(counts, base)
    if dead:
        lines.append(f"[СУДИТЬ НЕЧЕМ] {dead}")
        return EXIT_UNJUDGED, lines
    for f, (before, now) in sorted(grown.items()):
        lines.append(f"[ПРОВАЛ] {f}: широких except {before} -> {now}")
    for f, (before, now) in sorted(paid.items()):
        lines.append(f"[долг оплачен] {f}: {before} -> {now}")
    lines.append(f"mypy (список files в pyproject): ошибок {mypy_bad}")
    if mypy_bad:
        lines.append("[ПРОВАЛ] mypy на своём списке обязан быть чистым")
    if not grown and not mypy_bad:
        lines.append("[OK] долг не вырос")
    return (EXIT_REFUTED if grown or mypy_bad else 0), lines


def write_blocked(counts: dict[str, int],
                  base: dict) -> tuple[list[str], list[str]]:
    """-> (причины «судить нечем», причины отказа). Обе пустые = пересъём законен.

    Дыра пункта 1-28: `--write-baseline` не сверялся с эталоном ВОВСЕ —
    `read_baseline()` звался только в ветке `--check`. Замер 2026-08-19:
    широкий `except` в чистом `tools/corpus.py` → `--check` exit 1; один
    пересъём (178 → 179, exit 0) → `--check` снова зелёный. Пересъём идёт
    отдельным коммитом, как требует Д6, поэтому красный флаг №4 протокола
    («эталон изменён тем же коммитом, что и код») этого не видит.

    mypy сюда не входит намеренно: в файл эталона пишется только счёт ruff,
    и грязный mypy пересъёмом не легализуется — его судит `--check` на каждом
    прогоне. Блокировать им запись значило бы судить о том, чего не пишем.
    """
    ruff = base.get("ruff", {})
    if not ruff:                                    # первый снимок
        return [], []
    dead = measurement_dead(counts, base)           # арифметика общая (GATE-8)
    if dead:
        return [f"{dead}, а пересъём записал бы пустой долг"], []
    grown = debt_grown(counts, ruff.get("per_file", {}))
    if grown:
        return [], ["долг широких except вырос против эталона — пересъём его "
                    "узаконит:\n"
                    + "\n".join(f"    {f}: {was} -> {now}"
                                for f, (was, now) in sorted(grown.items()))
                    + "\n  Новый широкий except чинят или откатывают."]
    return [], []


def read_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="сверить с эталоном; exit 1 при росте долга")
    ap.add_argument("--write-baseline", action="store_true",
                    help="переснять эталон (отдельным коммитом, Д6)")
    args = ap.parse_args()

    try:
        counts = ruff_counts()
        mypy_bad, mypy_tail = mypy_errors()
    except RuntimeError as exc:
        # Убитый прогон линтера или молчащий git — сломана обстановка, а не код
        # (пункт 1-30). Раньше это был трейсбек и код 1, неотличимый от роста долга.
        print(f"[СУДИТЬ НЕЧЕМ] замер линтеров не состоялся: {exc}")
        return EXIT_UNJUDGED

    if args.write_baseline:
        unjudged, refused = write_blocked(counts, read_baseline())
        for msg in unjudged:
            print(f"[СУДИТЬ НЕЧЕМ] {msg}")
        for msg in refused:
            print(f"[ОТКАЗ] {msg}")
        if refused:                      # доказанный рост долга сильнее неполноты
            return EXIT_REFUTED
        if unjudged:
            return EXIT_UNJUDGED
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"ruff": {"select": ["BLE001", "E722"],
                      "total": sum(counts.values()),
                      "per_file": dict(sorted(counts.items()))}},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"эталон переснят: {BASELINE} ({sum(counts.values())} нарушений)")
        return 0

    code, lines = verdict(counts, mypy_bad, read_baseline())
    print("\n".join(lines))
    if mypy_bad and mypy_tail:
        print(mypy_tail)
    if not args.check:            # голый отчёт гейтом не является — всегда 0
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
