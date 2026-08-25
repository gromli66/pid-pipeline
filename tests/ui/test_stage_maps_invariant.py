# -*- coding: utf-8 -*-
"""Блок 1 болей (pains-1, 2026-08-25): работа, ошибка и бусина ОДНОЙ задачи
обязаны указывать на ОДИН этап.

Боль 1 (Б16). Финальная скелетизация — задача `task_skeletonize_simple`
(`worker/tasks/skeleton.py:436`, `StageType.FINAL_SKELETONIZATION`) — льёт
проценты и глушит кнопку «Выделение труб» (`_STAGE_TYPE_TO_KEY`:
`final_skeletonization` -> `segment`), тогда как её бусина стоит на «Проверке
узлов» (`_BEAD_DEFS`: `SKELETONIZING_FINAL`/`SKELETONIZED_FINAL` — в наборе
бусины `junction`). Оператор смотрит на крутящуюся бусину одного этапа и на
заполняющуюся кнопку ДРУГОГО. Ошибка того же этапа красит ТРЕТЬЮ кнопку —
«Проверку труб» (`pipe`), потому что писатель `error_stage` хардкодит значение
соседней задачи (`skeletonizing_simple`, `skeleton.py:795,810`).

Почему решётка, а не три точечных проверки значений. Правка одной клетки —
заплатка: следующий этап приедет с тем же расхождением, и не заметит никто.
Здесь перебираются ВСЕ `StageType` (`app/models/stage.py`) против трёх карт
клиента, а связка «задача -> её значение `error_stage`» снимается `ast`-разбором
`worker/tasks/` при каждом прогоне: новый этап попадает в перебор сам, а не
после того, как о нём вспомнят (образец — `tests/ui/test_error_stage_contract.py`,
пункт 1-36).

Границы названы явно, и каждая — множеством, снятым перебором популяции, а не
выборкой:
* `upload` — единственный `StageType` без кнопки: этапа «Загрузка» в столбце
  нет вовсе (та же граница, что у решётки пункта 1.12);
* `direction_classification` и `layout` — этапы без СВОЕГО статуса диаграммы
  (направление встроено между валидацией детекции и сегментацией и статуса не
  меняет; раскладка считается за спиной оператора внутри `ocr_bound`), поэтому
  строки в `_STAGE_DONE_STATUS` у них нет и решётка бусин их не судит;
* писатели `error_stage` со стороны `app/**` здесь не разбираются: запись и
  создание строки стадии лежат у них в РАЗНЫХ функциях, пару взять не из чего.
  Их множество запирает решётка 1-36 (`test_api_writers_are_exactly_one`).
"""
import ast
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from app.models.stage import StageType                     # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

# Абсолютные числа обеих популяций на момент блока (2026-08-25). Новый этап
# обязан пройти через решётку, а не проскочить мимо неё молча.
STAGE_TYPE_COUNT = 16
TASKS_WITH_STAGE = 11
PAIRS_STAGE_TO_ERROR = 10

# `StageType` без кнопки: этап «Загрузка» в столбце оператора не показан.
NO_BUTTON = {"upload"}
# `StageType` без своего статуса диаграммы — бусину по `_STAGE_DONE_STATUS`
# им не сопоставить (обоснование в докстроке модуля).
NO_DONE_STATUS = {"upload", "direction_classification", "layout"}
# Задача без писателя `error_stage`: раскладка не зовёт `set_diagram_error`
# вовсе (её отказ виден только строкой стадии `failed`). Клетка зафиксирована,
# а не забыта: заведут писателя — число пар вырастет, и решётка скажет об этом.
NO_ERROR_WRITER = {"layout"}


# ── карты снимаются с кода, а не переписываются руками ───────────────────

def _fallback_map():
    """Карта `_STAGE_TO_KEY` (`error_stage` -> ключ кнопки).

    Локальна для `_update_buttons`, поэтому снимается разбором, а не импортом:
    иначе решётка судила бы по своей копии, а не по коду клиента.
    """
    source = (ROOT / "ui/widgets/diagram_workspace.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STAGE_TO_KEY":
                    return {k.value: v.value for k, v in
                            zip(node.value.keys, node.value.values)}
    raise AssertionError("_STAGE_TO_KEY не найдена в diagram_workspace.py")


def _task_pairs():
    """Связка «задача -> (её `StageType`, её значения `error_stage`)».

    Пара берётся ВНУТРИ одной функции: строку стадии заводит `start_stage(...,
    StageType.X)`, а значение пишет `set_diagram_error(db, uid, msg, "y")` в
    её же обработчике отказа. Читается разбором, а не импортом: `worker.tasks`
    тянет torch/cv2, которых на приёмке может не быть.
    """
    pairs = []
    for path in sorted((ROOT / "worker" / "tasks").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stypes, errors = set(), set()
            for node in ast.walk(fn):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "StageType"):
                    stypes.add(StageType[node.attr].value)
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", ""))
                if name != "set_diagram_error":
                    continue
                stage = node.args[3] if len(node.args) >= 4 else None
                for kw in node.keywords:
                    if kw.arg == "stage":
                        stage = kw.value
                if isinstance(stage, ast.Constant) and isinstance(stage.value, str):
                    errors.add(stage.value)
            if stypes:
                pairs.append((f"{path.name}:{fn.lineno}", sorted(stypes), sorted(errors)))
    return pairs


def _bead_key_of_status(status):
    """Бусина, которой принадлежит статус: первая, чей `completed_when` его накрывает.

    Так же читает статус и сам воркспейс: бусина считается пройденной, когда
    диаграмма дошла до её `completed_when` (`_beads_for_status`), значит статус
    ДО этого порога принадлежит ей же.
    """
    idx = dw._STATUS_IDX[status]
    for _bead_idx, key, completed_at, _in_progress, _available in dw._BEAD_DEFS:
        if dw._STATUS_IDX[completed_at] >= idx:
            return key
    return None


PAIRS = _task_pairs()
FALLBACK = _fallback_map()


# ── размер машины ────────────────────────────────────────────────────────

def test_machine_sizes_are_locked():
    """Перебор написан на популяции ровно такого размера."""
    assert len(list(StageType)) == STAGE_TYPE_COUNT
    assert len(PAIRS) == TASKS_WITH_STAGE, PAIRS


def test_every_task_owns_exactly_one_stage_type():
    """Пара берётся из одной функции — значит у функции ровно один `StageType`.

    Появится задача с двумя — связка перестанет быть однозначной, и об этом
    обязана сказать решётка, а не оператор.
    """
    for addr, stypes, _errors in PAIRS:
        assert len(stypes) == 1, f"{addr}: этапов в одной задаче {stypes}"


# ── решётка 1: у каждого этапа есть кнопка ───────────────────────────────

def test_every_stage_type_has_a_button():
    """Все `StageType`, кроме `upload`, названы в карте основного пути."""
    outside = sorted(s.value for s in StageType
                     if s.value not in dw._STAGE_TYPE_TO_KEY)
    assert set(outside) == NO_BUTTON, outside
    keys = {key for _idx, key, *_rest in dw._BEAD_DEFS}
    assert set(dw._STAGE_TYPE_TO_KEY.values()) <= keys


# ── решётка 2: бусина этапа = кнопка этапа ───────────────────────────────

def test_stage_done_status_belongs_to_the_same_bead():
    """ГЛАВНАЯ решётка боли 1: работа этапа и его бусина — один ключ.

    До правки не выполнялось на `final_skeletonization`: работа лилась в
    `segment`, а статус `SKELETONIZED_FINAL` принадлежит бусине `junction`.
    """
    for stage_type, done in dw._STAGE_DONE_STATUS.items():
        assert dw._STAGE_TYPE_TO_KEY[stage_type] == _bead_key_of_status(done), (
            f"{stage_type}: кнопка «{dw._STAGE_TYPE_TO_KEY[stage_type]}», "
            f"бусина «{_bead_key_of_status(done)}»"
        )


def test_stages_without_done_status_are_exactly_three():
    """Порог с другой стороны: решётка 2 не молчит про новый этап.

    Этап, у которого нет строки в `_STAGE_DONE_STATUS`, решёткой выше не
    судится вовсе — поэтому их перечень заперт абсолютным множеством.
    """
    missing = sorted(s.value for s in StageType
                     if s.value not in dw._STAGE_DONE_STATUS)
    assert set(missing) == NO_DONE_STATUS, missing


# ── решётка 3: ошибка этапа = кнопка этапа ───────────────────────────────

def test_error_of_a_task_paints_its_own_button():
    """Значение `error_stage` задачи ведёт на ТУ ЖЕ кнопку, что её работа.

    До правки не выполнялось на финальной скелетизации: работа — `segment`,
    ошибка — `pipe` (писатель хардкодит `skeletonizing_simple`).
    """
    checked = 0
    for addr, stypes, errors in PAIRS:
        key = dw._STAGE_TYPE_TO_KEY[stypes[0]]
        for error_stage in errors:
            assert error_stage in FALLBACK, (
                f"{addr}: клиент не знает значения '{error_stage}'"
            )
            assert FALLBACK[error_stage] == key, (
                f"{addr}: работа красит «{key}», ошибка '{error_stage}' — "
                f"«{FALLBACK[error_stage]}»"
            )
            checked += 1
    assert checked == PAIRS_STAGE_TO_ERROR, checked


def test_only_layout_has_no_error_writer():
    """Порог: задача без писателя `error_stage` — ровно одна и названа."""
    silent = sorted(stypes[0] for _addr, stypes, errors in PAIRS if not errors)
    assert set(silent) == NO_ERROR_WRITER, silent
