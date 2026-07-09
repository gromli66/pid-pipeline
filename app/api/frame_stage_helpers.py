"""
Async-хелперы ProcessingStage для интерактивных frame-эндпоинтов (RUNBOOK §8.5-канон).

Вынесены из `app/api/frame.py` в отдельный файл, чтобы юнит-тесты могли грузить их
БЕЗ транзитивной зависимости на `app.services.storage` → `aiofiles` (которого нет в
тест-venv — тот же прецедент, что `test_cvat_stage_rows.py` уже документировал для
CVAT: `cvat.py` дешёво избегает `storage.py`, `frame.py` — не может, ему нужен
`StorageService`). `frame.py` импортирует эти функции для рантайма как обычно.

Зеркалят `_start_cvat_stage`/`_fail_cvat_stage` (`app/api/cvat.py`, Волна 1): воркерные
`start_stage`/`fail_stage` (`worker/utils/db_helpers.py`) синхронные — здесь AsyncSession.
"""

import traceback
from typing import Optional
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stage import ProcessingStage, StageStatus, StageType


async def start_frame_stage(db: AsyncSession, uid: UUID) -> ProcessingStage:
    """RUNNING-строка `frame_removal`. Коммитит сразу — видна в `/stages`, пока
    оператор работает во вкладке."""
    attempt = (
        await db.execute(
            select(func.count())
            .select_from(ProcessingStage)
            .where(
                ProcessingStage.diagram_uid == uid,
                ProcessingStage.stage_type == StageType.FRAME_REMOVAL,
            )
        )
    ).scalar_one() + 1
    stage = ProcessingStage(
        diagram_uid=uid,
        stage_type=StageType.FRAME_REMOVAL,
        status=StageStatus.PENDING,
        attempt=attempt,
    )
    stage.start()
    db.add(stage)
    await db.commit()
    return stage


async def get_running_frame_stage(db: AsyncSession, uid: UUID) -> Optional[ProcessingStage]:
    """Последняя RUNNING-строка `frame_removal` (открыта в /start), если есть."""
    result = await db.execute(
        select(ProcessingStage)
        .where(
            ProcessingStage.diagram_uid == uid,
            ProcessingStage.stage_type == StageType.FRAME_REMOVAL,
            ProcessingStage.status == StageStatus.RUNNING,
        )
        .order_by(ProcessingStage.id.desc())
    )
    return result.scalars().first()


def fail_frame_stage(stage: ProcessingStage, exc: BaseException, *, default_step: str) -> None:
    """Проставить FAILED + `error_code`/`failed_step`/traceback (БЕЗ commit).

    Зеркалит `_fail_cvat_stage`: `error_code` = `exc.code` (или имя типа),
    `failed_step` = `exc.step` (проставлен `obs.step`), иначе — `default_step`.
    """
    # .code бывает чужим (у SQLAlchemyError свой .code = None/"e3q8"): берём
    # только непустую строку, иначе — имя типа (аудит 2026-07-09, R6).
    code = getattr(exc, "code", None)
    stage.fail(
        str(exc)[:2000],
        traceback.format_exc()[:10000],
        error_code=code if isinstance(code, str) and code else type(exc).__name__,
        failed_step=getattr(exc, "step", None) or default_step,
    )
