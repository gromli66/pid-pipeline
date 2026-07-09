"""
Регресс (тест-сессия Волна0-1, §4-B): start_stage коммитит RUNNING-строку сразу.

Почему это важно: `reopen-bbox-validation` останавливает бегущую стадию, находя
её ProcessingStage в статусе RUNNING/PENDING (в ОТДЕЛЬНОЙ сессии) и делая revoke
по celery_task_id. Если start_stage лишь flush'ит строку (без commit), она не
видна другой сессии всю стадию → reopen ничего не ревокает, задача добегает и
корраптит статус (наблюдали: revoked_tasks=0 посреди сегментации, статус уезжал
в skeletonized). Поэтому start_stage ОБЯЗАН коммитить RUNNING-строку.
"""

from unittest.mock import MagicMock

from app.models.stage import StageStatus, StageType
from worker.utils.db_helpers import start_stage


def _mock_db() -> MagicMock:
    db = MagicMock()
    # attempt-счётчик: db.query(...).filter(...).count() -> 0  => attempt=1
    db.query.return_value.filter.return_value.count.return_value = 0
    return db


def test_start_stage_marks_running_with_task_id():
    db = _mock_db()

    stage = start_stage(db, "uid-1", StageType.SEGMENTATION, celery_task_id="task-1")

    # именно это ищет reopen: RUNNING + celery_task_id
    assert stage.status == StageStatus.RUNNING
    assert stage.celery_task_id == "task-1"


def test_start_stage_commits_so_reopen_can_see_running():
    db = _mock_db()

    start_stage(db, "uid-1", StageType.SEGMENTATION, celery_task_id="task-1")

    # ключевой инвариант фикса: строка закоммичена (иначе чужая сессия её не увидит)
    db.commit.assert_called_once()
