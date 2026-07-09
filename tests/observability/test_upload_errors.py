"""
Fault-тесты Волны 3 (upload): контракт нового листа `InvalidUploadError`.

upload — не worker-задача (async-эндпоинт `app/api/diagrams.py::upload_diagram`,
AsyncSession) и не «ручная/await»-стадия: по решению пользователя (RUNBOOK §0.2,
развилка upload/frame под-волны) инструментирована ТОЛЬКО логами/типизацией
(obs.bind + obs.step("compute")/obs.step("persist_artifacts") + typed raise) —
БЕЗ ProcessingStage-строки (StageType.UPLOAD остаётся неиспользуемым, как раньше).

Сам эндпоинт не юнит-тестируем без большого мока FastAPI/AsyncSession/StorageService —
для app/api/*.py в этом репо нет прецедента тестировать сами роуты (см.
test_cvat_stage_rows.py: тестируются только чистые хелперы). Здесь — контракт листа,
которым эндпоинт типизирует raise; сам wiring проверяется смоуком на стенде.
"""

from app.core.errors import ArtifactMissingError, InvalidUploadError, PipelineError


def test_invalid_upload_error_code():
    assert InvalidUploadError("x").code == "invalid_upload"


def test_invalid_upload_error_is_pipeline_error():
    assert issubclass(InvalidUploadError, PipelineError)
    assert isinstance(InvalidUploadError("x"), PipelineError)


def test_invalid_upload_error_distinct_from_artifact_missing():
    # Разные семьи: InvalidUploadError — контент ЕСТЬ, но битый;
    # ArtifactMissingError — файла нет вовсе. Не должны пересекаться в иерархии.
    assert InvalidUploadError.code != ArtifactMissingError.code
    assert not issubclass(InvalidUploadError, ArtifactMissingError)
    assert not issubclass(ArtifactMissingError, InvalidUploadError)


def test_invalid_upload_error_carries_correlation_fields():
    # Как раньше InvalidUploadError поднимается с stage/diagram_uid (RUNBOOK
    # §5-Волна0 контракт) — obs.step проставит step="compute" сам, если не задан.
    exc = InvalidUploadError("PDF render failed: x", stage="upload", diagram_uid="uid-1")
    assert exc.stage == "upload"
    assert exc.diagram_uid == "uid-1"
    assert exc.step is None  # проставляется obs.step() при всплытии, не здесь
