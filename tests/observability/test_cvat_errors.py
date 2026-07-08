"""
Тесты типов ошибок CVAT и StageStateError (Волна 1).

Транспортные CVAT-листья ловятся единым `except CVATError`, а StageStateError —
отдельная семья (неверный статус диаграммы), НЕ CVAT-сбой.
"""

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
