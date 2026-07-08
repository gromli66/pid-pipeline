"""
Stats API — агрегаты по стадиям для клиентских бюджетов прогресса.

`GET /api/stats/stage-durations` — p50 реальных длительностей авто-стадий за окно.
Клиент (`ui/services/progress_model.py`) кладёт их в `compute_progress(budgets=…)`,
чтобы прогресс/ETA считались от бюджетов ТОГО железа, где реально крутится бой
(CPU), а не от статических GPU-сидов `_DEFAULT_BUDGETS` (замирание бегущей фазы, §52).

Медиану считаем в чистом `compute_stage_budgets()` на строках, вытянутых одним
`SELECT` (окно / только `completed` / исключения / медиана / N≥5 — в хелпере, а не
в SQL, чтобы это покрывалось юнит-тестом на фикстурах: тест-харнес — SQLite, где
`percentile_cont … WITHIN GROUP` недоступен, а async-эндпоинт замокан). `SELECT`
с `WHERE` — лишь дешёвый скоуп-префильтр по тому же окну; источник правды — хелпер.
`statistics.median` для чётного N интерполирует середину — совпадает с
`percentile_cont(0.5)`.
"""

from datetime import datetime, timedelta
from statistics import median
from typing import Dict, Iterable

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_db
from app.models.stage import ProcessingStage, StageStatus

router = APIRouter()

# Окно свежести = окно ретеншна ошибок (§3): текущий сервер + текущий код,
# без GPU/до-опт перекоса. Пересмотреть на 14 д, если тайминги часто плывут.
_WINDOW_DAYS = 30
# Минимум наблюдений на стадию в окне; меньше → стадию не отдаём, и клиент берёт
# свой статический сид (_DEFAULT_BUDGETS) через merged.update() (фоллбэк < N=5).
_MIN_OBSERVATIONS = 5
# Ручные/await-стадии — человеческое время, не бюджет пайплайна. Это в точности
# стадии с budget=None в progress_model (frame_removal тоже ручная — иначе p50
# по ней перекрыл бы None→число и показал ETA на ручном шаге).
_MANUAL_STAGES = frozenset({
    "frame_removal",
    "cvat_validation",
    "mask_validation",
    "graph_validation",
})


def compute_stage_budgets(
    rows: Iterable[tuple],
    *,
    now: datetime,
    window_days: int = _WINDOW_DAYS,
    min_observations: int = _MIN_OBSERVATIONS,
    exclude: frozenset = _MANUAL_STAGES,
) -> Dict[str, float]:
    """p50 длительности (сек) по `stage_type` из строк стадий.

    rows: (stage_type, status, duration_seconds, completed_at).
    Отбор: `status == completed`, `completed_at` в окне [now-window; now],
    `duration_seconds > 0`, `stage_type` не в `exclude`. Медиана на стадию;
    стадии с < `min_observations` наблюдений опускаем (клиент → статический сид).
    """
    cutoff = now - timedelta(days=window_days)
    buckets: Dict[str, list] = {}
    for stage_type, status, duration_seconds, completed_at in rows:
        if stage_type in exclude:
            continue
        if str(getattr(status, "value", status)).lower() != "completed":
            continue
        if duration_seconds is None or duration_seconds <= 0:
            continue
        if completed_at is None or completed_at < cutoff:
            continue
        buckets.setdefault(stage_type, []).append(float(duration_seconds))

    return {
        stage_type: float(median(durations))
        for stage_type, durations in buckets.items()
        if len(durations) >= min_observations
    }


@router.get("/stage-durations")
async def get_stage_durations(db: AsyncSession = Depends(get_async_db)) -> dict:
    """p50 длительностей авто-стадий за окно (сек), по `stage_type`.

    Разовый агрегат для клиентских бюджетов прогресса (клиент кэширует на сессию).
    Стадии с < N наблюдений в окне опущены → клиент берёт статический сид.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=_WINDOW_DAYS)

    # WHERE — дешёвый скоуп-префильтр (не тянуть всю таблицу); бизнес-отбор и
    # медиана — в compute_stage_budgets (единый источник правды, покрыт тестами).
    result = await db.execute(
        select(
            ProcessingStage.stage_type,
            ProcessingStage.status,
            ProcessingStage.duration_seconds,
            ProcessingStage.completed_at,
        ).where(
            ProcessingStage.status == StageStatus.COMPLETED,
            ProcessingStage.completed_at >= cutoff,
            ProcessingStage.duration_seconds.isnot(None),
        )
    )

    budgets = compute_stage_budgets(result.all(), now=now)
    return {
        "budgets": budgets,
        "window_days": _WINDOW_DAYS,
        "min_observations": _MIN_OBSERVATIONS,
    }
