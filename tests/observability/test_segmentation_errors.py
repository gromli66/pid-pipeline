"""
Fault-тесты Волны 4 (segmentation): типизация под-под-шагов COMPUTE движка.

§1 / §8.6: COMPUTE tiled-инференса дробим на под-под-шаги ``tiling`` /
``inference`` / ``stitch`` (TTA свёрнута в ``inference`` — идёт покадрово внутри
``_run_inference``, отдельной границы нет). Инструментируем МОДУЛЬ (Вариант A,
как ensemble.py у detection): границы в логах + сбой инференса типизируется с
проставленным ``step`` (доезжает до ``failed_step`` в /stages).

ИЗОЛЯЦИЯ (канон docs/TESTING.md §6, пункт 0.3x): cv2/torch/tqdm и листья
pipe_segmentation подменяются ФИКСТУРОЙ ``eng`` через monkeypatch.setitem, а НЕ на
уровне модуля. Модуль-уровневый ``sys.modules.setdefault`` исполняется на сборке
pytest и держит заглушки всю сессию — мок cv2 отсюда красил чужие тесты масок
(MEASUREMENTS §37), а заглушки ``pipe_segmentation.inference.*`` уводили соседей
от настоящего движка. Свежий импорт engine под заглушками + pop из кэша на
teardown = ноль протечки. numpy — реальный; тяжёлые pipe_segmentation-листья
замоканы monkeypatch'ем в namespace движка. Задачу (worker.tasks.segmentation) не
тянем — её типизированные raise проверяются smoke'ом на стенде (как detection).
"""

import importlib
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

# Движок берёт из этих листьев только функции, которые мы всё равно подменяем
# monkeypatch'ем. Без заглушек их __init__ тянут albumentations /
# segmentation_models_pytorch / torch.utils.data, которых в тест-среде нет: тест
# остаётся про инструментовку, а не про ML-стек.
_LEAVES = (
    "pipe_segmentation.data",
    "pipe_segmentation.data.tiling",
    "pipe_segmentation.inference.preprocessing",
    "pipe_segmentation.inference.postprocessing",
    "pipe_segmentation.inference.tta",
)

from app.core.errors import (
    ArtifactMissingError,
    ConfigError,
    GpuOutOfMemoryError,
    InferenceError,
    ModelLoadError,
    PipelineError,
)


@pytest.fixture
def eng(monkeypatch):
    """Изолированный свежий импорт движка под заглушками (см. докстринг)."""
    for _mod in ("cv2", "torch", "tqdm", *_LEAVES):
        monkeypatch.setitem(sys.modules, _mod, MagicMock())

    sys.modules.pop("pipe_segmentation.inference.engine", None)
    mod = importlib.import_module("pipe_segmentation.inference.engine")
    yield mod
    sys.modules.pop("pipe_segmentation.inference.engine", None)


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


def _engine(eng, monkeypatch):
    """TiledInference с замоканными листьями pipe_segmentation (one-tile, cpu)."""
    # create_blending_mask зовётся в __init__ и участвует в numpy-блендинге stitch
    monkeypatch.setattr(eng, "create_blending_mask",
                        lambda *a, **k: np.ones((_TILE, _TILE), dtype=np.float32))
    # один тайл в позиции (0,0) — цикл инференса делает ровно 1 проход
    monkeypatch.setattr(eng, "calculate_tile_positions",
                        lambda *a, **k: [(0, 0)])
    monkeypatch.setattr(eng, "extract_tile",
                        lambda *a, **k: np.zeros((_TILE, _TILE, 3), dtype=np.uint8))
    monkeypatch.setattr(eng, "prepare_batch_from_tiles",
                        lambda *a, **k: MagicMock())  # .to(device) → MagicMock
    monkeypatch.setattr(eng, "post_process_mask", lambda m, **k: m)

    return eng.TiledInference(
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

def test_inference_failure_typed_with_step(eng, monkeypatch, caplog):
    inst = _engine(eng, monkeypatch)
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

def test_oom_maps_to_gpu_oom(eng, monkeypatch):
    inst = _engine(eng, monkeypatch)
    monkeypatch.setattr(inst, "_run_inference",
                        MagicMock(side_effect=RuntimeError("CUDA out of memory")))

    with pytest.raises(GpuOutOfMemoryError) as ei:
        inst.predict(_IMG, _NODE, postprocess=False)

    assert ei.value.code == "gpu_oom"
    assert ei.value.step == "inference"


# --- 4. Уже типизированный сбой проходит насквозь (не двойная обёртка) ---------

def test_pipeline_error_passthrough(eng, monkeypatch):
    inst = _engine(eng, monkeypatch)
    boom = PipelineError("domain-fail", step="inference")
    monkeypatch.setattr(inst, "_run_inference", MagicMock(side_effect=boom))

    with pytest.raises(PipelineError) as ei:
        inst.predict(_IMG, _NODE, postprocess=False)

    # тот же объект: step сохранён, не обёрнут повторно в InferenceError
    assert ei.value is boom
    assert not isinstance(ei.value, InferenceError)


# --- 5. happy path: все три под-под-шага логируют start/end + duration ---------

def test_substeps_log_start_end_duration(eng, monkeypatch, caplog):
    inst = _engine(eng, monkeypatch)
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
