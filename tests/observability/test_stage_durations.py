"""
Тесты p50-агрегата бюджетов прогресса (мини-волна «бюджеты из БД», §5).

Юнитим `compute_stage_budgets` на фикстурных строках стадий (без живой БД —
тест-харнес SQLite, где `percentile_cont … WITHIN GROUP` недоступен, а async-
эндпоинт замокан; сам `SELECT` смотрим на смоук стенда). Критерии из §5:
исключения ручных стадий / только `completed` / окно 30 дней / фоллбэк < N=5.

Модуль грузится ПО ПУТИ, минуя `app/api/__init__.py` (тянет diagrams→storage→
aiofiles, §9 #2) — тот же приём, что в `test_cvat_stage_rows`/`test_progress_model`.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "api" / "stats.py"
_spec = importlib.util.spec_from_file_location("stats_api_under_test", _MODULE_PATH)
stats = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stats
_spec.loader.exec_module(stats)

compute_stage_budgets = stats.compute_stage_budgets

NOW = datetime(2026, 7, 9, 12, 0, 0)


def _row(stage_type, status="completed", duration_seconds=10.0, completed_at=NOW):
    """Строка как из SELECT: (stage_type, status, duration_seconds, completed_at)."""
    return (stage_type, status, duration_seconds, completed_at)


def _rows(stage_type, durations, **kw):
    return [_row(stage_type, duration_seconds=d, **kw) for d in durations]


# --- p50: медиана на стадию, робастна к выбросу -------------------------------

def test_p50_is_median_per_stage():
    # 5 наблюдений; выброс 100 не тянет медиану (в отличие от среднего).
    rows = _rows("detection", [10, 12, 14, 16, 100])
    budgets = compute_stage_budgets(rows, now=NOW)
    assert budgets == {"detection": 14.0}


def test_even_count_interpolates_like_percentile_cont():
    # Чётный N → середина интерполируется (== percentile_cont(0.5)): (30+40)/2=35;
    # 35 нет в списке → это интерполяция, а не выбор элемента.
    rows = _rows("segmentation", [10, 20, 30, 40, 50, 60])  # N=6 ≥ 5
    budgets = compute_stage_budgets(rows, now=NOW)
    assert budgets["segmentation"] == 35.0


# --- фоллбэк < N=5 → стадию опускаем (клиент берёт статический сид) ------------

def test_below_min_observations_is_omitted():
    rows = _rows("detection", [10, 12, 14, 16])  # N=4 < 5
    assert compute_stage_budgets(rows, now=NOW) == {}


def test_exactly_min_observations_included():
    rows = _rows("detection", [10, 12, 14, 16, 18])  # N=5
    assert compute_stage_budgets(rows, now=NOW) == {"detection": 14.0}


# --- только completed ---------------------------------------------------------

def test_only_completed_counted():
    # 5 completed (медиана 14) + running/failed/pending с длительностями — шум.
    rows = _rows("detection", [10, 12, 14, 16, 18])
    rows += [
        _row("detection", status="running", duration_seconds=1),
        _row("detection", status="failed", duration_seconds=999),
        _row("detection", status="pending", duration_seconds=0.5),
        _row("detection", status="skipped", duration_seconds=2),
    ]
    budgets = compute_stage_budgets(rows, now=NOW)
    assert budgets == {"detection": 14.0}  # 999 (failed) не сместил медиану


def test_status_enum_value_accepted():
    # SELECT по Enum-колонке отдаёт объект со .value — хелпер должен принять и его.
    class _S:
        value = "completed"
    rows = _rows("detection", [10, 12, 14, 16, 18], status=_S())
    assert compute_stage_budgets(rows, now=NOW) == {"detection": 14.0}


# --- окно 30 дней -------------------------------------------------------------

def test_out_of_window_rows_dropped():
    old = NOW - timedelta(days=40)
    # 5 свежих (медиана 14) + 3 старых с большими длительностями (вне окна).
    rows = _rows("detection", [10, 12, 14, 16, 18])
    rows += _rows("detection", [500, 600, 700], completed_at=old)
    budgets = compute_stage_budgets(rows, now=NOW)
    assert budgets == {"detection": 14.0}


def test_window_edge_and_shrinks_below_min():
    old = NOW - timedelta(days=40)
    # 4 свежих + 2 старых → в окне лишь 4 < 5 → стадию опускаем.
    rows = _rows("detection", [10, 12, 14, 16])
    rows += _rows("detection", [20, 22], completed_at=old)
    assert compute_stage_budgets(rows, now=NOW) == {}


def test_none_completed_at_dropped():
    rows = _rows("detection", [10, 12, 14, 16, 18], completed_at=None)
    assert compute_stage_budgets(rows, now=NOW) == {}


# --- исключение ручных/await-стадий -------------------------------------------

def test_manual_stages_excluded_even_with_enough_obs():
    rows = []
    for manual in ("frame_removal", "cvat_validation", "mask_validation", "graph_validation"):
        rows += _rows(manual, [30, 40, 50, 60, 70])  # N≥5, но ручные
    assert compute_stage_budgets(rows, now=NOW) == {}


def test_manual_excluded_but_auto_kept_in_mix():
    rows = _rows("detection", [10, 12, 14, 16, 18])
    rows += _rows("cvat_validation", [100, 200, 300, 400, 500])
    budgets = compute_stage_budgets(rows, now=NOW)
    assert budgets == {"detection": 14.0}  # ручная выкинута, авто осталась


# --- мусорные длительности ----------------------------------------------------

def test_nonpositive_and_null_durations_ignored():
    rows = _rows("detection", [10, 12, 14, 16, 18])
    rows += [
        _row("detection", duration_seconds=None),
        _row("detection", duration_seconds=0),
        _row("detection", duration_seconds=-5),
    ]
    assert compute_stage_budgets(rows, now=NOW) == {"detection": 14.0}


# --- пусто --------------------------------------------------------------------

def test_empty_rows_returns_empty():
    assert compute_stage_budgets([], now=NOW) == {}
