"""
Fault-тесты §8.4 batch 3 (direction): observability-контракт task_classify_direction.

Стадия быстрая (~0.69с) — под-под-шаги COMPUTE не дробим (§1); модуль
direction_classifier не инструментируем (сбой классификации возвращается как
None и обрабатывается задачей, типизированных raise нет). Задачу как модуль не
тянем (Celery/torch/cv2/БД, §9 #2) — типизированные raise проверяются smoke'ом на
стенде (как detection §8.6). Здесь контракт, на который задача опирается:
(1) листья ConfigError/ArtifactMissingError, (2) obs.step проставляет failed_step
для load_inputs/compute, (3) fail_stage довозит error_code/failed_step до /stages.

Только app.core (чистый, без torch) → изоляция sys.modules не нужна.
"""

import logging

import pytest

from app.core.errors import ArtifactMissingError, ConfigError, PipelineError
from app.core.obs import step
from app.core.logging import get_logger
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage

logger = get_logger(__name__)


# --- 1. Листья ошибок, которыми задача типизирует raise -------------------------

def test_error_leaves_codes_and_hierarchy():
    assert ConfigError("x").code == "config_invalid"
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert PipelineError("x").code == "pipeline_error"
    for cls in (ConfigError, ArtifactMissingError):
        assert issubclass(cls, PipelineError)


# --- 2. ConfigError (нет конфига проекта) довозится до полей стадии --------------

def test_config_error_reaches_stage_fields():
    exc = ConfigError("Project config not found", stage="direction_classification")
    stage = ProcessingStage()
    fail_stage(stage, str(exc)[:500], "TRACE", exc=exc)
    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "config_invalid"


# --- 3. obs.step("load_inputs") атрибутирует отсутствующий вход ------------------

def test_missing_input_attributed_to_load_inputs():
    with pytest.raises(ArtifactMissingError) as ei:
        with step("load_inputs", logger):
            raise ArtifactMissingError(
                "COCO validated not found", stage="direction_classification"
            )
    assert ei.value.step == "load_inputs"

    stage = ProcessingStage()
    fail_stage(stage, "boom", "TRACE", exc=ei.value)
    assert stage.error_code == "artifact_missing"
    assert stage.failed_step == "load_inputs"


# --- 4. Неожиданный сбой COMPUTE → PipelineError(step=compute) -------------------

def test_obs_step_wraps_unexpected_compute_failure():
    with pytest.raises(PipelineError) as ei:
        with step("compute", logger):
            raise RuntimeError("boom in direction classifier")
    assert not isinstance(ei.value, RuntimeError)
    assert ei.value.step == "compute"
    assert ei.value.code == "pipeline_error"
    assert isinstance(ei.value.cause, RuntimeError)
