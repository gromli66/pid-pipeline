"""
Тесты: fail_stage пишет error_code / failed_step на строку стадии (Волна 0).

Контракт:
- типизированный PipelineError → error_code = exc.code, failed_step = exc.step;
- любой другой exc → error_code = type(exc).__name__, failed_step = None;
- без exc (все текущие вызовы) → колонки NULL, поведение прежнее;
- stage=None по-прежнему безопасен (no-op).
"""

from app.core.errors import InferenceError
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage


def _new_stage() -> ProcessingStage:
    # in-memory инстанс достаточно: fail_stage только проставляет атрибуты,
    # коммит/сессия не нужны.
    return ProcessingStage()


def test_typed_exc_sets_code_and_step():
    stage = _new_stage()
    exc = InferenceError("boom", step="inference")

    fail_stage(stage, "boom", "TRACE", exc=exc)

    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "inference_failed"
    assert stage.failed_step == "inference"
    assert stage.error_message == "boom"
    assert stage.error_traceback == "TRACE"


def test_plain_exc_falls_back_to_type_name():
    stage = _new_stage()

    fail_stage(stage, "boom", None, exc=ValueError("x"))

    assert stage.error_code == "ValueError"  # нет .code → имя типа
    assert stage.failed_step is None  # нет .step


def test_without_exc_is_backward_compatible():
    stage = _new_stage()

    fail_stage(stage, "boom", "TRACE")  # как все текущие вызовы

    assert stage.status == StageStatus.FAILED
    assert stage.error_message == "boom"
    assert stage.error_traceback == "TRACE"
    assert stage.error_code is None
    assert stage.failed_step is None


def test_none_stage_is_noop():
    # существующий контракт сохранён: None-stage не роняет вызов
    fail_stage(None, "boom", "TRACE", exc=ValueError("x"))


def test_foreign_none_code_falls_back_to_type_name():
    # R6 (аудит 2026-07-09): у SQLAlchemyError есть свой .code (None/"e3q8") —
    # не-строка/пустое не должны затирать имя типа.
    stage = _new_stage()

    class WeirdError(Exception):
        code = None  # как sqlalchemy.exc.SQLAlchemyError

    fail_stage(stage, "boom", None, exc=WeirdError("x"))

    assert stage.error_code == "WeirdError"


def test_oversized_code_and_step_truncated_to_columns():
    # R6: переполнение String(64)/String(32) не должно ронять сам fail-write.
    stage = _new_stage()
    exc = InferenceError("boom", step="s" * 100)
    exc.code = "c" * 100

    fail_stage(stage, "boom", None, exc=exc)

    assert stage.error_code == "c" * 64
    assert stage.failed_step == "s" * 32
