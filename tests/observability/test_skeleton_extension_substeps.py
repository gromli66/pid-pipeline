"""
Fault-тесты §9 #14: под-под-шаги COMPUTE skeleton_extension (skeletonize / bfs).

§1: COMPUTE дробим «вокруг длинных циклов / заведомо медленного на CPU». В
skeleton_extension два кандидата — единичный тяжёлый skeletonize (skimage) и
итеративный BFS. Обёрнуты в obs.step (Вариант A, как engine.py/builder.py): в
логах start/end+duration_ms (движение фазы на CPU); сбой типизируется step'ом.
Задачный step=compute (worker/tasks/skeleton.py, §8.9) остаётся СНАРУЖИ: внешний
except в process_single_image ловит и возвращает False, поэтому гранулярный step
виден в ЛОГАХ, а failed_step в /stages задаёт задача (поведение как в §8.9).

Изоляция (§9 #2): cv2/skimage + skeleton_extension.core/.visualization → мок ДО
импорта processing (не тянем нативный стек); numpy реальный; core-функции
замоканы monkeypatch'ем на минимально валидные возвраты. Обёртка obs — настоящая
(app на PYTHONPATH), проверяем реальные start/end/duration/типизацию. Заглушки
sys.modules не конфликтуют: test_skeleton_errors skeleton_extension не импортит
(урок #13 — глобальные заглушки текут в сессию).
"""

import logging
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

_MODULES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "modules"))
if _MODULES not in sys.path:
    sys.path.insert(0, _MODULES)

# Нативные/тяжёлые зависимости отсутствуют в тест-среде (§9 #2) → заглушки ДО импорта.
sys.modules.setdefault("cv2", MagicMock())
if "skimage" not in sys.modules:
    _sk = types.ModuleType("skimage")
    _morph = types.ModuleType("skimage.morphology")
    _morph.skeletonize = lambda a: np.zeros(np.asarray(a).shape, dtype=bool)
    _sk.morphology = _morph
    sys.modules["skimage"] = _sk
    sys.modules["skimage.morphology"] = _morph
# core (1605 стр.) / visualization (264) тянут cv2/scipy и не нужны для теста
# инструментовки — их функции всё равно замоканы monkeypatch'ем.
sys.modules.setdefault("skeleton_extension.core", MagicMock())
sys.modules.setdefault("skeleton_extension.visualization", MagicMock())

import app.core.obs as obs
import skeleton_extension.processing as proc


def _z():
    return np.zeros((8, 8), dtype=np.uint8)


def _wire_happy(monkeypatch):
    """Минимально валидный simple_mode-путь (endpoints=[] → BFS-тело пропущено,
    но `with _obs_step("bfs")` исполняется и логирует start/end)."""
    cv2 = sys.modules["cv2"]
    monkeypatch.setattr(cv2, "imread", MagicMock(return_value=_z()))       # 2D → без cvtColor
    monkeypatch.setattr(cv2, "adaptiveThreshold", MagicMock(return_value=_z()))
    monkeypatch.setattr(cv2, "imwrite", MagicMock(return_value=True))
    monkeypatch.setattr(proc, "skeletonize",
                        lambda a: np.zeros(np.asarray(a).shape, dtype=bool))
    monkeypatch.setattr(proc, "remove_skeleton_under_nodes_simple", lambda *a, **k: (_z(), _z()))
    monkeypatch.setattr(proc, "create_simple_protection_mask", lambda *a, **k: (_z(), _z()))
    monkeypatch.setattr(proc, "find_skeleton_endpoints", lambda *a, **k: [])
    monkeypatch.setattr(proc, "connect_with_directed_lines", lambda *a, **k: (_z(), {}, set(), []))
    monkeypatch.setattr(proc, "create_final_protection_mask", lambda *a, **k: _z())


_CFG = {"simple_mode": True, "debug": False, "bfs_iterations": 1,
        "orphan_trim_length": 0, "remove_orphans": False}


def _call():
    return proc.process_single_image("o.png", "p.png", "n.png", "out.png", "skel.png", _CFG)


# --- 0. Обёртка obs — настоящая (не no-op fallback из standalone-ветки) ----------

def test_obs_layer_is_real():
    assert proc._obs_step is obs.step


# --- 1. happy path: skeletonize + bfs логируют start/end + duration_ms ----------

def test_substeps_log_start_end_duration(monkeypatch, caplog):
    _wire_happy(monkeypatch)

    with caplog.at_level(logging.INFO):
        result = _call()

    assert result is True

    ends = [r for r in caplog.records if getattr(r, "event", None) == "end"]
    names = {getattr(r, "step", None) for r in ends}
    assert {"skeletonize", "bfs"} <= names
    # duration_ms проставлен на конце каждого под-под-шага (DoD §4)
    assert all(
        isinstance(getattr(r, "duration_ms", None), int)
        for r in ends if getattr(r, "step", None) in {"skeletonize", "bfs"}
    )


# --- 2. Сбой в skeletonize: типизируется step'ом в ЛОГАХ, наружу — False ---------

def test_skeletonize_failure_typed_in_logs_and_swallowed(monkeypatch, caplog):
    _wire_happy(monkeypatch)
    monkeypatch.setattr(proc, "skeletonize",
                        MagicMock(side_effect=RuntimeError("skel boom")))

    with caplog.at_level(logging.INFO):
        result = _call()

    # внешний except(396) глотает → False (поведение как §8.9; задача поднимет
    # SkeletonizationError step=compute сама, /stages получает task-уровень)
    assert result is False
    # но obs.step УСПЕЛ типизировать под-под-шаг в логах: event=error, step=skeletonize
    errs = [r for r in caplog.records if getattr(r, "event", None) == "error"]
    assert any(getattr(r, "step", None) == "skeletonize" for r in errs)
    # skeletonize упал ДО bfs — bfs не стартовал
    events = {(getattr(r, "step", None), getattr(r, "event", None)) for r in caplog.records}
    assert ("skeletonize", "start") in events
    assert ("bfs", "start") not in events
