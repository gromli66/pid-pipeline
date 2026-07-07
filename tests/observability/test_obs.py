"""
Тесты примитива инструментирования ``obs.step`` / ``bind`` / ``load_artifact``
(Волна 0).

Критерии готовности волны:
- step логирует границы под-шага: start / end + duration_ms;
- при типизированном сбое — проставляет step и пробрасывает ТОТ ЖЕ экземпляр;
- при неожиданном сбое — оборачивает в PipelineError (step + phase + cause);
- поведение не «глотает» ошибки: всё пробрасывается наверх.
"""

import logging

import pytest

from app.core import obs
from app.core.errors import ArtifactMissingError, InferenceError, PipelineError

log = logging.getLogger("test.obs")


def _events(caplog):
    """(step, event) по всем нашим лог-строкам, в порядке появления."""
    return [(r.step, r.event) for r in caplog.records if hasattr(r, "event")]


def test_step_logs_start_end_duration(caplog):
    caplog.set_level(logging.INFO)
    with obs.step("compute", log, tiles=4):
        pass

    recs = [r for r in caplog.records if hasattr(r, "event")]
    assert [(r.step, r.event) for r in recs] == [
        ("compute", "start"),
        ("compute", "end"),
    ]
    start, end = recs
    assert start.tiles == 4  # свободные поля прокидываются в start
    assert isinstance(end.duration_ms, int)
    assert end.duration_ms >= 0


def test_step_typed_error_reraised_with_step(caplog):
    caplog.set_level(logging.INFO)
    original = InferenceError("boom")

    with pytest.raises(InferenceError) as ei:
        with obs.step("compute", log):
            raise original

    assert ei.value is original  # не переоборачиваем типизированное
    assert ei.value.step == "compute"  # step проставлен

    err_recs = [r for r in caplog.records if getattr(r, "event", None) == "error"]
    assert len(err_recs) == 1
    assert err_recs[0].code == "inference_failed"
    assert err_recs[0].exc_info is not None  # exc_info=True → traceback в логе
    # duration/end на сбое не логируется
    assert not [r for r in caplog.records if getattr(r, "event", None) == "end"]


def test_step_preserves_existing_step(caplog):
    caplog.set_level(logging.INFO)
    err = InferenceError("boom", step="inference")  # step уже проставлен вложенным шагом

    with pytest.raises(InferenceError) as ei:
        with obs.step("compute", log):
            raise err

    assert ei.value.step == "inference"  # внешний step не перетирает существующий


def test_step_wraps_unexpected(caplog):
    caplog.set_level(logging.INFO)
    obs.bind(phase="segmenting")

    with pytest.raises(PipelineError) as ei:
        with obs.step("compute", log):
            raise ValueError("unexpected root")

    exc = ei.value
    assert type(exc) is PipelineError  # именно базовый тип
    assert exc.step == "compute"
    assert exc.stage == "segmenting"  # phase взят из bind()
    assert isinstance(exc.cause, ValueError)  # исходное сохранено
    assert exc.__cause__ is exc.cause  # raise ... from ... (цепочка трейсбека)

    err_recs = [r for r in caplog.records if getattr(r, "event", None) == "error"]
    assert len(err_recs) == 1
    assert err_recs[0].exc_info is not None


def test_load_artifact_missing_raises(tmp_path):
    missing = tmp_path / "nope.png"
    with pytest.raises(ArtifactMissingError) as ei:
        with obs.load_artifact("PIPE_MASK", missing, log):
            pass
    assert ei.value.step == "load_inputs"
    assert ei.value.code == "artifact_missing"


def test_load_artifact_ok_yields_path(tmp_path):
    f = tmp_path / "ok.png"
    f.write_bytes(b"x")
    with obs.load_artifact("PIPE_MASK", f, log) as p:
        assert p == f
