"""
Тесты закрытия DoD-дыры Волны 1 (§8.4 шаг 2 / §9 #8, Вариант A):
интерактивные CVAT-эндпоинты пишут строку `cvat_validation` в `/stages`,
а `failed_step` различает под-шаги (болевой `upload_media` vs общий сбой).

Юнитим три механизма (без живой БД/CVAT — полный эндпоинт на смоук стенда):
- `_cvat_op(step=...)` проставляет `.step` на доменное CVAT-исключение;
- `create_task` разбит на две фазы: `task_shell` (POST /tasks) и
  `upload_media` (POST /tasks/{id}/data) → правильный `.step` по месту сбоя;
- `_fail_cvat_stage` кладёт `error_code`/`failed_step` из исключения (или дефолт).
"""

import importlib.util
import pathlib
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.errors import CVATRequestError
from app.models.stage import ProcessingStage, StageStatus
from app.services.cvat_client import CVATClient, _cvat_op


def _status_error(code: int) -> httpx.HTTPStatusError:
    """httpx.HTTPStatusError с заданным статусом (как raise_for_status)."""
    req = httpx.Request("POST", "http://cvat/api/x")
    resp = httpx.Response(code, text="too large", request=req)
    return httpx.HTTPStatusError(str(code), request=req, response=resp)


@pytest.fixture(scope="module")
def fail_cvat_stage():
    """`_fail_cvat_stage` из app/api/cvat.py, загруженного ПО ПУТИ.

    `from app.api.cvat import ...` исполнил бы `app/api/__init__.py`
    (diagrams→storage→aiofiles — нет в тест-среде, RUNBOOK §9 #2); сам `cvat.py`
    таких зависимостей не имеет. Тот же приём, что в `test_progress_model`
    (Волна 2): грузим модуль по файлу, минуя тяжёлый `__init__` пакета.
    Ленивая фикстура — сбой здесь изолирован в эти два теста, не роняет сборку.
    """
    path = pathlib.Path(__file__).resolve().parents[2] / "app" / "api" / "cvat.py"
    spec = importlib.util.spec_from_file_location("cvat_api_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._fail_cvat_stage


# --- 1. _cvat_op проставляет step на исключение --------------------------------

def test_cvat_op_sets_step_on_error():
    with pytest.raises(CVATRequestError) as ei:
        with _cvat_op("upload_media", step="upload_media"):
            raise _status_error(413)
    assert ei.value.step == "upload_media"
    assert ei.value.code == "cvat_request"


def test_cvat_op_step_defaults_to_none():
    # прочие вызовы step не передают → поведение прежнее (27 тестов Волны 1 целы)
    with pytest.raises(CVATRequestError) as ei:
        with _cvat_op("login"):
            raise _status_error(500)
    assert ei.value.step is None


# --- 2. create_task: две фазы → точный failed_step ----------------------------

def _client_with_mocked_http() -> CVATClient:
    client = CVATClient(base_url="http://cvat", token="t")
    client._client = MagicMock()
    return client


def test_create_task_shell_phase_step(tmp_path):
    """Сбой оболочки задачи (POST /tasks) → step=task_shell."""
    img = tmp_path / "image.png"
    img.write_bytes(b"x")
    client = _client_with_mocked_http()

    def fake_post(url, **kwargs):
        resp = MagicMock()
        if url == "/api/tasks":
            resp.raise_for_status.side_effect = _status_error(400)
        return resp

    client._client.post.side_effect = fake_post

    with pytest.raises(CVATRequestError) as ei:
        client.create_task(project_id=1, name="t", image_path=img)
    assert ei.value.step == "task_shell"


def test_create_task_upload_media_phase_step(tmp_path):
    """Сбой загрузки медиа (POST /tasks/{id}/data, большой файл) → step=upload_media."""
    img = tmp_path / "image.png"
    img.write_bytes(b"x")
    client = _client_with_mocked_http()

    def fake_post(url, **kwargs):
        resp = MagicMock()
        if url == "/api/tasks":
            resp.json.return_value = {"id": 7}
            resp.raise_for_status.return_value = None
        else:  # /api/tasks/7/data
            resp.raise_for_status.side_effect = _status_error(413)
        return resp

    client._client.post.side_effect = fake_post

    with pytest.raises(CVATRequestError) as ei:
        client.create_task(project_id=1, name="t", image_path=img)
    assert ei.value.step == "upload_media"


def test_wait_for_data_failed_maps_to_upload_media():
    """Асинхронный отказ CVAT (state=Failed после /data) → upload_media + причина (§9 #8, b)."""
    client = _client_with_mocked_http()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "state": "Failed",
        "message": "Traceback ...\nPIL.Image.DecompressionBombError: image too large",
    }
    client._client.get.return_value = resp

    with pytest.raises(CVATRequestError) as ei:
        client._wait_for_data(task_id=1, max_attempts=1, delay=0)
    assert ei.value.step == "upload_media"
    assert "image too large" in str(ei.value)  # последняя строка traceback CVAT


def test_wait_for_data_finished_returns_none():
    client = _client_with_mocked_http()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"state": "Finished"}
    client._client.get.return_value = resp

    assert client._wait_for_data(task_id=1, max_attempts=1, delay=0) is None


# --- 3. _fail_cvat_stage: атрибуция на строку стадии --------------------------

def test_fail_cvat_stage_uses_exc_code_and_step(fail_cvat_stage):
    stage = ProcessingStage()
    try:
        raise CVATRequestError("boom", step="upload_media")
    except CVATRequestError as exc:
        fail_cvat_stage(stage, exc, default_step="create_task")

    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "cvat_request"
    assert stage.failed_step == "upload_media"  # из exc, дефолт не применился
    assert stage.error_message == "boom"
    assert stage.error_traceback  # traceback заполнен


def test_fail_cvat_stage_falls_back_to_default_step(fail_cvat_stage):
    stage = ProcessingStage()
    try:
        raise CVATRequestError("boom")  # .step пуст
    except CVATRequestError as exc:
        fail_cvat_stage(stage, exc, default_step="create_task")

    assert stage.error_code == "cvat_request"
    assert stage.failed_step == "create_task"  # дефолтный под-шаг
