# -*- coding: utf-8 -*-
"""Судья вкладки «Ручная правка» как ГЕЙТ (пункт ГЕЙТ-1).

Дефект уровня стенда: корпус лежит вне git (`.gitignore:37`), поэтому на
чистом дереве мерить нечего — а `--check` печатал «рост дефектов: 0» и
отдавал exit 0. Зелёный, снятый там, где судить нечем, хуже красного
(`PROTOCOL §5`: доказано · опровергнуто · судить нечем).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import edit_bench as eb  # noqa: E402

BASE = {"a.json": {"diag": 3, "through": 1, "max_dev": 10},
        "b.json": {"diag": 0, "through": 0, "max_dev": 0}}


def _rows(counts_by_name: dict) -> dict:
    return {name: {"counts": c} for name, c in counts_by_name.items()}


def test_no_growth_is_green():
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1, "max_dev": 99},
                              "b.json": {"diag": 0, "through": 0}}), BASE)
    assert code == 0


def test_growth_of_defect_column_is_red():
    code = eb._compare(_rows({"a.json": {"diag": 4, "through": 1},
                              "b.json": {"diag": 0, "through": 0}}), BASE)
    assert code == 1


def test_file_absent_from_baseline_is_skipped():
    """Новый холст в корпусе судить не с чем — это пропуск, а не провал."""
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1},
                              "b.json": {"diag": 0, "through": 0},
                              "new.json": {"diag": 9, "through": 9}}), BASE)
    assert code == 0


def test_unmeasured_baseline_file_is_not_green(capsys):
    """Дыра ГЕЙТ-1: замерена часть корпуса — это НЕ «дефекты не выросли»."""
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1}}), BASE)
    assert code == 2
    out = capsys.readouterr().out
    assert "судить нечем" in out
    assert "b.json" in out


def test_empty_baseline_is_not_green(capsys):
    code = eb._compare(_rows({"a.json": {"diag": 3, "through": 1}}), {})
    assert code == 2
    assert "судить нечем" in capsys.readouterr().out


def test_growth_beats_unmeasured():
    """Доказанный рост дефектов важнее неполноты: 1 (опровергнуто), не 2."""
    code = eb._compare(_rows({"a.json": {"diag": 4, "through": 1}}), BASE)
    assert code == 1


def test_empty_corpus_says_nothing_to_judge(monkeypatch, capsys):
    """Зонд «убрать корпус»: `--all --check` на пустом корпусе — exit 2."""
    monkeypatch.setattr(eb, "CORPUS", REPO / "tools" / "bench" / "no_such_corpus")
    monkeypatch.setattr(sys, "argv", ["edit_bench.py", "--all", "--check"])
    assert eb.main() == 2
    assert "судить нечем" in capsys.readouterr().out
