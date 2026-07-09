"""
Fault-тесты под-волны §8.4 batch 3 (junction): observability-контракт
task_detect_junctions + под-под-шаги COMPUTE в
modules/junction_segmentation/inference.py::run_inference.

Здесь: (1) листья ошибок, которыми задача/модуль типизируют raise, (2) fail_stage
довозит error_code/failed_step до /stages, (3) под-под-шаги
tiling/inference/extract_points логируют start/end+duration_ms и типизируют сбой
инференса в InferenceError/GpuOutOfMemoryError(step=inference).

ИЗОЛЯЦИЯ (§9 #2 / урок §8.12): заглушки torch/cv2/tqdm/sibling-модулей ставим
через ФИКСТУРУ `ji` с monkeypatch.setitem (функц. скоуп → авто-восстановление), а
НЕ на уровне модуля. Модуль-уровневый sys.modules-мок торчит всю сессию pytest и
роняет соседей: наш torch-мок (no_grad→passthrough) ломал engine.py сегментации на
`with torch.no_grad()` (TypeError: 'function' object ... context manager). Свежий
импорт inference под заглушками + pop из кэша на teardown = ноль протечки. numpy
реальный; obs настоящий (app на PYTHONPATH). Happy-path гоняем с 0 тайлов (torch-
тензоры не исполняются); сбои инференса поднимаем из get_tile_tensor ДО torch.stack.
"""

import importlib
import logging
import sys
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

# Контрактные тесты (листья/иерархия/fail_stage) — чистый app.core, без torch.
from app.core.errors import (
    ArtifactMissingError,
    ConfigError,
    GpuOutOfMemoryError,
    InferenceError,
    ModelLoadError,
    PipelineError,
)
from app.core.logging import get_logger
from app.models.stage import ProcessingStage, StageStatus
from worker.utils.db_helpers import fail_stage

logger = get_logger(__name__)


class _NoGradCM:
    """torch.no_grad() — и декоратор-passthrough (@no_grad() на run_inference),
    и контекст-менеджер (совместимо, если бы мок где-то протёк)."""

    def __call__(self, fn):
        return fn

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


@pytest.fixture
def ji(monkeypatch):
    """Изолированный свежий импорт run_inference под заглушками (см. модульный
    docstring). setitem — авто-восстановление; inference удаляем из кэша сами."""
    _torch = MagicMock()
    _torch.no_grad.return_value = _NoGradCM()
    _tqdm = MagicMock()
    _tqdm.tqdm = (lambda x, *a, **k: x)
    monkeypatch.setitem(sys.modules, "torch", _torch)
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())
    monkeypatch.setitem(sys.modules, "tqdm", _tqdm)
    for _sib in ("config", "dataset", "metrics", "model"):
        monkeypatch.setitem(sys.modules, f"modules.junction_segmentation.{_sib}", MagicMock())

    sys.modules.pop("modules.junction_segmentation.inference", None)
    mod = importlib.import_module("modules.junction_segmentation.inference")
    yield mod
    sys.modules.pop("modules.junction_segmentation.inference", None)


# --- 1. Листья ошибок, которыми типизируются raise задачи/модуля ----------------

def test_error_leaves_codes_and_hierarchy():
    assert ConfigError("x").code == "config_invalid"
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert ModelLoadError("x").code == "model_load_failed"
    assert InferenceError("x").code == "inference_failed"
    assert GpuOutOfMemoryError("x").code == "gpu_oom"
    assert issubclass(GpuOutOfMemoryError, InferenceError)
    for cls in (ConfigError, ArtifactMissingError, ModelLoadError, InferenceError):
        assert issubclass(cls, PipelineError)


# --- 2. Сбой инференса довозится до полей стадии (error_code/failed_step) --------

def test_inference_failure_reaches_stage_fields():
    exc = InferenceError("boom", stage="detecting_junctions", step="inference")
    stage = ProcessingStage()
    fail_stage(stage, str(exc)[:500], "TRACE", exc=exc)
    assert stage.status == StageStatus.FAILED
    assert stage.error_code == "inference_failed"
    assert stage.failed_step == "inference"


# --- helpers для модульных тестов run_inference ---------------------------------

class _FakeTiled:
    def __init__(self, n=0, exc=None):
        self._n = n
        self._exc = exc

    def __len__(self):
        return self._n

    def get_tile_tensor(self, i):
        if self._exc:
            raise self._exc
        return (MagicMock(), (0, 0))


def _run(ji, monkeypatch, tiled_n=0, tile_exc=None, extract_exc=None, tiling_exc=None):
    if tiling_exc is not None:
        monkeypatch.setattr(ji, "TiledInferenceDataset", MagicMock(side_effect=tiling_exc))
    else:
        monkeypatch.setattr(ji, "TiledInferenceDataset",
                            lambda *a, **k: _FakeTiled(tiled_n, tile_exc))
    if extract_exc is not None:
        monkeypatch.setattr(ji, "extract_local_maxima", MagicMock(side_effect=extract_exc))
    else:
        monkeypatch.setattr(ji, "extract_local_maxima", lambda *a, **k: [])
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    return ji.run_inference(
        MagicMock(), img, img[:, :, 0].copy(), img[:, :, 0].copy(), "cpu",
        tile_size=4, overlap=1, batch_size=2,
    )


# --- 3. happy path: 3 под-под-шага логируют start/end + duration_ms -------------

def test_run_inference_substeps_log_start_end_duration(ji, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        result = _run(ji, monkeypatch, tiled_n=0)
    assert result["junction_points"] == [] and result["bridge_points"] == []
    ends = [r for r in caplog.records if getattr(r, "event", None) == "end"]
    names = {getattr(r, "step", None) for r in ends}
    assert {"tiling", "inference", "extract_points"} <= names
    assert all(isinstance(getattr(r, "duration_ms", None), int) for r in ends)


# --- 4. Сбой tiling → PipelineError(step=tiling) --------------------------------

def test_tiling_failure_typed_with_step(ji, monkeypatch):
    with pytest.raises(PipelineError) as ei:
        _run(ji, monkeypatch, tiling_exc=RuntimeError("tiling boom"))
    assert ei.value.step == "tiling"
    assert isinstance(ei.value.cause, RuntimeError)


# --- 5. Сбой инференса → InferenceError(step=inference) --------------------------

def test_inference_failure_typed_with_step(ji, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        with pytest.raises(InferenceError) as ei:
            _run(ji, monkeypatch, tiled_n=1, tile_exc=RuntimeError("infer boom"))
    assert ei.value.step == "inference"
    assert ei.value.code == "inference_failed"
    assert not isinstance(ei.value, GpuOutOfMemoryError)
    events = {(getattr(r, "step", None), getattr(r, "event", None)) for r in caplog.records}
    assert ("tiling", "end") in events  # движение до точки сбоя видно


# --- 6. OOM → GpuOutOfMemoryError(step=inference) -------------------------------

def test_oom_failure_typed_as_gpu_oom(ji, monkeypatch):
    with pytest.raises(GpuOutOfMemoryError) as ei:
        _run(ji, monkeypatch, tiled_n=1, tile_exc=RuntimeError("CUDA out of memory"))
    assert ei.value.step == "inference"
    assert ei.value.code == "gpu_oom"


# --- 7. Сбой extract_points → PipelineError(step=extract_points) ----------------

def test_extract_points_failure_typed_with_step(ji, monkeypatch):
    with pytest.raises(PipelineError) as ei:
        _run(ji, monkeypatch, tiled_n=0, extract_exc=RuntimeError("extract boom"))
    assert ei.value.step == "extract_points"
