
"""
Тесты ContextFilter и конфигурации логов (Волна 0).

ContextFilter инъектирует корреляционные поля (uid/phase/step/attempt/task_id)
в каждую запись: из obs.bind, а step — из extra под-шага; чего нет — "-".
"""

import logging

import pytest

from app.core import obs
from app.core.logging import ContextFilter, _parse_overrides


@pytest.fixture(autouse=True)
def _reset_ctx():
    # чистим корреляционный контекст между тестами (обход утечки contextvar)
    obs._ctx.set({})
    yield
    obs._ctx.set({})


def _record(**extra) -> logging.LogRecord:
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1, "msg", (), None)
    for key, val in extra.items():
        setattr(rec, key, val)
    return rec


def test_defaults_when_unbound():
    rec = _record()
    ContextFilter().filter(rec)
    assert rec.uid == "-"
    assert rec.phase == "-"
    assert rec.step == "-"
    assert rec.attempt == "-"
    assert rec.task_id == "-"


def test_injects_bound_values():
    obs.bind(uid="U1", phase="segmenting", attempt=3, task_id="t-9")
    rec = _record()
    ContextFilter().filter(rec)
    assert rec.uid == "U1"
    assert rec.phase == "segmenting"
    assert rec.attempt == 3
    assert rec.task_id == "t-9"
    assert rec.step == "-"  # step не биндится — только через step()


def test_does_not_overwrite_extra_step():
    obs.bind(phase="segmenting")
    rec = _record(step="compute")  # как проставляет obs.step() через extra
    ContextFilter().filter(rec)
    assert rec.step == "compute"  # контекст не перетирает уже заданное поле


def test_duration_ms_default_and_from_extra():
    # обычная запись — duration_ms нет → "-" (иначе LOG_FORMAT падает)
    rec = _record()
    ContextFilter().filter(rec)
    assert rec.duration_ms == "-"
    # step.end кладёт duration_ms в extra → сохраняется (виден в тексте лога)
    rec2 = _record(duration_ms=1234)
    ContextFilter().filter(rec2)
    assert rec2.duration_ms == 1234


def test_parse_overrides():
    assert _parse_overrides("httpx:ERROR, sqlalchemy:INFO ") == {
        "httpx": "ERROR",
        "sqlalchemy": "INFO",
    }
    assert _parse_overrides("") == {}
    assert _parse_overrides("garbage,novalue:") == {}  # мусор игнорируется
