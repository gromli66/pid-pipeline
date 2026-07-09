"""
Fault-тесты §8.4 batch 3 (contours): observability-контракт task_extract_contours
+ прогресс/типизация в modules/sam2_contour.py::ContourExtractor.predict_batch.

(1) листья ConfigError/ArtifactMissingError/InferenceError, (2) fail_stage довозит
error_code/failed_step до /stages, (3) predict_batch логирует прогресс (движение на
CPU для ~25с-батча, §52) и типизирует сбой одного узла в InferenceError(step=compute),
уже типизированный PipelineError проходит насквозь.

ИЗОЛЯЦИЯ (§9 #2, урок §8.12): torch/torch.nn/torch.nn.functional/cv2/scipy — заглушки
через ФИКСТУРУ `sc` (monkeypatch.setitem → авто-восстановление), НЕ на уровне модуля
(иначе течёт в сессию и роняет соседей — как было с junction/segmentation). nn.Module —
реальный тип (sam2_contour на уровне модуля объявляет `class LoRALayer(nn.Module)`).
ContourExtractor инстанцируем через object.__new__ (минуя тяжёлый __init__ / загрузку SAM2).
"""

import importlib
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

from app.core.errors import (
    ArtifactMissingError,
    ConfigError,
    InferenceError,
    PipelineError,
)
from app.core.logging import get_logger
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage

logger = get_logger(__name__)


@pytest.fixture
def sc(monkeypatch):
    """Изолированный свежий импорт sam2_contour под заглушками (setitem → restore)."""
    _nn = MagicMock()
    _nn.Module = type("Module", (), {})   # реальный базовый класс для LoRALayer(nn.Module)
    _torch = MagicMock()
    _torch.nn = _nn
    monkeypatch.setitem(sys.modules, "torch", _torch)
    monkeypatch.setitem(sys.modules, "torch.nn", _nn)
    monkeypatch.setitem(sys.modules, "torch.nn.functional", MagicMock())
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())
    _scipy = types.ModuleType("scipy")
    _ndi = types.ModuleType("scipy.ndimage")
    _ndi.distance_transform_edt = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "scipy", _scipy)
    monkeypatch.setitem(sys.modules, "scipy.ndimage", _ndi)

    sys.modules.pop("modules.sam2_contour", None)
    mod = importlib.import_module("modules.sam2_contour")
    yield mod
    sys.modules.pop("modules.sam2_contour", None)


# --- 1. Листья ошибок, которыми типизируются raise задачи/модуля ----------------

def test_error_leaves_codes_and_hierarchy():
    assert ConfigError("x").code == "config_invalid"
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert InferenceError("x").code == "inference_failed"
    for cls in (ConfigError, ArtifactMissingError, InferenceError):
        assert issubclass(cls, PipelineError)


# --- 2. Сбой инференса довозится до полей стадии (error_code/failed_step) --------

def test_inference_failure_reaches_stage_fields():
    exc = InferenceError("boom", stage="contour_extraction", step="compute")
    stage = ProcessingStage()
    fail_stage(stage, str(exc)[:500], "TRACE", exc=exc)
    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "inference_failed"
    assert stage.failed_step == "compute"


# --- helpers для модульных тестов predict_batch ---------------------------------

def _extractor(sc):
    return object.__new__(sc.ContourExtractor)   # минуя __init__ (SAM2)


def _dets(n):
    return [{"id": i, "category_id": 1, "bbox": [0, 0, 10, 10]} for i in range(n)]


def _img():
    return np.zeros((10, 10, 3), dtype=np.uint8)


# --- 3. Прогресс-лог батча (движение на CPU) ------------------------------------

def test_predict_batch_logs_progress(sc, caplog):
    ext = _extractor(sc)
    ext.predict = lambda **k: {"polygon_flat": [0, 0, 5, 0, 5, 5],
                               "confidence": 0.9, "status": "auto"}
    with caplog.at_level(logging.INFO):
        res = ext.predict_batch(image=_img(), detections=_dets(25), pipe_mask=None)
    assert len(res) == 25 and res[0]["ann_id"] == 0
    prog = [r for r in caplog.records if "SAM2 contour node" in r.getMessage()]
    assert prog, "нет прогресс-логов predict_batch (движение на CPU не видно)"


# --- 4. Сбой узла → InferenceError(step=compute) --------------------------------

def test_node_failure_typed_as_inference_error(sc):
    ext = _extractor(sc)
    ext.predict = MagicMock(side_effect=RuntimeError("sam boom"))
    with pytest.raises(InferenceError) as ei:
        ext.predict_batch(image=_img(),
                          detections=[{"id": 7, "bbox": [0, 0, 1, 1]}], pipe_mask=None)
    assert ei.value.step == "compute"
    assert ei.value.code == "inference_failed"
    assert isinstance(ei.value.cause, RuntimeError)


# --- 5. Уже типизированный PipelineError проходит насквозь -----------------------

def test_pipeline_error_passthrough(sc):
    ext = _extractor(sc)
    boom = PipelineError("domain-fail", step="compute")
    ext.predict = MagicMock(side_effect=boom)
    with pytest.raises(PipelineError) as ei:
        ext.predict_batch(image=_img(),
                          detections=[{"id": 8, "bbox": [0, 0, 1, 1]}], pipe_mask=None)
    assert ei.value is boom
