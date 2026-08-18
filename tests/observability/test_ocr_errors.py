"""
Fault-тесты под-волны §8.4 batch 3 (OCR): observability-контракт task_run_ocr +
под-под-шаги COMPUTE в modules/ocr/pipeline_clean.py.

Задачу worker.tasks.ocr как модуль не тянем (Celery/torch/БД, §9 #2) — её
типизированные raise проверяются smoke'ом на стенде (как detection §8.6). Здесь:
(1) листья OcrError/ModelLoadError/ArtifactMissingError, (2) fail_stage довозит
error_code/failed_step до /stages, (3) под-под-шаги text_detect/recognize/postfilter
в pipeline_clean логируют start/end+duration_ms и типизируют сбой инференса в
OcrError(step=…).

Изоляция (§9 #2, канон docs/TESTING.md §6): cv2 / ultralytics / sibling-модули
modules.ocr → MagicMock ФИКСТУРОЙ ``pc`` через monkeypatch.setitem, а НЕ на уровне
модуля (пункт 0.3x: модуль-уровневая заглушка исполняется на сборке pytest и
держит мок всю сессию — так и краснели чужие тесты масок, MEASUREMENTS §37).
Свежий импорт pipeline_clean под заглушками + pop из кэша на teardown = ноль
протечки. numpy реальный; obs — настоящий (app на PYTHONPATH), так что проверяем
реальные границы под-шагов (start/end/duration_ms).
"""

import importlib
import logging
import sys
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

from app.core.errors import (
    ArtifactMissingError,
    ModelLoadError,
    OcrError,
    PipelineError,
)
from app.core.logging import get_logger
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage

logger = get_logger(__name__)


@pytest.fixture
def pc(monkeypatch):
    """Изолированный свежий импорт pipeline_clean под заглушками (см. докстринг)."""
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())
    monkeypatch.setitem(sys.modules, "ultralytics", MagicMock())
    for _sib in ("text_detect_yolo", "recognize_surya", "text_filter"):
        monkeypatch.setitem(sys.modules, f"modules.ocr.{_sib}", MagicMock())

    sys.modules.pop("modules.ocr.pipeline_clean", None)
    mod = importlib.import_module("modules.ocr.pipeline_clean")
    yield mod
    sys.modules.pop("modules.ocr.pipeline_clean", None)


# --- 1. Листья ошибок, которыми типизируется OCR --------------------------------

def test_error_leaves_codes_and_hierarchy():
    assert OcrError("x").code == "ocr_failed"
    assert ModelLoadError("x").code == "model_load_failed"
    assert ArtifactMissingError("x").code == "artifact_missing"
    for cls in (OcrError, ModelLoadError, ArtifactMissingError):
        assert issubclass(cls, PipelineError)


# --- 2. Сбой OCR довозится до полей стадии (error_code/failed_step) --------------

def test_ocr_failure_reaches_stage_fields():
    exc = OcrError("surya boom", stage="ocr", step="recognize")
    stage = ProcessingStage()
    fail_stage(stage, str(exc)[:500], "TRACE", exc=exc)
    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "ocr_failed"
    assert stage.failed_step == "recognize"


# --- helpers для модульных тестов pipeline_clean --------------------------------

def _wire_happy(pc, monkeypatch):
    """Замокать вызовы run_ocr_pipeline_clean на минимально валидные возвраты."""
    monkeypatch.setattr(pc, "predict_tiled", lambda *a, **k: [[0, 0, 10, 10]])
    monkeypatch.setattr(pc, "merge_overlap", lambda boxes: boxes)
    monkeypatch.setattr(pc, "load_surya_recognizer", lambda *a, **k: object())
    monkeypatch.setattr(pc, "recognize_boxes", lambda *a, **k: [("TAG-1", 0.9)])
    monkeypatch.setattr(pc, "junk_reason", lambda *a, **k: None)
    monkeypatch.setattr(pc, "dedup", lambda items: (items, []))


def _run(pc, tmp_path):
    img = tmp_path / "image.png"
    img.write_bytes(b"x")
    model = tmp_path / "best.pt"
    model.write_bytes(b"x")
    return pc.run_ocr_pipeline_clean(img, tmp_path / "out", model, device="cpu")


# --- 3. happy path: 3 под-под-шага логируют start/end + duration_ms --------------

def test_pipeline_substeps_log_start_end_duration(pc, monkeypatch, caplog, tmp_path):
    _wire_happy(pc, monkeypatch)
    with caplog.at_level(logging.INFO):
        result = _run(pc, tmp_path)
    assert len(result["target"]) == 1
    ends = [r for r in caplog.records if getattr(r, "event", None) == "end"]
    names = {getattr(r, "step", None) for r in ends}
    assert {"text_detect", "recognize", "postfilter"} <= names
    # duration проставлен на конце каждого под-под-шага (DoD §4)
    assert all(isinstance(getattr(r, "duration_ms", None), int) for r in ends)


# --- 4. Сбой text_detect → OcrError(step=text_detect) ---------------------------

def test_text_detect_failure_typed_with_step(pc, monkeypatch, caplog, tmp_path):
    _wire_happy(pc, monkeypatch)
    monkeypatch.setattr(pc, "predict_tiled",
                        MagicMock(side_effect=RuntimeError("yolo boom")))
    with caplog.at_level(logging.INFO):
        with pytest.raises(OcrError) as ei:
            _run(pc, tmp_path)
    assert ei.value.step == "text_detect"
    assert ei.value.code == "ocr_failed"
    assert isinstance(ei.value.cause, RuntimeError)
    events = {(getattr(r, "step", None), getattr(r, "event", None))
              for r in caplog.records}
    assert ("text_detect", "start") in events


# --- 5. Сбой recognize → OcrError(step=recognize); text_detect уже закрылся ------

def test_recognize_failure_typed_with_step(pc, monkeypatch, caplog, tmp_path):
    _wire_happy(pc, monkeypatch)
    monkeypatch.setattr(pc, "recognize_boxes",
                        MagicMock(side_effect=RuntimeError("surya boom")))
    with caplog.at_level(logging.INFO):
        with pytest.raises(OcrError) as ei:
            _run(pc, tmp_path)
    assert ei.value.step == "recognize"
    assert ei.value.code == "ocr_failed"
    events = {(getattr(r, "step", None), getattr(r, "event", None))
              for r in caplog.records}
    assert ("text_detect", "end") in events  # движение до точки сбоя видно
    assert ("recognize", "start") in events


# --- 6. Предусловия: веса → ModelLoadError, образ → ArtifactMissingError ---------

def test_missing_weights_raise_model_load_error(pc, monkeypatch, tmp_path):
    _wire_happy(pc, monkeypatch)
    img = tmp_path / "image.png"
    img.write_bytes(b"x")
    with pytest.raises(ModelLoadError):
        pc.run_ocr_pipeline_clean(img, tmp_path / "out",
                                  tmp_path / "nonexistent.pt", device="cpu")


def test_unreadable_image_raise_artifact_missing(pc, monkeypatch, tmp_path):
    _wire_happy(pc, monkeypatch)
    monkeypatch.setattr(pc.cv2, "imread", lambda *a, **k: None)
    img = tmp_path / "image.png"
    img.write_bytes(b"x")
    model = tmp_path / "best.pt"
    model.write_bytes(b"x")
    with pytest.raises(ArtifactMissingError):
        pc.run_ocr_pipeline_clean(img, tmp_path / "out", model, device="cpu")
