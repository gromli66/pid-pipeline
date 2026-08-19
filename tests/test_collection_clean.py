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
import subprocess
import sys
from pathlib import Path

import pytest

from tools import suite_baseline

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
    `collect_ignore`, сломанный `conftest`. Считается по карте файлов из
    базовой линии (пункт 1-27): тесты, пропавшие вместе со своим файлом или
    у файла, который правили, видно в диффе — это не усыхание, иначе штатный
    `git revert` пункта красил бы набор законным откатом.

    В счёт идёт git-видимая часть сбора (пункт 0.3y): параметры по корпусу из
    локального `storage/` на чистом дереве не собираются, и если бы они входили
    в счёт, пол зависел бы от числа диаграмм на машине. Арифметика общая со
    стендом, чтобы три потребителя одного пола не разъехались.

    ⛔ Третий исход — `skip` (пункт 1-30): git не ответил про исчезнувшие
    файлы, и откат пункта от тихой пропажи отличить нечем. Красным тут была бы
    ложь про код там, где сломана обстановка; «судить нечем» у теста и есть
    пропуск с громкой причиной.
    """
    assert BASELINE.exists(), f"нет базовой линии {BASELINE}: python tools/suite_baseline.py --write-baseline"
    base = json.loads(BASELINE.read_text(encoding="utf-8"))

    out = _collect().stdout or ""
    per_file, _ = suite_baseline.split_collected(out, suite_baseline.local_corpus_uids())
    problems, _, unjudged = suite_baseline.floor_problems(base, per_file)
    if unjudged and not problems:
        pytest.skip("судить нечем: " + "\n".join(unjudged))
    assert not problems, (
        "\n".join(problems)
        + "\n  если тесты убраны намеренно, пересними базу: "
        "python tools/suite_baseline.py --write-baseline"
    )
