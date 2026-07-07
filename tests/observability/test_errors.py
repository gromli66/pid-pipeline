"""
Тесты доменной иерархии ошибок (Волна 0).

Проверяем стабильные ``code``, корреляционные поля и наследование —
контракт, на который опираются obs.step() и (позже) fail_stage/set_diagram_error.
"""

import pytest

from app.core.errors import (
    ArtifactMissingError,
    ArtifactWriteError,
    CVATError,
    GpuOutOfMemoryError,
    InferenceError,
    ModelLoadError,
    PipelineError,
)


def test_base_defaults():
    err = PipelineError("boom")
    assert err.code == "pipeline_error"
    assert err.message == "boom"
    assert str(err) == "boom"
    assert err.stage is None
    assert err.diagram_uid is None
    assert err.step is None
    assert err.cause is None


def test_correlation_fields_stored():
    cause = ValueError("root")
    err = PipelineError(
        "boom", stage="segmenting", diagram_uid="uid-1", step="compute", cause=cause
    )
    assert err.stage == "segmenting"
    assert err.diagram_uid == "uid-1"
    assert err.step == "compute"
    assert err.cause is cause


def test_leaf_codes():
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert ArtifactWriteError("x").code == "artifact_write_failed"
    assert ModelLoadError("x").code == "model_load_failed"
    assert InferenceError("x").code == "inference_failed"
    assert GpuOutOfMemoryError("x").code == "gpu_oom"
    assert CVATError("x").code == "cvat_error"


def test_hierarchy():
    # Каждый лист — PipelineError: единый `except PipelineError` в пайплайне
    # ловит всё семейство.
    for cls in (
        ArtifactMissingError,
        ArtifactWriteError,
        ModelLoadError,
        InferenceError,
        GpuOutOfMemoryError,
        CVATError,
    ):
        assert issubclass(cls, PipelineError)
    # GpuOOM — частный случай InferenceError.
    assert issubclass(GpuOutOfMemoryError, InferenceError)
    assert isinstance(GpuOutOfMemoryError("x"), InferenceError)


def test_correlation_is_keyword_only():
    # stage/diagram_uid/step/cause — только по имени: защищает сигнатуру от
    # случайной передачи по позиции.
    with pytest.raises(TypeError):
        PipelineError("boom", "segmenting")
