"""
Fault-тесты Волны 3 (detection): типизация под-под-шагов COMPUTE ансамбля.

§9 #7: под-под-шаги detection (inference по моделям / fusion) обёрнуты в
``obs.step()`` → в логах видно движение, а сбой типизируется с проставленным
``step`` (доезжает до ``failed_step`` в /stages). Тесты изолированы от cv2/torch:
cv2 подменяется MagicMock (RUNBOOK §9 #2), numpy — реальный; sahi/ensemble_boxes
не нужны (ленивы / замоканы).
"""

import logging
import sys
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

# cv2 отсутствует в тест-среде (§9 #2) → подменяем ДО импорта модуля детектора.
sys.modules.setdefault("cv2", MagicMock())

from app.core.errors import ConfigError, InferenceError, PipelineError
from modules.yolo_detector.ensemble import EnsembleDetector


# --- 1. Новый лист ошибки (config_invalid) ------------------------------------

def test_config_error_leaf():
    err = ConfigError("bad config", stage="detecting")
    assert err.code == "config_invalid"
    assert isinstance(err, PipelineError)
    assert not isinstance(err, InferenceError)


# --- helpers -------------------------------------------------------------------

def _ensemble(merge_strategy="wbf"):
    """EnsembleDetector с одной моделью и БЕЗ реальной загрузки весов."""
    return EnsembleDetector(
        models=[{"weights": "fake.pt", "tile_size": 640,
                 "weight": 1.0, "confidence": 0.5, "sahi_overlap": 0.25}],
        merge_strategy=merge_strategy,
        class_names={0: "node"},
    )


class _FakeDetector:
    """Заглушка NodeDetector: возвращает готовый результат или бросает exc."""

    def __init__(self, result=None, exc=None):
        self._result = result if result is not None else []
        self._exc = exc

    def detect(self, img, return_absolute=False, apply_reverse_mapping=False):
        if self._exc is not None:
            raise self._exc
        return list(self._result)


def _inject(det, fake):
    # обходим ленивую _load_detectors() (грузит реальные веса) — подсовываем фейк
    det._detectors = [(fake, det.models_config[0])]


_IMG = np.zeros((8, 8, 3), dtype=np.uint8)
_ONE_DET = {
    "class_id": 0, "x_center": 0.5, "y_center": 0.5,
    "width": 0.1, "height": 0.1, "confidence": 0.9,
}


# --- 2. inference: неожиданный сбой → InferenceError(step=inference) -----------

def test_inference_error_carries_step(caplog):
    det = _ensemble()
    _inject(det, _FakeDetector(exc=RuntimeError("cuda blew up")))

    with caplog.at_level(logging.INFO):
        with pytest.raises(InferenceError) as ei:
            det.detect(_IMG, apply_grayscale=False, apply_reverse_mapping=False)

    assert ei.value.step == "inference"
    assert ei.value.code == "inference_failed"
    # под-шаг начал логироваться до сбоя (видно движение по моделям, §9 #7)
    assert any(getattr(r, "step", None) == "inference"
               and getattr(r, "event", None) == "start" for r in caplog.records)


# --- 3. fusion: неизвестная стратегия → ConfigError(step=fusion) --------------

def test_unknown_merge_strategy_raises_config_error():
    det = _ensemble(merge_strategy="bogus")
    _inject(det, _FakeDetector(result=[]))

    with pytest.raises(ConfigError) as ei:
        det.detect(_IMG, apply_grayscale=False, apply_reverse_mapping=False)

    assert ei.value.step == "fusion"
    assert ei.value.code == "config_invalid"


# --- 4. fusion: неожиданный сбой merge → PipelineError(step=fusion) ------------

def test_fusion_error_wrapped_with_step():
    det = _ensemble(merge_strategy="wbf")
    _inject(det, _FakeDetector(result=[dict(_ONE_DET)]))
    det._merge_wbf = MagicMock(side_effect=RuntimeError("boom in wbf"))

    with pytest.raises(PipelineError) as ei:
        det.detect(_IMG, apply_grayscale=False, apply_reverse_mapping=False)

    assert ei.value.step == "fusion"
    # неожиданное обёрнуто в базовый PipelineError, не в под-тип
    assert ei.value.code == "pipeline_error"
    assert not isinstance(ei.value, (InferenceError, ConfigError))


# --- 5. happy path: обе границы под-шагов в логах (start+end+duration) ---------

def test_substeps_log_start_end_duration(caplog):
    det = _ensemble(merge_strategy="wbf")
    _inject(det, _FakeDetector(result=[]))
    det._merge_wbf = MagicMock(return_value=[])  # без реального ensemble_boxes

    with caplog.at_level(logging.INFO):
        out = det.detect(_IMG, apply_grayscale=False, apply_reverse_mapping=False)

    assert out == []
    ends = [r for r in caplog.records if getattr(r, "event", None) == "end"]
    names = {getattr(r, "step", None) for r in ends}
    assert {"inference", "fusion"} <= names
    # duration проставлен на конце под-шага (DoD §4)
    assert all(isinstance(getattr(r, "duration_ms", None), int) for r in ends)
