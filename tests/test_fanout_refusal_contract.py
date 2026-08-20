# -*- coding: utf-8 -*-
"""Веер `junctions/complete`: отказ, названный СЛОВАМИ, обязан быть назван и ПОЛЕМ.

Пункт 1-48 дороги. Дефект — red-team №9 ревизии связки 1.15+1.16 (`MEASUREMENTS §104з`).

Что не так. Параллельный OCR — единственная ветка веера, чей отказ отправки НЕ откатывает
состояние: задача сборки графа к этому моменту уже в брокере, и возврат осиротил бы её
(`app/api/validation.py`, граница заявлена в `STATUS_MACHINE §5`). Поэтому про отказ
эндпоинт говорит ПРОЗОЙ — англоязычным хвостом `ocr_note` в `message`, — а машинного поля
у отказа нет вовсе: `ocr_task_id: null` одинаков у ТРЁХ разных исходов (ушёл / выключен
конфигом / не ушёл). Различить их клиенту нечем, кроме подстроки в человеческом тексте,
и он не различает: `_on_junction_confirmed` читает из ответа только `task_id`.

⭐ Форма сторожа — образец пункта 1-41 (`MEASUREMENTS §112`): вместо перечня известных
случаев здесь РЕШЁТКА исходов веера, а список веток снимается `ast`-разбором самого
эндпоинта. Новая ветка отказа заводит новый литерал `ocr_note`, решётка его не наблюдает
и краснеет — то есть «сказать о ней клиенту» перестаёт быть тем, о чём надо помнить.

Числа абсолютные, ожидания — литералы (`PROTOCOL §3`: вычисленное из проверяемого
осталось бы зелёным при любом его значении).
"""
import ast
import asyncio
import uuid
from pathlib import Path

import pytest

from app.api.validation import complete_junction_validation
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus
from app.models.stage import StageType

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "app" / "api" / "validation.py"

UID = uuid.UUID("c1a55e77-1111-2222-3333-444455556666")

GRAPH_TASK = "worker.tasks.graph.task_build_graph"
OCR_TASK = "worker.tasks.ocr.task_run_ocr"

# Голова сообщения веера, к которой приклеивается хвост `ocr_note`. Литерал, а не
# вычисление из кода: подгонка ожидания под проверяемое — тот же класс, что «вход из
# проверяемой константы» (`PROTOCOL §3`).
PREFIX = "Junction validation completed, graph build started"


# ── поддельная БД: судится НАСТОЯЩАЯ корутина ────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _artifact(path):
    art = Artifact()
    art.file_path = path
    art.file_size = 1
    art.mime_type = "image/png"
    return art


# Обе validated-маски на месте: копирования файлов не будет, гейты артефактов пройдены.
ARTIFACTS = {
    ArtifactType.JUNCTION_MASK_VALIDATED: _artifact("junction_mask_validated.png"),
    ArtifactType.BRIDGE_MASK_VALIDATED: _artifact("bridge_mask_validated.png"),
}


class FakeDB:
    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        params = stmt.compile().params
        return _FakeResult(ARTIFACTS.get(params.get("artifact_type_1")))

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        pass

    async def flush(self):
        pass


def _diagram(status):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = status
    diagram.error_stage = None
    diagram.error_message = None
    diagram.project_code = "thermohydraulics"
    return diagram


def _set_ocr_enabled(monkeypatch, on: bool):
    """`tests/conftest.py` уводит конфиги проектов в пустой каталог, поэтому без
    подмены `_pc_ocr` всегда `None` и ветка OCR оказалась бы непройденной."""
    import app.services.project_loader as project_loader

    class _Ocr:
        enabled = on

    class _Config:
        ocr = _Ocr()

    class _Loader:
        def load(self, code):
            return _Config()

    monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())


def _set_broker(monkeypatch, dead_tasks=()):
    from worker.celery_app import celery_app

    class _AsyncResult:
        id = "task-0001"

    def _send_task(name, args=None, kwargs=None, **rest):
        if name in dead_tasks:
            raise OSError("[Errno 111] Connection refused")
        return _AsyncResult()

    monkeypatch.setattr(celery_app, "send_task", _send_task)


# ── решётка исходов веера ────────────────────────────────────────────────
#
# Четыре клетки — полное множество исходов, которые эндпоинт умеет вернуть с 200.
# Отказ САМОЙ сборки графа сюда не входит: он отвечает 503 и в ответе не живёт.
CELLS = ("started", "disabled", "failed", "already_past")


def _run_cell(cell, monkeypatch):
    """Прогнать клетку решётки настоящей корутиной и вернуть её ответ."""
    _set_ocr_enabled(monkeypatch, cell != "disabled")
    _set_broker(monkeypatch, dead_tasks=(OCR_TASK,) if cell == "failed" else ())
    status = (DiagramStatus.BUILT if cell == "already_past"
              else DiagramStatus.VALIDATING_JUNCTIONS)
    return asyncio.run(complete_junction_validation(UID, db=FakeDB(_diagram(status))))


def _note(result) -> str:
    """Хвост `ocr_note`, каким он доехал до клиента в человеческом тексте."""
    msg = result["message"]
    return msg[len(PREFIX):] if msg.startswith(PREFIX) else ""


def _note_literals() -> set:
    """Все строковые литералы, присваиваемые `ocr_note` ВНУТРИ эндпоинта.

    Разбором, а не перечнем: список веток обязан браться из кода, иначе новая
    ветка отказа проскочит мимо решётки молча (`PROTOCOL §3` — граница без
    команды, которой получен список, не граница).
    """
    tree = ast.parse(VALIDATION.read_text(encoding="utf-8"), filename=str(VALIDATION))
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "complete_junction_validation"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Assign):
                continue
            for target in inner.targets:
                if (isinstance(target, ast.Name) and target.id == "ocr_note"
                        and isinstance(inner.value, ast.Constant)
                        and isinstance(inner.value.value, str)):
                    found.add(inner.value.value)
    assert found, "литералы `ocr_note` не найдены — разбор смотрит не туда"
    return found


# ── сторож: решётка обязана наблюдать КАЖДУЮ ветку кода ──────────────────

def test_the_grid_observes_every_branch_of_the_note(monkeypatch):
    """Четыре ветки `ocr_note` в коде — четыре клетки решётки, ни одной мимо.

    Это и есть сторож вместо перечня: пятая ветка отказа, добавленная в веер,
    красит ЭТОТ тест, а не тихо уезжает к оператору необъявленной.
    """
    observed = {_note(_run_cell(cell, monkeypatch)) for cell in CELLS}
    assert observed == _note_literals()


def test_the_grid_is_exactly_four_cells(monkeypatch):
    """Абсолютное число: исходов веера с ответом 200 — ЧЕТЫРЕ."""
    assert len(CELLS) == 4
    assert len(_note_literals()) == 4


# ── Д1: проза и машинное поле не имеют права разойтись ───────────────────

def test_prose_and_machine_field_cannot_disagree(monkeypatch):
    """Сказал об отказе словами — обязан назвать его полем, и наоборот.

    До правки поля нет вовсе, поэтому клетка `failed` краснеет здесь первой.
    """
    for cell in CELLS:
        result = _run_cell(cell, monkeypatch)
        said_in_prose = "NOT started" in _note(result)
        named_in_field = bool(result.get("dispatch_failed"))
        assert said_in_prose == named_in_field, (
            f"клетка {cell}: проза {said_in_prose!r}, поле {named_in_field!r} — "
            f"ответ говорит об отказе только человеку")


def test_refused_leg_is_named_by_its_stage_type(monkeypatch):
    """Отказавший этап назван словарём `ProcessingStage.stage_type`.

    Словарь не произвольный: этой же картой (`_STAGE_TYPE_TO_KEY`) клиент уже
    читает `/stages`, поэтому перевод «этап → бусина» новой строки в клиенте
    не требует.
    """
    result = _run_cell("failed", monkeypatch)
    assert result["dispatch_failed"] == ["ocr"]
    assert set(result["dispatch_failed"]) <= {s.value for s in StageType}


def test_a_named_leg_really_has_no_task_id(monkeypatch):
    """Поле не имеет права назвать этап, задача которого всё-таки ушла."""
    result = _run_cell("failed", monkeypatch)
    assert "ocr" in result["dispatch_failed"]
    assert result["ocr_task_id"] is None
    assert result["task_id"] == "task-0001", "сборка графа обязана была уйти"


@pytest.mark.parametrize("cell", ["started", "disabled", "already_past"])
def test_a_leg_that_did_not_fail_is_never_named(cell, monkeypatch):
    """Обратная сторона: успех, выключенный конфигом и «ушли вперёд» — не отказ.

    Без этой половины сторож остался бы зелёным у ответа, который зовёт отказом
    ВСЁ подряд, — красная бусина стала бы такой же ложью, как молчание.
    """
    result = _run_cell(cell, monkeypatch)
    assert result.get("dispatch_failed") == []


def test_failure_still_does_not_roll_the_state_back(monkeypatch):
    """Заявленная граница §5 цела: отказ OCR ничего не откатывает.

    Замок с другой стороны — правка пункта не имеет права превратить «сказать
    правду» в «вернуть состояние»: сборка графа уже в брокере, возврат осиротил
    бы её.
    """
    _set_ocr_enabled(monkeypatch, True)
    _set_broker(monkeypatch, dead_tasks=(OCR_TASK,))
    diagram = _diagram(DiagramStatus.VALIDATING_JUNCTIONS)
    db = FakeDB(diagram)

    result = asyncio.run(complete_junction_validation(UID, db=db))

    assert diagram.status is DiagramStatus.VALIDATED_JUNCTIONS
    assert (diagram.error_stage, diagram.error_message) == (None, None)
    assert db.commits == 1, "состояние всё-таки вернули — граф остался сиротой"
    assert result["status"] == "validated_junctions"
