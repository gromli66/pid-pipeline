"""
Тесты типов ошибок CVAT и StageStateError (Волна 1).

Транспортные CVAT-листья ловятся единым `except CVATError`, а StageStateError —
отдельная семья (неверный статус диаграммы), НЕ CVAT-сбой.
"""

import httpx
import pytest

from app.core.errors import (
    CVATConnectionError,
    CVATError,
    CVATExportError,
    CVATImportError,
    CVATRequestError,
    CVATTimeoutError,
    PipelineError,
    StageStateError,
)
from app.services.cvat_client import _cvat_op


def test_cvat_leaf_codes():
    assert CVATError("x").code == "cvat_error"
    assert CVATConnectionError("x").code == "cvat_connection"
    assert CVATTimeoutError("x").code == "cvat_timeout"
    assert CVATRequestError("x").code == "cvat_request"
    assert CVATImportError("x").code == "cvat_import"
    assert CVATExportError("x").code == "cvat_export"


def test_cvat_hierarchy():
    # единый `except CVATError` в detection.py должен ловить все листья
    for cls in (
        CVATConnectionError,
        CVATTimeoutError,
        CVATRequestError,
        CVATImportError,
        CVATExportError,
    ):
        assert issubclass(cls, CVATError)
        assert issubclass(cls, PipelineError)
    assert isinstance(CVATExportError("x"), CVATError)


def test_stage_state_error():
    err = StageStateError("wrong state", stage="cvat_validation", step="confirm")
    assert err.code == "stage_state_invalid"
    assert issubclass(StageStateError, PipelineError)
    assert not issubclass(StageStateError, CVATError)  # это не CVAT-сбой, а предусловие
    assert err.step == "confirm"


def _http_error(code: int) -> None:
    """Бросить httpx.HTTPStatusError с заданным статусом (как raise_for_status)."""
    req = httpx.Request("GET", "http://cvat/api/x")
    resp = httpx.Response(code, text="boom body", request=req)
    raise httpx.HTTPStatusError(str(code), request=req, response=resp)


def test_cvat_op_http_status_to_request_error():
    with pytest.raises(CVATRequestError) as ei:
        with _cvat_op("login"):
            _http_error(500)
    assert ei.value.code == "cvat_request"


def test_cvat_op_http_status_carries_body_in_message():
    # Причина от CVAT (тело ответа) попадает в текст исключения → видно и в
    # docker logs, и в клиентском окне «Не удалось…» (§9 #8).
    with pytest.raises(CVATRequestError) as ei:
        with _cvat_op("create_task"):
            _http_error(400)
    msg = str(ei.value)
    assert "400" in msg
    assert "boom body" in msg


def test_cvat_op_timeout():
    with pytest.raises(CVATTimeoutError):
        with _cvat_op("export_annotations"):
            raise httpx.ReadTimeout("slow")


def test_cvat_op_connect():
    with pytest.raises(CVATConnectionError):
        with _cvat_op("login"):
            raise httpx.ConnectError("connection refused")


def test_cvat_op_wrap_overrides_type():
    # import/export: любой httpx-сбой мапится в свой доменный тип
    with pytest.raises(CVATImportError):
        with _cvat_op("import_annotations", wrap=CVATImportError):
            _http_error(500)
    with pytest.raises(CVATExportError):
        with _cvat_op("export_annotations", wrap=CVATExportError):
            _http_error(400)


def test_cvat_op_passes_domain_error_unchanged():
    # доменный CVATError (напр. export «не готов») не переоборачивается
    orig = CVATExportError("Export not ready after 60 attempts")
    with pytest.raises(CVATExportError) as ei:
        with _cvat_op("export_annotations", wrap=CVATExportError):
            raise orig
    assert ei.value is orig
