# -*- coding: utf-8 -*-
"""Храповик линтеров (пункт 0.10): вердикт проверяется тестом, а не глазами.

Дефект уровня стенда, ради которого тесты и написаны, — гейт, зелёный там, где
долг вырос: новый широкий `except` в уже грязном файле, приписка к чистому,
и убитый прогон линтера с пустым выводом (тот же класс, что блокер пункта 0.3).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import lint_gate  # noqa: E402

BASE = {"ruff": {"select": ["BLE001", "E722"], "total": 5,
                 "per_file": {"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2}}}


def _lines(counts, mypy_bad=0, base=BASE):
    ok, lines = lint_gate.verdict(counts, mypy_bad, base)
    return ok, "\n".join(lines)


def test_no_growth_is_green():
    ok, text = _lines({"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2})
    assert ok
    assert "[OK]" in text


def test_growth_in_dirty_file_fails():
    """Главный сценарий: файл уже в долгу, туда дописали ещё один except."""
    ok, text = _lines({"app/api/validation.py": 4, "ui/tabs/frame_tab.py": 2})
    assert not ok
    assert "app/api/validation.py: широких except 3 -> 4" in text


def test_first_violation_in_clean_file_fails():
    ok, text = _lines({"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2,
                       "worker/tasks/graph.py": 1})
    assert not ok
    assert "worker/tasks/graph.py: широких except 0 -> 1" in text


def test_paid_debt_is_reported_but_green():
    ok, text = _lines({"app/api/validation.py": 1, "ui/tabs/frame_tab.py": 2})
    assert ok
    assert "[долг оплачен] app/api/validation.py: 3 -> 1" in text


def test_empty_result_on_nonempty_baseline_is_a_failure():
    """Убитый линтер не имеет права выглядеть оплаченным долгом."""
    ok, text = _lines({})
    assert not ok
    assert "прогон убит" in text


def test_mypy_errors_fail_the_gate():
    ok, text = _lines({"app/api/validation.py": 3, "ui/tabs/frame_tab.py": 2},
                      mypy_bad=2)
    assert not ok
    assert "mypy" in text


def test_baseline_in_git_matches_configured_rules():
    """Эталон снят тем же набором правил, что стоит в pyproject.toml."""
    base = json.loads(lint_gate.BASELINE.read_text(encoding="utf-8"))
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for code in base["ruff"]["select"]:
        assert f'"{code}"' in text, f"правило {code} есть в эталоне, но не в конфиге"
    assert base["ruff"]["total"] == sum(base["ruff"]["per_file"].values())


@pytest.mark.parametrize("rc", [2, 77])
def test_abnormal_linter_exit_is_loud(monkeypatch, rc):
    """Код возврата вне (0, 1) — исключение, а не тихий ноль нарушений."""
    class _Proc:
        returncode = rc
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(lint_gate, "_run", lambda args: _Proc())
    with pytest.raises(RuntimeError, match=str(rc)):
        lint_gate.ruff_counts()


def test_baseline_holds_only_tracked_files():
    """Мусор рабочего дерева в эталон не попадает.

    Замер 2026-08-18: нетрекнутый `tools/tz_lint.py` давал 200/74 локально
    против 199/73 на раннере, а его строка в эталоне заранее прощала бы долг
    файлу, которого в git ещё нет.
    """
    base = json.loads(lint_gate.BASELINE.read_text(encoding="utf-8"))
    under_git = lint_gate.tracked()
    stray = sorted(set(base["ruff"]["per_file"]) - under_git)
    assert not stray, f"в эталоне файлы вне git: {stray}"
