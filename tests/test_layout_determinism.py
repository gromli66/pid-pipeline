# -*- coding: utf-8 -*-
"""Стенд воспроизводимости раскладки (пункт 0.10, ПР1).

Дефект уровня стенда: гейт, зелёный на графе, который перестал быть
воспроизводимым, — именно такой молчаливый регресс и делает `cmp_bitexact`
ложно-красным. Плюс убитый дочерний прогон не имеет права выглядеть удачным.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import layout_determinism as det  # noqa: E402

BASE = {"runs": 6, "routing": True,
        "stable": {"aaaaaaaa": True, "bbbbbbbb": False}}


def _lines(result, base=BASE):
    ok, lines = det.verdict(result, base)
    return ok, "\n".join(lines)


def test_stability_needs_all_runs_equal():
    assert det.stability(["x", "x", "x"])
    assert not det.stability(["x", "y", "x"])


def test_known_stable_graph_that_broke_fails():
    ok, text = _lines({"aaaaaaaa": ["x", "y"], "bbbbbbbb": ["z", "z"]})
    assert not ok
    assert "перестали воспроизводиться: aaaaaaaa" in text


def test_known_unstable_graph_stays_green():
    """Неповторимость роутинга записана в эталон — гейт на ней не валится."""
    ok, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "w"]})
    assert ok
    assert "НЕПОВТОРИМ" in text


def test_unstable_graph_that_settled_is_reported_but_green():
    ok, text = _lines({"aaaaaaaa": ["x", "x"], "bbbbbbbb": ["z", "z"]})
    assert ok
    assert "воспроизводятся впервые: bbbbbbbb" in text


def test_new_unstable_graph_fails():
    ok, text = _lines({"aaaaaaaa": ["x", "x"], "cccccccc": ["p", "q"]})
    assert not ok
    assert "новый граф корпуса неповторим: cccccccc" in text


def test_baseline_in_git_covers_the_fixtures():
    """Три графа фикстуры (то, что видит CI) обязаны быть в эталоне."""
    from tools import corpus

    base = json.loads(det.BASELINE.read_text(encoding="utf-8"))
    for uid8 in corpus.fixture_paths():
        assert uid8 in base["stable"], f"граф {uid8} не замерен стендом"


@pytest.mark.parametrize("stdout,rc,match", [
    ("", 77, "вернул 77"),
    ("не json", 0, "не дал разбираемого вывода"),
    ('{"aaaaaaaa": "x"}', 0, "потерял графы"),
])
def test_killed_child_is_loud(monkeypatch, stdout, rc, match):
    class _Proc:
        returncode = rc
        stderr = "boom"

    _Proc.stdout = stdout
    monkeypatch.setattr(det.subprocess, "run", lambda *a, **kw: _Proc())
    with pytest.raises(RuntimeError, match=match):
        det.run_pass(["aaaaaaaa", "bbbbbbbb"], True, 1)
