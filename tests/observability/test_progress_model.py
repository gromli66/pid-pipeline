"""
Тесты детерминированного прогресса клиента (Волна 2).

Критерии:
- проценты монотонны по факту завершённых стадий; 0..100; completed → 100;
- ETA считается на клиенте, динамична (тикает по elapsed) и калибруется по
  фактическим длительностям завершённых стадий этой же диаграммы;
- ручные/await-стадии не дают ETA; упавшая стадия → state=failed, прогресс
  до неё сохраняется.

Модуль грузится ПО ПУТИ, минуя ui/services/__init__.py (тот тянет PySide6/httpx),
чтобы тест был headless и не зависел от Qt.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "ui" / "services" / "progress_model.py"
)
_spec = importlib.util.spec_from_file_location("progress_model_under_test", _MODULE_PATH)
progress_model = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = progress_model
_spec.loader.exec_module(progress_model)

compute_progress = progress_model.compute_progress
format_eta = progress_model.format_eta

NOW = datetime(2026, 7, 8, 10, 0, 0)


def _stage(stage_type, status, started_at=None, duration_seconds=None):
    return {
        "stage_type": stage_type,
        "status": status,
        "started_at": started_at,
        "duration_seconds": duration_seconds,
    }


def test_empty_stages_is_idle():
    ps = compute_progress([], now=NOW)
    assert ps.state == "idle"
    assert ps.percent == 0
    assert ps.eta_seconds is None
    assert ps.running_stage is None


def test_running_auto_stage_partial_and_eta():
    stages = [
        _stage("upload", "completed", duration_seconds=5),
        _stage("detection", "running", started_at="2026-07-08T09:59:30"),  # elapsed 30s
    ]
    ps = compute_progress(stages, now=NOW)
    assert ps.state == "running"
    assert ps.running_stage == "detection"
    assert 0 < ps.percent < 100
    assert ps.eta_seconds is not None and ps.eta_seconds > 0
    assert ps.phase_label == "Поиск элементов"


def test_completed_pipeline_is_100():
    stages = [
        _stage("detection", "completed", duration_seconds=55),
        _stage("segmentation", "completed", duration_seconds=88),
        _stage("fxml_generation", "completed", duration_seconds=12),
    ]
    ps = compute_progress(stages, now=NOW)
    assert ps.percent == 100
    assert ps.state == "completed"
    assert ps.eta_seconds is None
    assert ps.phase_label == "Готово"


def test_failed_stage_preserves_progress():
    stages = [
        _stage("detection", "completed", duration_seconds=55),
        _stage("segmentation", "failed", started_at="2026-07-08T09:59:00"),
    ]
    ps = compute_progress(stages, now=NOW)
    assert ps.state == "failed"
    assert ps.failed_stage == "segmentation"
    assert ps.percent > 0  # прогресс до падения сохранён (detection зачтён)
    assert ps.eta_seconds is None


def test_manual_stage_running_has_no_eta():
    # cvat_validation — ручная (budget=None): в процессе, но ETA неизвестна.
    stages = [
        _stage("detection", "completed", duration_seconds=55),
        _stage("cvat_validation", "running", started_at="2026-07-08T09:58:00"),
    ]
    ps = compute_progress(stages, now=NOW)
    assert ps.state == "running"
    assert ps.running_stage == "cvat_validation"
    assert ps.eta_seconds is None
    assert ps.phase_label == "Проверка элементов"


def test_eta_ticks_down_over_time():
    stages = [_stage("detection", "running", started_at="2026-07-08T10:00:00")]
    early = compute_progress(stages, now=datetime(2026, 7, 8, 10, 0, 10))
    later = compute_progress(stages, now=datetime(2026, 7, 8, 10, 0, 40))
    assert early.eta_seconds is not None and later.eta_seconds is not None
    assert later.eta_seconds < early.eta_seconds  # динамично уменьшается


def test_percent_grows_as_stage_completes():
    running = compute_progress(
        [_stage("detection", "running", started_at="2026-07-08T09:59:30")], now=NOW
    )
    done = compute_progress(
        [_stage("detection", "completed", duration_seconds=60)], now=NOW
    )
    assert done.percent > running.percent


def test_calibration_slows_eta_on_slow_machine():
    started = "2026-07-08T10:00:00"
    baseline = compute_progress(
        [_stage("segmentation", "running", started_at=started)], now=NOW
    )
    # detection заняла вдвое дольше бюджета (60) → k≈2.0 → ETA больше.
    slow = compute_progress(
        [
            _stage("detection", "completed", duration_seconds=120),
            _stage("segmentation", "running", started_at=started),
        ],
        now=NOW,
    )
    assert slow.eta_seconds > baseline.eta_seconds


def test_budgets_override():
    stages = [_stage("detection", "running", started_at="2026-07-08T10:00:00")]
    default = compute_progress(stages, now=NOW)
    overridden = compute_progress(stages, now=NOW, budgets={"detection": 600})
    assert overridden.eta_seconds > default.eta_seconds


def test_percent_bounds_are_clamped():
    stages = [_stage("detection", "running", started_at="2000-01-01T00:00:00")]
    ps = compute_progress(stages, now=NOW)  # огромный elapsed
    assert 0 <= ps.percent <= 100


def test_format_eta():
    assert format_eta(None) == ""
    assert format_eta(-5) == ""
    assert format_eta(0) == "~0 с"
    assert format_eta(15) == "~15 с"
    assert format_eta(60) == "~1 мин"
    assert format_eta(80) == "~1 мин 20 с"
    assert format_eta(125) == "~2 мин 5 с"


def test_stage_percent_for_running_auto():
    # detection: elapsed 30s из бюджета 60 → 50% своей стадии (для заливки кнопки).
    stages = [_stage("detection", "running", started_at="2026-07-08T09:59:30")]
    ps = compute_progress(stages, now=NOW)
    assert ps.stage_percent == 50


def test_stage_percent_none_for_manual_and_idle():
    manual = compute_progress(
        [_stage("cvat_validation", "running", started_at="2026-07-08T09:59:00")], now=NOW
    )
    assert manual.stage_percent is None          # ручная стадия — без %
    assert compute_progress([], now=NOW).stage_percent is None  # нет бегущей
