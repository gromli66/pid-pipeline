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
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_pytest_collect_only_is_clean():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    tail = "\n".join((proc.stdout or "").splitlines()[-40:])
    assert proc.returncode == 0, (
        f"pytest --collect-only вернул {proc.returncode}; хвост вывода:\n{tail}"
    )
