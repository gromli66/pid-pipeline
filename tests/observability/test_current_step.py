"""
Волна B: current_step — под-шаг бегущей стадии доезжает до клиента.

Слои: репортер worker'а (отдельная сессия, UPDATE по id) → колонка модели
(чистится на complete/fail) → поле /stages (ProcessingStageResponse).
Механика obs.bind_step_sink/step() — в test_obs.py; клиентская строка
статусбара — в test_substep_status_line.py.
"""

from unittest.mock import MagicMock

from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import make_step_reporter


def _mock_session(monkeypatch):
    import app.db.session as session_mod
    session = MagicMock()
    monkeypatch.setattr(session_mod, "SessionLocal", MagicMock(return_value=session))
    return session


def test_reporter_updates_current_step_by_id(monkeypatch):
    session = _mock_session(monkeypatch)

    make_step_reporter(42)("inference")

    session.query.return_value.filter.return_value.update.assert_called_once_with(
        {"current_step": "inference"}
    )
    session.commit.assert_called_once()
    session.close.assert_called_once()


def test_reporter_truncates_to_column_width(monkeypatch):
    session = _mock_session(monkeypatch)

    make_step_reporter(1)("x" * 100)

    session.query.return_value.filter.return_value.update.assert_called_once_with(
        {"current_step": "x" * 64}
    )


def test_reporter_closes_session_on_failure(monkeypatch):
    # ошибка поднимается (глотает obs.step), но сессия закрыта — без утечек
    session = _mock_session(monkeypatch)
    session.commit.side_effect = RuntimeError("db down")

    try:
        make_step_reporter(1)("compute")
    except RuntimeError:
        pass
    session.close.assert_called_once()


def test_complete_and_fail_clear_current_step():
    stage = ProcessingStage()
    stage.current_step = "inference"
    stage.complete()
    assert stage.status == StageStatus.COMPLETED
    assert stage.current_step is None

    stage2 = ProcessingStage()
    stage2.current_step = "inference"
    stage2.fail("boom")
    assert stage2.status == StageStatus.FAILED
    assert stage2.current_step is None


def test_stages_response_carries_current_step():
    from app.schemas.diagram import ProcessingStageResponse

    r = ProcessingStageResponse(
        id=1, stage_type="segmentation", status="running", attempt=1,
        current_step="inference",
    )
    assert r.current_step == "inference"
    # поле опционально — старые продьюсеры/потребители не ломаются
    r2 = ProcessingStageResponse(id=2, stage_type="ocr", status="pending", attempt=1)
    assert r2.current_step is None
