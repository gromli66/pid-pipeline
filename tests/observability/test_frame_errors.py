"""
Fault-тесты Волны 3 (frame_removal): закрытие DoD-дыры по образцу §8.5
(async-хелперы `start_frame_stage`/`get_running_frame_stage`/`fail_frame_stage` в
`app/api/frame_stage_helpers.py`, НЕ воркерный start_stage/fail_stage). frame_removal
— ручная/await-стадия (RUNBOOK §8.8: budget=None, наравне с cvat_validation/
mask_validation/graph_validation) — 4 эндпоинта (start/save/complete/skip) +
UI-вкладка, произвольное время оператора.

Юнитим то же, что test_cvat_stage_rows.py юнитит для CVAT: чистую `fail_frame_stage`
(без БД/FastAPI, обычный ProcessingStage()). `start_frame_stage`/`get_running_frame_stage`
пишут через AsyncSession — прецедента юнит-теста в репо нет (та же ситуация с
`_start_cvat_stage` в Волне 1), проверяются смоуком на стенде.

Хелперы вынесены в отдельный модуль `frame_stage_helpers.py` (а не оставлены в
`frame.py`) именно затем, чтобы этот тест мог загрузить их напрямую: `frame.py`
тянет `app.services.storage` → `aiofiles`, которого нет в тест-venv (тот же
прецедент, что уже документировал test_cvat_stage_rows.py для cvat.py/storage.py).
`frame_stage_helpers.py` этой зависимости не имеет — грузим ПО ПУТИ, минуя
`app/api/__init__.py` (тем же приёмом, что и `cvat.py` там), для симметрии с
прецедентом и на случай будущих тяжёлых соседей по пакету.
"""

import importlib.util
import pathlib

import pytest

from app.core.errors import PipelineError
from app.models.stage import ProcessingStage, StageStatus


@pytest.fixture(scope="module")
def fail_frame_stage():
    """`fail_frame_stage` из app/api/frame_stage_helpers.py, загруженного ПО ПУТИ."""
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "app" / "api" / "frame_stage_helpers.py"
    )
    spec = importlib.util.spec_from_file_location("frame_stage_helpers_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.fail_frame_stage


def test_fail_frame_stage_uses_exc_code_and_step(fail_frame_stage):
    stage = ProcessingStage()
    try:
        raise PipelineError("boom", step="save")
    except PipelineError as exc:
        fail_frame_stage(stage, exc, default_step="start")

    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "pipeline_error"
    assert stage.failed_step == "save"  # из exc, дефолт не применился
    assert stage.error_message == "boom"
    assert stage.error_traceback  # traceback заполнен


def test_fail_frame_stage_falls_back_to_default_step(fail_frame_stage):
    stage = ProcessingStage()
    try:
        raise PipelineError("boom")  # .step пуст
    except PipelineError as exc:
        fail_frame_stage(stage, exc, default_step="skip")

    assert stage.error_code == "pipeline_error"
    assert stage.failed_step == "skip"  # дефолтный под-шаг
