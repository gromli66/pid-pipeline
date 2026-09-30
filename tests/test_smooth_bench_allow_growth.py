# -*- coding: utf-8 -*-
"""Ключ `smooth_bench --allow-growth "<основание>"` (решение Максима 30.09).

Без ключа пересъём при росте дефектной колонки — ОТКАЗ (1), эталон не
пишется. С ключом рост узаконивается явно: основание и поимённый перечень
выросших строк уходят в историю `allowed_growth` эталона. Пустое основание —
ошибка вызова (2) ДО замера. Qt и libavoid не нужны: судится `_write` и
разбор аргументов.
"""
from __future__ import annotations

import json
import sys

import pytest

from tools import smooth_bench as sb


def _cell(v):
    return {"med": v, "lo": v, "hi": v}


def _base(history=None) -> dict:
    b = {"version": sb.BASELINE_VERSION, "runs": 5, "populations": {
        "edited@model": {"files": {"a.json": {"bends": _cell(29),
                                              "steps": _cell(3)}},
                         "inputs": {"a.json": "fp-a"}},
        "raw": {"files": {"r.json": {"bends": _cell(7)}},
                "inputs": {"r.json": "fp-r"}},
    }}
    if history is not None:
        b["allowed_growth"] = history
    return b


def _out(bends=30, steps=3) -> dict:
    return {"edited@model": {
        "summary": {"a.json": {"bends": _cell(bends), "steps": _cell(steps)}},
        "detail": {}, "inputs": {"a.json": "fp-a"}}}


def _write(base, out, allow=None, unjudged=()):
    return sb._write(out, base, base["populations"], list(unjudged), 5, allow)


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    p = tmp_path / "smooth_baseline.json"
    monkeypatch.setattr(sb, "BASELINE", p)
    monkeypatch.setattr(sb, "REPO", tmp_path)      # строка «заморожена -> …»
    return p


def test_growth_without_key_is_refused(baseline, capsys):
    assert _write(_base(), _out()) == 1
    assert not baseline.exists()
    text = capsys.readouterr().out
    assert "[ОТКАЗ]" in text and "a.json: bends 29 -> 30" in text


def test_growth_with_key_is_written_with_reason_and_rows(baseline, capsys):
    assert _write(_base(), _out(bends=30, steps=4), "цена Р5 — принято") == 0
    got = json.loads(baseline.read_text(encoding="utf-8"))
    assert got["populations"]["edited@model"]["files"]["a.json"]["bends"] \
        == _cell(30)
    assert got["populations"]["raw"] == _base()["populations"]["raw"]
    assert got["allowed_growth"] == [{
        "reason": "цена Р5 — принято",
        # порядок колонок — порядок DEFECT_COLS
        "rows": [{"population": "edited@model", "file": "a.json",
                  "col": "steps", "was": 3, "now": 4},
                 {"population": "edited@model", "file": "a.json",
                  "col": "bends", "was": 29, "now": 30}]}]
    text = capsys.readouterr().out
    assert "[ОТКАЗ]" not in text
    assert "основание: цена Р5 — принято" in text
    assert "edited@model/a.json: bends 29 -> 30" in text
    assert "edited@model/a.json: steps 3 -> 4" in text


def test_history_survives_rewrites(baseline):
    old = [{"reason": "старое", "rows": [{"population": "raw", "file": "r.json",
                                          "col": "bends", "was": 6, "now": 7}]}]
    assert _write(_base(old), _out(), "новое") == 0
    got = json.loads(baseline.read_text(encoding="utf-8"))["allowed_growth"]
    assert [h["reason"] for h in got] == ["старое", "новое"]
    # без роста ключ ничего не дописывает, история не стирается
    assert _write(_base(old), _out(bends=29), "лишнее") == 0
    got = json.loads(baseline.read_text(encoding="utf-8"))["allowed_growth"]
    assert got == old
    # и пересъём без ключа её не стирает
    assert _write(_base(old), _out(bends=28)) == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))[
        "allowed_growth"] == old


def test_key_does_not_lift_incompleteness(baseline):
    assert _write(_base(), _out(), "основание",
                  unjudged=["a.json: прогон 1 — битый файл"]) == 2
    assert not baseline.exists()


@pytest.mark.parametrize("argv", [
    ["--all", "--write-baseline", "--allow-growth", ""],
    ["--all", "--write-baseline", "--allow-growth", "   "],
    ["--all", "--check", "--allow-growth", "основание"],
])
def test_bad_key_is_a_call_error_before_measuring(monkeypatch, argv):
    def measured():
        raise AssertionError("дошло до замера")
    monkeypatch.setattr(sb, "env_problem", measured)
    monkeypatch.setattr(sys, "argv", ["smooth_bench.py", *argv])
    with pytest.raises(SystemExit) as exc:
        sb.main()
    assert exc.value.code == 2
