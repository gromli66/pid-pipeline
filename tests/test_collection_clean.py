# -*- coding: utf-8 -*-
"""Сборка pytest обязана проходить без ошибок (пункт 0.0 дороги рефакторинга).

Гейт всего плана — «зелёный базовый набор». Пока сборка обрывается, набора не
существует: pytest импортирует ВСЕ тест-модули до запуска первого теста, и один
модуль, испортивший sys.modules на уровне модуля, роняет сборку всех следующих
за ним по алфавиту (так пункт 0.0 и появился: заглушка Qt из
tests/test_stage7_graph_flow.py убивала tests/ui/test_bundled_fonts.py и
tests/ui/test_frame_crop_preview.py).

Отдельным процессом — иначе повторный сбор внутри текущей сессии не покажет
порчу, которую сессия уже нанесла. Рекурсии нет: дочерний pytest только
собирает, тестов не исполняет.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE = PROJECT_ROOT / "tools" / "bench" / "suite_baseline.json"


def _collect():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    return proc


def test_pytest_collect_only_is_clean():
    proc = _collect()
    tail = "\n".join((proc.stdout or "").splitlines()[-40:])
    assert proc.returncode == 0, (
        f"pytest --collect-only вернул {proc.returncode}; хвост вывода:\n{tail}"
    )


def test_suite_does_not_shrink():
    """Тихое усыхание набора: exit-код сбора нулевой, а тестов стало меньше.

    Так уходят целые файлы — `importorskip` без установленной зависимости,
    `collect_ignore`, снесённый модуль. Нижняя граница — из базовой линии
    набора (`min_collected`, пункт 0.3), в ней уже заложен запас на
    необязательные зависимости.
    """
    assert BASELINE.exists(), f"нет базовой линии {BASELINE}: python tools/suite_baseline.py --write-baseline"
    floor = json.loads(BASELINE.read_text(encoding="utf-8"))["min_collected"]

    out = _collect().stdout or ""
    m = re.search(r"^(\d+) tests? collected", out, re.MULTILINE)
    assert m, f"не нашёл число собранных в выводе:\n{out[-1000:]}"
    collected = int(m.group(1))
    assert collected >= floor, (
        f"собрано {collected} < {floor} — набор усох; если тесты убраны намеренно, "
        f"пересними базу: python tools/suite_baseline.py --write-baseline"
    )
