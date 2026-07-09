"""
Fault-тесты Волны 4 (segmentation): типизация под-под-шагов COMPUTE движка.

§1 / §8.6: COMPUTE tiled-инференса дробим на под-под-шаги ``tiling`` /
``inference`` / ``stitch`` (TTA свёрнута в ``inference`` — идёт покадрово внутри
``_run_inference``, отдельной границы нет). Инструментируем МОДУЛЬ (Вариант A,
как ensemble.py у detection): границы в логах + сбой инференса типизируется с
проставленным ``step`` (доезжает до ``failed_step`` в /stages).

Изоляция как в test_detection_errors (§9 #2): cv2/torch/tqdm → MagicMock ДО
импорта движка; numpy — реальный; тяжёлые pipe_segmentation-листья замоканы
monkeypatch'ем в namespace движка. Задачу (worker.tasks.segmentation) не тянем
— её типизированные raise проверяются smoke'ом на стенде (как detection).
"""

import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

# modules/ на sys.path → pipe_segmentation импортится как top-level (как в worker,
# где modules/ в PYTHONPATH; локальный pytest этого не делает).
_MODULES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "modules"))
if _MODULES not in sys.path:
    sys.path.insert(0, _MODULES)

# Нативные зависимости отсутствуют в тест-среде (§9 #2) → подменяем ДО импорта.
for _mod in ("cv2", "torch", "tqdm"):
    sys.modules.setdefault(_mod, MagicMock())

# Движок берёт из pipe_segmentation-листьев только функции, которые мы всё равно
# подменяем monkeypatch'ем. Регистрируем заглушки ДО импорта движка — иначе их
# __init__ тянут albumentations / segmentation_models_pytorch / torch.utils.data,
# которых в тест-среде нет. Так тест остаётся про инструментовку, а не про ML-стек.
for _mod in (
    "pipe_segmentation.data",
    "pipe_segmentation.data.tiling",
    "pipe_segmentation.inference.preprocessing",
    "pipe_segmentation.inference.postprocessing",
    "pipe_segmentation.inference.tta",
):
    sys.modules.setdefault(_mod, MagicMock())

from app.core.errors import (
    ArtifactMissingError,
    ConfigError,
    GpuOutOfMemoryError,
    InferenceError,
    ModelLoadError,
    PipelineError,
)
import pipe_segmentation.inference.engine as engine_mod
from pipe_segmentation.inference.engine import TiledInference


# --- 1. Листья ошибок, на которые опирается задача/движок ----------------------

def test_error_leaves_codes_and_hierarchy():
    # задача segmentation.py типизирует raise'ы этими листьями
    assert ModelLoadError("x").code == "model_load_failed"
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert ConfigError("x").code == "config_invalid"
    # OOM — под-тип InferenceError (для точного error_code на стадии)
    assert InferenceError("x").code == "inference_failed"
    assert GpuOutOfMemoryError("x").code == "gpu_oom"
    assert issubclass(GpuOutOfMemoryError, InferenceError)
    for cls in (ModelLoadError, ArtifactMissingError, ConfigError, InferenceError):
        assert issubclass(cls, PipelineError)


# --- helpers -------------------------------------------------------------------

_TILE = 4


def _engine(monkeypatch):
    """TiledInference с замоканными листьями pipe_segmentation (one-tile, cpu)."""
    # create_blending_mask зовётся в __init__ и участвует в numpy-блендинге stitch
    monkeypatch.setattr(engine_mod, "create_blending_mask",
                        lambda *a, **k: np.ones((_TILE, _TILE), dtype=np.float32))
    # один тайл в позиции (0,0) — цикл инференса делает ровно 1 проход
    monkeypatch.setattr(engine_mod, "calculate_tile_positions",
                        lambda *a, **k: [(0, 0)])
    monkeypatch.setattr(engine_mod, "extract_tile",
                        lambda *a, **k: np.zeros((_TILE, _TILE, 3), dtype=np.uint8))
    monkeypatch.setattr(engine_mod, "prepare_batch_from_tiles",
                        lambda *a, **k: MagicMock())  # .to(device) → MagicMock
    monkeypatch.setattr(engine_mod, "post_process_mask", lambda m, **k: m)

    return TiledInference(
        model=MagicMock(), device="cpu",
        tile_size=_TILE, overlap=0, batch_size=8,
        threshold=0.5, use_tta=False, binarize=False, in_channels=4,
    )


class _FakeTensor:
    """Мимикрия torch.Tensor для стежка: .cpu().numpy() → реальный ndarray."""

    def __init__(self, arr):
        self._arr = arr

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


_IMG = np.zeros((_TILE, _TILE, 3), dtype=np.uint8)
_NODE = np.zeros((_TILE, _TILE), dtype=np.uint8)


# --- 2. inference: неожиданный сбой → InferenceError(step=inference) -----------

def test_inference_failure_typed_with_step(monkeypatch, caplog):
    inst = _engine(monkeypatch)
    monkeypatch.setattr(inst, "_run_inference",
                        MagicMock(side_effect=RuntimeError("boom")))

    with caplog.at_level(logging.INFO):
        with pytest.raises(InferenceError) as ei:
            inst.predict(_IMG, _NODE, postprocess=False)

    assert ei.value.step == "inference"
    assert ei.value.code == "inference_failed"
    assert not isinstance(ei.value, GpuOutOfMemoryError)
    # tiling закрылся, inference стартовал до сбоя (на CPU видно движение фазы)
    events = {(getattr(r, "step", None), getattr(r, "event", None))
              for r in caplog.records}
    assert ("tiling", "end") in events
    assert ("inference", "start") in events


# --- 3. inference: OOM → GpuOutOfMemoryError(step=inference) -------------------

def test_oom_maps_to_gpu_oom(monkeypatch):
    inst = _engine(monkeypatch)
    monkeypatch.setattr(inst, "_run_inference",
                        MagicMock(side_effect=RuntimeError("CUDA out of memory")))

    with pytest.raises(GpuOutOfMemoryError) as ei:
        inst.predict(_IMG, _NODE, postprocess=False)

    assert ei.value.code == "gpu_oom"
    assert ei.value.step == "inference"


# --- 4. Уже типизированный сбой проходит насквозь (не двойная обёртка) ---------

def test_pipeline_error_passthrough(monkeypatch):
    inst = _engine(monkeypatch)
    boom = PipelineError("domain-fail", step="inference")
    monkeypatch.setattr(inst, "_run_inference", MagicMock(side_effect=boom))

    with pytest.raises(PipelineError) as ei:
        inst.predict(_IMG, _NODE, postprocess=False)

    # тот же объект: step сохранён, не обёрнут повторно в InferenceError
    assert ei.value is boom
    assert not isinstance(ei.value, InferenceError)


# --- 5. happy path: все три под-под-шага логируют start/end + duration ---------

def test_substeps_log_start_end_duration(monkeypatch, caplog):
    inst = _engine(monkeypatch)
    monkeypatch.setattr(
        inst, "_run_inference",
        lambda batch: _FakeTensor(np.zeros((1, 1, _TILE, _TILE), dtype=np.float32)),
    )

    with caplog.at_level(logging.INFO):
        result = inst.predict(_IMG, _NODE, postprocess=True)

    assert result["mask"].shape == (_TILE, _TILE)
    assert result["n_tiles"] == 1

    ends = [r for r in caplog.records if getattr(r, "event", None) == "end"]
    names = {getattr(r, "step", None) for r in ends}
    assert {"tiling", "inference", "stitch"} <= names
    # duration проставлен на конце каждого под-под-шага (DoD §4)
    assert all(isinstance(getattr(r, "duration_ms", None), int) for r in ends)
