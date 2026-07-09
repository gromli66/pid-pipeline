"""
Волна B (клиент): substep_status_line — строка «Стадия · под-шаг» для статусбара.

Чистый Python (без Qt), как progress_model: headless. Правила:
- самая ранняя по пайплайну БЕГУЩАЯ АВТО-стадия с current_step;
- ручные стадии не показываем (ждём оператора);
- нет current_step / нет бегущих / пусто → "" (совместимость со старым сервером);
- неизвестное имя шага — как есть (тех-имя из логов).

Модуль грузится ПО ПУТИ, минуя ui/services/__init__.py (тот тянет PySide6/httpx),
как в test_progress_model — headless, без Qt.
"""

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "ui" / "services" / "progress_model.py"
)
_spec = importlib.util.spec_from_file_location("substep_line_under_test", _MODULE_PATH)
_pm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _pm
_spec.loader.exec_module(_pm)

substep_status_line = _pm.substep_status_line


def _row(st, status="running", step=None):
    return {"stage_type": st, "status": status, "current_step": step}


def test_line_for_running_auto_stage():
    line = substep_status_line([_row("segmentation", step="inference")])
    assert line == "Выделение труб · прогон модели"


def test_unknown_step_shown_as_is():
    line = substep_status_line([_row("segmentation", step="warp42")])
    assert line == "Выделение труб · warp42"


def test_manual_stage_hidden():
    assert substep_status_line([_row("cvat_validation", step="await")]) == ""


def test_empty_cases():
    assert substep_status_line([_row("segmentation", step=None)]) == ""  # старый сервер
    assert substep_status_line([_row("segmentation", status="completed", step="x")]) == ""
    assert substep_status_line([]) == ""
    assert substep_status_line(None) == ""


def test_parallel_prefers_earliest_auto_with_step():
    rows = [_row("ocr", step="recognize"), _row("graph_building", step="trace_edges")]
    assert substep_status_line(rows) == "Сборка схемы · трассировка линий"


def test_falls_through_to_parallel_stage_when_first_has_no_step():
    rows = [_row("graph_building", step=None), _row("ocr", step="recognize")]
    assert substep_status_line(rows) == "Распознавание текста · распознавание"


def test_manual_running_does_not_shadow_later_auto():
    # оператор держит graph_validation, параллельно бежит OCR — показываем OCR
    rows = [_row("graph_validation", step=None), _row("ocr", step="text_detect")]
    assert substep_status_line(rows) == "Распознавание текста · поиск текста"
