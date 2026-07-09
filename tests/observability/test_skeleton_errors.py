"""
Fault-тесты Волны 3 (skeleton): observability-контракт задач skeletonize /
skeletonize_simple.

Задачу (worker.tasks.skeleton) как модуль не тянем — она импортит Celery/cv2/БД
(§9 #2), а её типизированные raise проверяются smoke'ом на стенде (как detection
§8.6 / segmentation §8.7). Здесь проверяем именно контракт, на который задача
опирается: (1) лист SkeletonizationError, (2) obs.step проставляет failed_step
для сбоя COMPUTE/LOAD_INPUTS, (3) fail_stage довозит error_code/failed_step до
/stages. Модуль skeleton_extension в этой волне не инструментируем (решено:
«минимум» — стадия быстрая, уже печатает [SKEL_EXT] тайминги на stdout-мост).
"""

import logging

import pytest

from app.core.errors import (
    ArtifactMissingError,
    ConfigError,
    PipelineError,
    SkeletonizationError,
)
from app.core.obs import step
from app.core.logging import get_logger
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage

logger = get_logger(__name__)


# --- 1. Листья ошибок, которыми задача типизирует raise'ы -----------------------

def test_error_leaves_codes_and_hierarchy():
    assert SkeletonizationError("x").code == "skeletonization_failed"
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert ConfigError("x").code == "config_invalid"
    assert PipelineError("x").code == "pipeline_error"
    for cls in (SkeletonizationError, ArtifactMissingError, ConfigError):
        assert issubclass(cls, PipelineError)


# --- 2. Сбой COMPUTE довозится до полей стадии (error_code/failed_step) ----------

def test_skeletonization_failure_reaches_stage_fields():
    # то, что задача поднимает при falsy-возврате process_single_image / нет файла
    exc = SkeletonizationError(
        "Skeleton extension returned failure",
        stage="skeletonizing", step="compute",
    )
    stage = ProcessingStage()
    fail_stage(stage, str(exc)[:500], "TRACE", exc=exc)

    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "skeletonization_failed"
    assert stage.failed_step == "compute"


# --- 3. obs.step("load_inputs") атрибутирует отсутствующий вход ------------------

def test_missing_input_attributed_to_load_inputs(caplog):
    # задача поднимает ArtifactMissingError БЕЗ явного step внутри
    # with obs.step("load_inputs") → step проставляется контекст-менеджером.
    with pytest.raises(ArtifactMissingError) as ei:
        with step("load_inputs", logger):
            raise ArtifactMissingError("Image not found: x.png", stage="skeletonizing")

    assert ei.value.step == "load_inputs"

    stage = ProcessingStage()
    fail_stage(stage, "boom", "TRACE", exc=ei.value)
    assert stage.error_code == "artifact_missing"
    assert stage.failed_step == "load_inputs"


# --- 4. Неожиданный сбой COMPUTE → типизируется в PipelineError(step=compute) ----

def test_obs_step_wraps_unexpected_compute_failure():
    # process_single_image кинул неожиданное внутри with obs.step("compute")
    with pytest.raises(PipelineError) as ei:
        with step("compute", logger):
            raise RuntimeError("boom in skeleton_extension")

    assert not isinstance(ei.value, RuntimeError)
    assert ei.value.step == "compute"
    assert ei.value.code == "pipeline_error"
    assert isinstance(ei.value.cause, RuntimeError)


# --- 5. Явный step="compute" на типизированном сбое сохраняется ------------------

def test_explicit_compute_step_preserved():
    boom = SkeletonizationError("returned failure", stage="skeletonizing", step="compute")
    with pytest.raises(SkeletonizationError) as ei:
        with step("compute", logger):
            raise boom

    assert ei.value is boom            # не обёрнут повторно
    assert ei.value.step == "compute"  # не перезатёрт именем шага
