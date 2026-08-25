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

import ast
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.models.stage import StageType

ROOT = Path(__file__).resolve().parents[2]

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


def test_parallel_running_each_stage_has_own_percent():
    """graph_building и ocr бегут ОДНОВРЕМЕННО → у каждой свой %; foreground=ранняя.

    Регресс §9 #15: раньше compute_progress брал ПОСЛЕДНЮЮ бегущую (ocr) →
    running_stage=ocr, кнопка graph не заливалась. Теперь foreground = самая
    ранняя (graph_building), а stage_percents несёт % для КАЖДОЙ.
    """
    stages = [
        _stage("graph_building", "running", started_at="2026-07-08T09:59:50"),  # elapsed 10s
        _stage("ocr", "running", started_at="2026-07-08T09:59:55"),             # elapsed 5s
    ]
    ps = compute_progress(stages, now=NOW)
    assert ps.state == "running"
    # foreground = самая ранняя бегущая, НЕ последняя
    assert ps.running_stage == "graph_building"
    # у каждой параллельной авто-стадии — своя процентовка
    assert ps.stage_percents is not None
    assert ps.stage_percents.get("graph_building", 0) > 0
    assert ps.stage_percents.get("ocr", 0) > 0
    # одиночный stage_percent соответствует foreground-стадии
    assert ps.stage_percent == ps.stage_percents["graph_building"]


def test_single_running_still_reports_stage_percents():
    """Одиночная бегущая стадия по-прежнему в stage_percents (обратная совместимость)."""
    stages = [_stage("detection", "running", started_at="2026-07-08T09:59:30")]
    ps = compute_progress(stages, now=NOW)
    assert ps.running_stage == "detection"
    assert ps.stage_percents.get("detection", 0) > 0
    assert ps.stage_percent == ps.stage_percents["detection"]


# ── канон пайплайна: решётка блока болей pains-1 (Б19) ───────────────────
#
# Канон обязан совпадать с тем, что конвейер РЕАЛЬНО заводит. Стадия-призрак
# держит вес в знаменателе и не завершается никогда, поэтому процент не доходит
# до 100 структурно (замер 0.9); стадия, которой в каноне нет, наоборот, не
# показывается вовсе. Множество снимается `ast`-разбором `app/**` и `worker/**`
# при каждом прогоне: новый этап попадает в решётку сам, а не после того, как
# о нём вспомнят.

CANON_SIZE = 13


def _stage_types_used_by_the_server():
    """Значения `StageType`, которые вообще упоминает код сервера и воркера."""
    used = set()
    for package in ("app", "worker"):
        for path in sorted((ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "StageType"):
                    used.add(StageType[node.attr].value)
    return used


def test_canon_is_exactly_what_the_server_writes():
    """Ни призраков, ни пропущенных этапов.

    До правки в каноне жили три призрака (`upload`, `mask_validation`,
    `graph_validation`) — строк с такими типами не заводит НИКТО, — а реальная
    `direction_classification` не значилась вовсе.
    """
    canon = list(progress_model._PIPELINE)
    assert len(canon) == len(set(canon)) == CANON_SIZE, canon
    assert set(canon) == _stage_types_used_by_the_server(), (
        sorted(set(canon) ^ _stage_types_used_by_the_server())
    )


def test_canon_order_follows_the_chain():
    """Порядок канона — порядок конвейера, иначе ETA врёт.

    Направление — первое звено цепочки `POST /segment`
    (`app/api/segmentation.py`: `chain(direction -> segment)`), а перекрёстки
    диспетчит САМА финальная скелетизация (`worker/tasks/skeleton.py`:
    `task_detect_junctions` уходит из `task_skeletonize_simple`).
    """
    canon = list(progress_model._PIPELINE)
    assert canon.index("direction_classification") < canon.index("segmentation")
    assert canon.index("final_skeletonization") < canon.index("junction_classification")


def test_labels_and_budgets_cover_the_canon_exactly():
    """Три карты канона — одно множество ключей, без сирот и без дыр."""
    canon = set(progress_model._PIPELINE)
    assert set(progress_model._STAGE_LABELS) == canon, (
        sorted(set(progress_model._STAGE_LABELS) ^ canon)
    )
    assert set(progress_model._DEFAULT_BUDGETS) == canon, (
        sorted(set(progress_model._DEFAULT_BUDGETS) ^ canon)
    )


def test_percent_reaches_the_end_of_the_canon():
    """Пройденный конвейер доходит до конца шкалы, а не упирается в призраков.

    Числа абсолютные и сняты прогоном (`MEASUREMENTS §P1`): весь канон, кроме
    экспорта, — **97 %** после правки против **92 %** до неё (знаменатель
    590 -> 555). Порог заперт с двух сторон: 100 здесь быть не может — экспорт
    не завершён, а его завершение процент форсирует отдельной веткой.
    """
    stages = [_stage(st, "completed", duration_seconds=1)
              for st in progress_model._PIPELINE if st != "fxml_generation"]
    ps = compute_progress(stages, now=NOW)
    assert 97 <= ps.percent < 100, ps.percent
