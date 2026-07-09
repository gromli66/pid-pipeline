"""
Regress-тест §9 #13 (+ смежное [SKELETON_CONNECT]): информационные тайминг-логи
постобработки идут на INFO, не WARNING.

Контекст: смоук Волны 4 — `[POSTPROCESS] TOTAL: 0.49s` сыпалось как WARNING (шум
в docker logs). Волна понизила ВСЕ тайминги post_process_mask и
smart_skeleton_connect до INFO — тайминги под-шагов должны быть видны в штатных
логах (DoD §4), а не кричать WARNING.

Изоляция: postprocessing.py грузится НАПРЯМУЮ из файла через importlib — в обход
pipe_segmentation.inference.__init__ (тот тянет engine→data.dataset→torch.utils.data)
и БЕЗ записи заглушек в sys.modules["pipe_segmentation.inference.*"]. Это важно:
модуль-уровневые заглушки текут в сессию pytest и ломают другие тесты волн
(test_segmentation_errors стабает те же имена и ждёт НАСТОЯЩИЙ engine). Единственная
общая заглушка — cv2 (совместима: тот же MagicMock, что у соседей). numpy —
реальный; хелперы/скелетонизация замоканы monkeypatch'ем (тест про уровень лога,
не про морфологию).
"""

import importlib.util
import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

# modules/ на sys.path → внутренний `from pipe_segmentation.config.defaults import`
# резолвится (config.defaults — чистые константы, без cv2/torch).
_MODULES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "modules"))
if _MODULES not in sys.path:
    sys.path.insert(0, _MODULES)

# postprocessing.py делает `import cv2` — в тест-среде его нет (§9 #2). Заглушка
# cv2 совместима с другими тестами волн (тот же MagicMock).
sys.modules.setdefault("cv2", MagicMock())

# Грузим postprocessing.py по ФАЙЛОВОМУ пути под приватным именем: НЕ трогаем
# sys.modules["pipe_segmentation.inference.*"] → ноль протечки в другие тесты.
_PP_PATH = os.path.join(_MODULES, "pipe_segmentation", "inference", "postprocessing.py")
_spec = importlib.util.spec_from_file_location("_wave13_postprocessing_under_test", _PP_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


_POST = "[POSTPROCESS]"
_SKEL = "[SKELETON_CONNECT]"


def _tagged(caplog, tag):
    return [r for r in caplog.records if tag in r.getMessage()]


def test_postprocess_timing_logs_are_info(monkeypatch, caplog):
    """post_process_mask: Start / шаги / TOTAL — на INFO, ни одной WARNING."""
    # Чистые хелперы шагов → identity (обходим cv2/скелет; тест про уровень лога).
    monkeypatch.setattr(pp, "remove_small_components", lambda b, *a, **k: b)
    monkeypatch.setattr(pp, "remove_drawing_frame", lambda b, *a, **k: b)
    monkeypatch.setattr(pp, "smart_skeleton_connect", lambda b, *a, **k: b)
    monkeypatch.setattr(pp, "fill_mask_holes", lambda b, *a, **k: b)

    mask = np.zeros((8, 8), dtype=np.uint8)
    with caplog.at_level(logging.INFO):
        pp.post_process_mask(
            mask,
            remove_small_objects=1,                 # шаги 1 и 6
            closing_kernel_size=0,                  # cv2-морфология выкл (cv2 замокан)
            opening_kernel_size=0,
            remove_border_frame={"enabled": True},  # шаг 2
            skeleton_gap_fill={"enabled": True},    # шаг 3
            fill_holes={"enabled": True},           # шаг 5
        )

    recs = _tagged(caplog, _POST)
    assert recs, "нет [POSTPROCESS]-логов — путь функции изменился?"
    # Start + 1_remove_small + 2_remove_frame + 3_skeleton_gap_fill + 5_fill_holes
    # + 6_final_cleanup + TOTAL = 7 (шаг 4 cv2-морфологии выключен).
    assert len(recs) >= 6, f"мало под-шагов покрыто: {[r.getMessage() for r in recs]}"
    offenders = [(r.levelname, r.getMessage()) for r in recs if r.levelno != logging.INFO]
    assert not offenders, f"[POSTPROCESS] не на INFO (регресс #13): {offenders}"


def test_skeleton_connect_timing_logs_are_info(monkeypatch, caplog):
    """smart_skeleton_connect: тайминги skeletonize/find_endpoints — на INFO."""
    # Скелетонизация/endpoints замоканы: ранний выход после двух тайминг-логов.
    monkeypatch.setattr(pp, "_fast_skeletonize", lambda m: np.ones((8, 8), dtype=np.uint8))
    monkeypatch.setattr(pp, "_find_endpoints", lambda s: [])   # <2 endpoints → return

    with caplog.at_level(logging.INFO):
        pp.smart_skeleton_connect(np.ones((8, 8), dtype=np.uint8))  # sum=64 ≥ 50

    recs = _tagged(caplog, _SKEL)
    assert len(recs) >= 2, "ожидались skeletonize + find_endpoints тайминги"
    offenders = [(r.levelname, r.getMessage()) for r in recs if r.levelno != logging.INFO]
    assert not offenders, f"[SKELETON_CONNECT] не на INFO (регресс): {offenders}"
