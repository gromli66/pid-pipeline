# -*- coding: utf-8 -*-
"""Подтверждение разметки CVAT: гейт статуса и исход отправки сегментации.

Зачем. Класс правки — [сма] (блок 2 плана точечных болей, Б8): `PROTOCOL §Гейты`
требует СНАЧАЛА зафиксировать текущие переходы и только потом править. У
`app/api/cvat.py::fetch_cvat_annotations` покрытия не было вовсе: греп
`fetch_cvat_annotations` по `tests/`/`tools/` давал только `tests/e2e_local.py`
(живой стек, счастливый путь). Без этой таблицы отличить рефакторинг от порчи
на единственном пути «вперёд» в `validated_bbox` нечем.

Что проверяется. Эндпоинт прогоняется по ВСЕМУ множеству `DiagramStatus`
(31 значение) плюс отдельные ветки: нет `cvat_task_id`, нет конфига проекта,
выгрузка упала, метки разошлись. Судится настоящая корутина: `AsyncSession`
подделана (её поверхность здесь — `execute`/`commit`/`add`/`flush`),
`_fetch_cvat_annotations_sync` и `_start_cvat_stage` подменены, `send_task` —
журнал вместо брокера. Живой БД, CVAT и брокера не нужно.

Числа и множества — абсолютные литералы, снятые ЧТЕНИЕМ кода, а не вычисленные
из него (`PROTOCOL §3`): вычисленное ожидание осталось бы зелёным при любом
значении проверяемого. Обе редакции («до Б8» и «после») лежат рядом
независимыми литералами — правка одной без другой краснит сторож ниже.
"""
import asyncio
import logging
import uuid

import pytest
from fastapi import HTTPException

import app.api.cvat as cvat_api
from app.core.errors import CVATLabelMismatchError
from app.core.logging import ContextFilter
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus
from app.models.stage import ProcessingStage

UID = uuid.UUID("d74eb9f1-9999-8888-7777-666655554444")

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча (`ast`-разбор `app/models/diagram.py`).
STATUS_COUNT = 31

# Единственный статус, из которого подтверждение проходит (`cvat.py:358`).
# Литерал снят чтением кода, не импортом: сторож ниже сверяет их между собой.
CONFIRM_ALLOWED = "validating_bbox"

DIRECTION_TASK = "worker.tasks.direction.task_classify_direction"
SEGMENT_TASK = "worker.tasks.segmentation.task_segment_pipes"

ANNOTATIONS = 42

# ── таблица «что делает подтверждение на счастливом пути» ────────────────
#
# (статус после, отправленная задача, коммитов). Коммита ДВА даже до Б8:
# первый делает `_start_cvat_stage` (RUNNING-строка видна в `/stages`, пока
# идёт долгая выгрузка), второй — запись артефактов и статуса.
#
# ДО Б8 (зафиксировано на нетронутом дереве, ветка road/pains-2): конвейер
# после подтверждения двигал ТОЛЬКО десктоп (`_on_cvat_confirmed` →
# `_start_segmentation`), сервер не отправлял ничего.
CONFIRM_BEFORE = ("validated_bbox", None, 2)

# ПОСЛЕ Б8: сегментацию ставит сервер тем же способом, что `/segment` —
# головой цепочки «направление → сегментация». Третий коммит — перевод
# в `segmenting` внутри диспетчера.
CONFIRM_AFTER = ("segmenting", DIRECTION_TASK, 3)

# Отказ отправки — best-effort: подтверждение рамки/разметки уже состоялось,
# и валить его мёртвым брокером нельзя. Состояние возвращается в `validated_bbox`,
# ответ 200, кнопка «Выделение труб» остаётся рабочей.
CONFIRM_AFTER_DEAD_BROKER = ("validated_bbox", DIRECTION_TASK, 4)


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуется эндпоинт.

    Ответ выбирается по СУЩНОСТИ запроса, а не по порядку вызовов. `delete(...)`
    сюда тоже приходит (upsert артефактов) — его считаем отдельно и молчим.
    """

    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0
        self.added = []
        self.deletes = 0

    async def execute(self, stmt):
        if not getattr(stmt, "is_select", False):
            self.deletes += 1
            return _FakeResult(None)
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        if entity is ProcessingStage:
            return _FakeResult(None)
        return _FakeResult(None)

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class FakeStage:
    """Строка `processing_stages` глазами эндпоинта: `complete`/`fail`."""

    def __init__(self):
        self.completed = 0
        self.failed = 0
        self.metrics = None

    def complete(self, metrics=None):
        self.completed += 1
        self.metrics = metrics

    def fail(self, *args, **kwargs):
        self.failed += 1


class Bench:
    """Один прогон подтверждения на свежем состоянии БД и диска."""

    def __init__(self, tmp_path, status):
        diagram = Diagram()
        diagram.uid = UID
        diagram.status = status
        diagram.cvat_task_id = 777
        diagram.project_code = "thermohydraulics"
        diagram.error_message = None
        diagram.error_stage = None
        self.diagram = diagram
        self.db = FakeDB(diagram)
        self.stages = []
        self.dispatched = []
        self.broker_down = False
        self.fetch_raises = None
        self.tmp_path = tmp_path
        self.result = None

    @property
    def stages_opened(self):
        return len(self.stages)

    @property
    def stages_completed(self):
        return sum(s.completed for s in self.stages)

    @property
    def stages_failed(self):
        return sum(s.failed for s in self.stages)

    def call(self):
        """Вызвать эндпоинт; вернуть http-код (200 — прошло без HTTPException)."""
        try:
            self.result = asyncio.run(cvat_api.fetch_cvat_annotations(UID, db=self.db))
            return 200
        except HTTPException as exc:
            self.result = None
            return exc.status_code


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Стенд: подменены хранилище, загрузчик конфига, выгрузка CVAT и брокер."""

    def _make(status, *, broker_down=False, fetch_raises=None, cvat_task_id=777,
              project_config=True):
        b = Bench(tmp_path, status)
        b.broker_down = broker_down
        b.fetch_raises = fetch_raises
        b.diagram.cvat_task_id = cvat_task_id

        monkeypatch.setattr(cvat_api.settings, "STORAGE_PATH", str(tmp_path))

        class _Detection:
            models = {"ensemble_v1": object()}
            default_model = "ensemble_v1"

        class _Config:
            detection = _Detection()

        class _Loader:
            def load(self, code):
                return _Config() if project_config else None

        import app.services.project_loader as project_loader
        monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())

        detection_dir = tmp_path / str(UID) / "detection"
        detection_dir.mkdir(parents=True, exist_ok=True)
        coco_path = detection_dir / "coco_validated.json"
        yolo_path = detection_dir / "yolo_validated.txt"

        def _fetch_sync(task_id, output_dir, config):
            if b.fetch_raises is not None:
                raise b.fetch_raises
            coco_path.write_text("{}", encoding="utf-8")
            yolo_path.write_text("0 0.5 0.5 0.1 0.1\n", encoding="utf-8")
            return coco_path, yolo_path, ANNOTATIONS

        async def _start_stage(db, uid):
            stage = FakeStage()
            b.stages.append(stage)
            await db.commit()
            return stage

        monkeypatch.setattr(cvat_api, "_fetch_cvat_annotations_sync", _fetch_sync)
        monkeypatch.setattr(cvat_api, "_start_cvat_stage", _start_stage)

        class _AsyncResult:
            id = "task-0001"

        def _send_task(name, args=None, kwargs=None, **rest):
            b.dispatched.append({"name": name, "args": args, "kwargs": kwargs or {}})
            if b.broker_down:
                raise OSError("[Errno 111] Connection refused")
            return _AsyncResult()

        from worker.celery_app import celery_app
        monkeypatch.setattr(celery_app, "send_task", _send_task)
        return b

    return _make


def _names(dispatched):
    return [c["name"] for c in dispatched]


# ── сторожа самой таблицы ────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана таблица."""
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_gate_literal_matches_code():
    """`CONFIRM_ALLOWED` — независимый литерал, но обязан совпадать с гейтом.

    Гейт таблица НЕ вычисляет из кода (иначе она осталась бы зелёной при любой
    его правке); этот сторож — единственное место, где они сверяются.
    """
    assert DiagramStatus(CONFIRM_ALLOWED) is DiagramStatus.VALIDATING_BBOX


def test_confirm_changed_by_exactly_the_declared_cells():
    """Б8 переписал ровно одну клетку счастливого пути и ничего сверх неё.

    Обе редакции — независимые литералы, поэтому правка одной без другой краснит
    этот сторож: «переход вне зафиксированного набора» (`PROTOCOL §Гейты`)
    ловится здесь, а не глазами ревизора.
    """
    assert CONFIRM_BEFORE == ("validated_bbox", None, 2)
    assert CONFIRM_AFTER == ("segmenting", DIRECTION_TASK, 3)
    assert CONFIRM_BEFORE != CONFIRM_AFTER, "правка не изменила ничего"
    # Отказ отправки возвращает РОВНО ту точку, из которой Б8 стартовал.
    assert CONFIRM_AFTER_DEAD_BROKER[0] == CONFIRM_BEFORE[0]
    assert CONFIRM_AFTER_DEAD_BROKER[2] == CONFIRM_AFTER[2] + 1


# ── полный перебор статусов ──────────────────────────────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_confirm_gate_over_every_status(status, bench):
    """Каждый статус машины: подтверждение проходит ровно из одного.

    Порог заперт с двух сторон: отказ дополнительно утверждает, что статус
    не сдвинут, транзакция не коммитилась, строка стадии не заведена и задача
    не отправлялась.
    """
    b = bench(status)
    code = b.call()

    if status.value != CONFIRM_ALLOWED:
        assert code == 400
        assert b.diagram.status is status, "статус сдвинут отказавшим эндпоинтом"
        assert b.db.commits == 0, "транзакция закоммичена при отказе"
        assert b.stages_opened == 0, "RUNNING-строка заведена при отказе"
        assert b.dispatched == [], "задача отправлена при отказе"
        return

    target, task, commits = CONFIRM_AFTER
    assert code == 200
    assert b.diagram.status is DiagramStatus(target)
    assert b.diagram.validated_detection_count == ANNOTATIONS
    assert b.db.commits == commits
    assert b.stages_opened == 1
    assert b.stages_completed == 1
    assert _names(b.dispatched) == ([task] if task else [])


def test_confirm_upserts_both_artifacts(bench):
    """Оба артефакта переписываются: старый удаляется, новый добавляется."""
    b = bench(DiagramStatus.VALIDATING_BBOX)
    assert b.call() == 200

    assert b.db.deletes == 2, "upsert не снял предыдущие артефакты"
    types = [a.artifact_type for a in b.db.added if isinstance(a, Artifact)]
    assert types == [ArtifactType.COCO_VALIDATED, ArtifactType.YOLO_VALIDATED]


def test_confirm_without_cvat_task_is_400(bench):
    """Нет задачи CVAT — 400 до всякой работы и без строки стадии."""
    b = bench(DiagramStatus.VALIDATING_BBOX, cvat_task_id=None)
    assert b.call() == 400
    assert b.diagram.status is DiagramStatus.VALIDATING_BBOX
    assert b.stages_opened == 0
    assert b.dispatched == []


def test_confirm_without_project_config_is_400(bench):
    """Нет конфига проекта — 400 до строки стадии."""
    b = bench(DiagramStatus.VALIDATING_BBOX, project_config=False)
    assert b.call() == 400
    assert b.diagram.status is DiagramStatus.VALIDATING_BBOX
    assert b.stages_opened == 0
    assert b.dispatched == []


def test_confirm_missing_diagram_is_404(bench):
    """Нет диаграммы — 404, а не 400 гейта."""
    b = bench(DiagramStatus.VALIDATING_BBOX)
    b.db.diagram = None
    assert b.call() == 404
    assert b.stages_opened == 0
    assert b.dispatched == []


# ── ветки отказа выгрузки ────────────────────────────────────────────────

def test_failed_export_moves_to_error(bench):
    """Выгрузка упала — диаграмма в `error`, стадия FAILED, задачи нет."""
    b = bench(DiagramStatus.VALIDATING_BBOX, fetch_raises=RuntimeError("boom"))
    assert b.call() == 500
    assert b.diagram.status is DiagramStatus.ERROR
    assert b.diagram.error_stage == "fetching_annotations"
    assert b.stages_failed == 1
    assert b.dispatched == [], "сегментация отправлена при упавшей выгрузке"


def test_label_mismatch_keeps_status(bench):
    """Метки разошлись — 400, статус НЕ уводится в `error`, задачи нет.

    Чинится в CVAT, после чего та же кнопка отработает из того же статуса.
    """
    b = bench(DiagramStatus.VALIDATING_BBOX,
              fetch_raises=CVATLabelMismatchError("метка 'кран' не найдена"))
    assert b.call() == 400
    assert b.diagram.status is DiagramStatus.VALIDATING_BBOX
    assert b.diagram.error_stage is None
    assert b.stages_failed == 1
    assert b.dispatched == []


# ── Б8: отказ отправки не валит подтверждение ────────────────────────────

class _Capture(logging.Handler):
    """Приёмник записей эндпоинта с настоящим `ContextFilter` на входе."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.addFilter(ContextFilter())


    def emit(self, record):
        self.records.append(record)


def test_dead_broker_keeps_the_confirmation(bench):
    """Брокер лёг: разметка ПОДТВЕРЖДЕНА, статус `validated_bbox`, ответ 200.

    Это и есть политика best-effort из Б9/Б8: отправка — не часть подтверждения.
    Оператор уходит с рабочей кнопкой «Выделение труб», а не с 503 на этапе,
    который на самом деле завершён.
    """
    b = bench(DiagramStatus.VALIDATING_BBOX, broker_down=True)
    assert b.call() == 200

    target, task, commits = CONFIRM_AFTER_DEAD_BROKER
    assert b.diagram.status is DiagramStatus(target)
    assert b.diagram.error_stage is None, "мёртвый брокер увёл диаграмму в error"
    assert b.diagram.validated_detection_count == ANNOTATIONS
    assert b.db.commits == commits
    assert _names(b.dispatched) == [task]
    assert b.stages_completed == 1, "стадия CVAT не закрыта из-за чужого отказа"


def test_dead_broker_leaves_a_trace_with_uid(bench):
    """Д2: отказ автозапуска не молчит — строка с `uid` и точкой возврата."""
    b = bench(DiagramStatus.VALIDATING_BBOX, broker_down=True)

    handler = _Capture()
    api_logger = logging.getLogger("app.api.cvat")
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        assert b.call() == 200
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    trace = [r for r in handler.records
             if getattr(r, "event", None) == "dispatch_failed"]
    assert len(trace) == 1, "отказ автозапуска сегментации не оставил следа"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == "cvat_validation"
    assert "validated_bbox" in trace[0].getMessage(), "точка возврата не названа"


def test_operator_button_still_works_after_a_dead_broker(bench, monkeypatch):
    """Сценарий ПОСЛЕ чужого действия: брокер лёг → оператор жмёт «Выделение труб».

    Прогон идёт не с чистого листа: сначала подтверждение с мёртвым брокером
    (состояние `validated_bbox` записал ОН), потом настоящая корутина
    `/segment` на том же объекте. Обещание Б8 «кнопка остаётся рабочей»
    проверяется тем же путём, которым по ней идёт клиент.
    """
    from app.api.segmentation import start_segmentation

    b = bench(DiagramStatus.VALIDATING_BBOX, broker_down=True)
    assert b.call() == 200
    assert b.diagram.status is DiagramStatus.VALIDATED_BBOX

    b.broker_down = False
    b.dispatched.clear()

    result = asyncio.run(start_segmentation(UID, db=b.db))
    assert result["status"] == "segmenting"
    assert b.diagram.status is DiagramStatus.SEGMENTING
    assert _names(b.dispatched) == [DIRECTION_TASK]


def test_repeated_confirm_does_not_dispatch_twice(bench):
    """Повтор подтверждения из `segmenting` — 400 и НИ ОДНОЙ новой отправки.

    Второй прогон идёт по состоянию, которое записал первый, а не с чистого
    листа: именно так дубль и родился бы, если бы гейт пускал повтор.
    """
    b = bench(DiagramStatus.VALIDATING_BBOX)
    assert b.call() == 200
    assert len(b.dispatched) == 1

    assert b.call() == 400, "повторное подтверждение прошло второй раз"
    assert len(b.dispatched) == 1, "второй прогон отправил дубль сегментации"


# ── Б8: клиентский путь больше не диспатчит ──────────────────────────────

def _workspace_source():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1]
            / "ui" / "widgets" / "diagram_workspace.py").read_text(encoding="utf-8")


def test_client_no_longer_dispatches_segmentation_after_cvat():
    """Дубль клиент+сервер закрыт: `_on_cvat_confirmed` сегментацию не ставит.

    Разбором `ast`, а не грепом по всему файлу: греп нашёл бы ВТОРОЙ вызов —
    кнопку «Выделение труб» в карте обработчиков, — и остался бы зелёным при
    вернувшемся дубле. Здесь судится ровно тело одного метода.

    Кнопка при этом обязана остаться: она нужна откатам, ERROR и случаю,
    когда у сервера не поднялся брокер (`test_dead_broker_keeps_the_confirmation`).
    Без второго утверждения «лечением» сошло бы удаление самого метода.
    """
    import ast

    tree = ast.parse(_workspace_source())
    confirmed = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_on_cvat_confirmed"
    ]
    assert len(confirmed) == 1, "метод подтверждения CVAT не один"

    called = {
        node.func.attr for node in ast.walk(confirmed[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_start_segmentation" not in called, (
        "клиент снова диспатчит сегментацию — задача уйдёт дважды")
    assert "_close_tab_and_restore_header" in called, (
        "закрытие вкладки ушло вместе с диспатчем: слежение за статусом не вернётся")

    # Кнопка «Выделение труб» жива и ведёт туда же, куда вела.
    handlers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_start_segmentation"
    ]
    assert len(handlers) == 1, "обработчик кнопки «Выделение труб» пропал"
    assert '"segment": self._start_segmentation' in _workspace_source(), (
        "кнопка «Выделение труб» отвязана от обработчика")
