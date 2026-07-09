"""
R2 (аудит 2026-07-09): FAILED-попытка фиксируется ПЕРЕД ``raise self.retry(...)``.

Без коммита строка попытки навсегда остаётся RUNNING (start_stage коммитит
RUNNING сразу — см. test_start_stage_commit), а error_code/failed_step/traceback
нефинальных попыток теряются; в ocr/contours/direction ``db.rollback()`` ПОСЛЕ
fail_stage вовсе стирал сам фейл. Контракт ``persist_failed_attempt``:
- порядок: rollback (сброс незакоммиченного задачи) → fail_stage → commit;
- сбой персиста глотается (Retry не должен маскироваться сбоем БД);
- stage=None безопасен (как у fail_stage).
"""

from unittest.mock import MagicMock

from app.core.errors import InferenceError
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import persist_failed_attempt


def test_persists_failed_attempt_rollback_first_then_commit():
    db = MagicMock()
    stage = ProcessingStage()
    exc = InferenceError("boom", step="inference")

    persist_failed_attempt(db, stage, "boom", "TRACE", exc=exc)

    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "inference_failed"
    assert stage.failed_step == "inference"
    assert stage.error_traceback == "TRACE"
    # порядок: rollback (не тащим чужие изменения в коммит фейла) → commit
    names = [c[0] for c in db.method_calls]
    assert "rollback" in names and "commit" in names
    assert names.index("rollback") < names.index("commit")
    db.commit.assert_called_once()


def test_persist_failure_is_swallowed_to_not_mask_retry():
    db = MagicMock()
    db.commit.side_effect = RuntimeError("db down")
    stage = ProcessingStage()

    # не поднимает: иначе Retry замаскирован и задача уйдёт в FAILURE без попыток
    persist_failed_attempt(db, stage, "boom")


def test_none_stage_is_safe():
    db = MagicMock()

    persist_failed_attempt(db, None, "boom")

    # fail_stage(None) — noop, но commit безвреден
    db.commit.assert_called_once()
