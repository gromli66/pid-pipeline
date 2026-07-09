"""
Fault-тесты §8.4 batch 3 (fxml): observability-контракт task_generate_fxml.

Стадия быстрая (~0.07с) — под-под-шаги не дробим; модуль graph_to_fxml не
инструментируем (сбой generate_fxml оборачивает task-level obs.step("compute")).
§8.9 намеренно отложил fxml в эту под-волну (границы задач чистые: build_graph и
generate_fxml — раздельные функции одного graph.py; здесь трогаем ТОЛЬКО fxml).

Задачу как модуль не тянем (Celery/cv2/БД, §9 #2) — типизированные raise
проверяются smoke'ом на стенде (как detection §8.6). Здесь контракт:
(1) листья PipelineError/ArtifactMissingError, (2) obs.step проставляет failed_step
для load_inputs/compute, (3) fail_stage довозит error_code/failed_step до /stages.

Только app.core (без torch) → изоляция sys.modules не нужна.
"""

import logging

import pytest

from app.core.errors import ArtifactMissingError, PipelineError
from app.core.obs import step
from app.core.logging import get_logger
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage

logger = get_logger(__name__)


# --- 1. Листья ошибок, которыми задача типизирует raise -------------------------

def test_error_leaves_codes_and_hierarchy():
    assert PipelineError("x").code == "pipeline_error"
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert issubclass(ArtifactMissingError, PipelineError)


# --- 2. Диаграмма не найдена (PipelineError) довозится до полей стадии -----------

def test_diagram_not_found_reaches_stage_fields():
    exc = PipelineError(
        "Diagram X not found", stage="generating_fxml", diagram_uid="X",
    )
    stage = ProcessingStage()
    fail_stage(stage, str(exc)[:500], "TRACE", exc=exc)
    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "pipeline_error"


# --- 3. obs.step("load_inputs") атрибутирует отсутствующий граф-JSON -------------

def test_missing_graph_attributed_to_load_inputs():
    with pytest.raises(ArtifactMissingError) as ei:
        with step("load_inputs", logger):
            raise ArtifactMissingError(
                "No graph JSON found", stage="generating_fxml"
            )
    assert ei.value.step == "load_inputs"

    stage = ProcessingStage()
    fail_stage(stage, "boom", "TRACE", exc=ei.value)
    assert stage.error_code == "artifact_missing"
    assert stage.failed_step == "load_inputs"


# --- 4. Неожиданный сбой COMPUTE (generate_fxml) → PipelineError(step=compute) ---

def test_obs_step_wraps_unexpected_compute_failure():
    with pytest.raises(PipelineError) as ei:
        with step("compute", logger):
            raise RuntimeError("boom in generate_fxml")
    assert not isinstance(ei.value, RuntimeError)
    assert ei.value.step == "compute"
    assert ei.value.code == "pipeline_error"
    assert isinstance(ei.value.cause, RuntimeError)
