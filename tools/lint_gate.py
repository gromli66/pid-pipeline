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


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)          # родительские ключи детям не наследуем
    return subprocess.run([sys.executable, "-X", "utf8", "-m", *args],
                          cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


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
    counts: dict[str, int] = {}
    for item in found:
        rel = Path(item["filename"]).resolve().relative_to(REPO).as_posix()
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


def verdict(counts: dict[str, int], mypy_bad: int,
            base: dict) -> tuple[bool, list[str]]:
    """-> (гейт пройден, строки отчёта). Отделено от печати ради теста."""
    known = base.get("ruff", {}).get("per_file", {})
    grown = {f: (known.get(f, 0), n) for f, n in counts.items()
             if n > known.get(f, 0)}
    paid = {f: (n, counts.get(f, 0)) for f, n in known.items()
            if counts.get(f, 0) < n}

    total, was = sum(counts.values()), base.get("ruff", {}).get("total")
    lines = [f"ruff {'/'.join(base.get('ruff', {}).get('select', ['?']))}: "
             f"{total} нарушений в {len(counts)} файлах"
             + (f" (эталон {was})" if was is not None else "")]
    if was and not counts:
        lines.append("[ПРОВАЛ] ноль нарушений при непустом эталоне — прогон убит")
        return False, lines
    for f, (before, now) in sorted(grown.items()):
        lines.append(f"[ПРОВАЛ] {f}: широких except {before} -> {now}")
    for f, (before, now) in sorted(paid.items()):
        lines.append(f"[долг оплачен] {f}: {before} -> {now}")
    lines.append(f"mypy (список files в pyproject): ошибок {mypy_bad}")
    if mypy_bad:
        lines.append("[ПРОВАЛ] mypy на своём списке обязан быть чистым")
    if not grown and not mypy_bad:
        lines.append("[OK] долг не вырос")
    return (not grown and not mypy_bad), lines


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

    counts = ruff_counts()
    mypy_bad, mypy_tail = mypy_errors()

    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"ruff": {"select": ["BLE001", "E722"],
                      "total": sum(counts.values()),
                      "per_file": dict(sorted(counts.items()))}},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"эталон переснят: {BASELINE} ({sum(counts.values())} нарушений)")
        return 0

    ok, lines = verdict(counts, mypy_bad, read_baseline())
    print("\n".join(lines))
    if mypy_bad and mypy_tail:
        print(mypy_tail)
    if not args.check:
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
