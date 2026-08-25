# -*- coding: utf-8 -*-
"""Стадия «Очистка рамки» — таблица переходов `app/api/frame.py` (пункт 1.17 дороги).

Зачем. Класс правки — [сма], стейт-машина: `PROTOCOL §Гейты` требует СНАЧАЛА
зафиксировать текущие переходы, и только потом править. У `app/api/frame.py`
покрытия нет вовсе (грепом по `tests/`/`tools/` — только `tests/e2e_local.py`,
который гоняет счастливый путь на живом стеке), поэтому без этой таблицы
отличить рефакторинг от порчи здесь нечем.

Что проверяется. ВСЕ ЧЕТЫРЕ эндпоинта файла — `/start`, `/save`, `/complete`,
`/skip` — прогоняются по ВСЕМУ множеству `DiagramStatus` (31 значение, снято
`ast`-разбором `app/models/diagram.py`; `docs/GLOSSARY.md` и `docs/UI_GUIDE.md`
пишут 29 — это долг пункта 0.6). Судится настоящая корутина эндпоинта:
`AsyncSession` подделана (её поверхность здесь — `execute`/`commit`/`delete`/
`flush`/`add`), `StorageService` подменён на временный каталог, хелперы
`ProcessingStage` — на журнал вызовов. Живой БД, брокера и `storage/` не нужно.

Клетка таблицы — не только «пустил / не пустил»: за каждым прогоном снимается
статус ПОСЛЕ, судьба маркера `ORIGINAL_CLEANED`, содержимое `original/image.png`
и `original/image_raw.png` и число открытых/закрытых RUNNING-строк стадии.
Пункт 1.17 — про потерю очистки, а её видно только по файлам и артефакту.

Числа и множества — абсолютные литералы, снятые ЧТЕНИЕМ кода, а не вычисленные
из него: иначе набор остался бы зелёным при любом значении гейта (`PROTOCOL §3`).
Порог заперт с двух сторон — каждый отказ дополнительно утверждает, что статус
не сдвинут, транзакция не коммитилась и ни один файл не тронут.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.api.frame as frame_api
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("d74eb9f1-1111-2222-3333-444455556666")

# Размер машины. Абсолютное число: новый статус обязан пройти через эту таблицу,
# а не проскочить мимо неё молча.
STATUS_COUNT = 31

# Единственный гейт всех четырёх эндпоинтов — `_FRAME_EDITABLE` (`frame.py:57-61`).
# Литералы сняты чтением кода, не импортом кортежа: сторож ниже сверяет их с ним.
EDITABLE = frozenset({"uploaded", "cleaning_frame", "frame_cleaned"})

START_ALLOWED = EDITABLE
SAVE_ALLOWED = EDITABLE
COMPLETE_ALLOWED = EDITABLE
SKIP_ALLOWED = EDITABLE

DETECT_TASK = "worker.tasks.detection.task_detect_yolo"

# ── что делает подтверждение этапа (Б9, блок 2 плана точечных болей) ─────
#
# (статус ПОСЛЕ, отправленная задача, коммитов). Обе редакции — независимые
# литералы, сторож ниже сверяет их между собой: правка одной без другой краснит
# «переход вне зафиксированного набора» (`PROTOCOL §Гейты`) здесь, а не глазами
# ревизора.
#
# ДО Б9: конвейер после рамки двигал только десктоп — клиент жал «Поиск
# элементов» сам, а закрытая вкладка останавливала схему навсегда.
CONFIRM_BEFORE = ("frame_cleaned", None, 1)
# ПОСЛЕ Б9: детекцию ставит сервер. Второй коммит — перевод в `detecting`
# внутри диспетчера; модель выбирает сама задача (`model_id=None`).
CONFIRM_AFTER = ("detecting", DETECT_TASK, 2)
# Мёртвый брокер: подтверждение НЕ валится, состояние возвращается ровно
# в ту точку, из которой Б9 стартовал, ответ 200. Третий коммит — возврат.
CONFIRM_AFTER_DEAD_BROKER = ("frame_cleaned", DETECT_TASK, 3)

# `/skip` из уже завершённого этапа. ДО Б9 повтор переигрывал работу: сносил
# сохранённую очистку и бэкап (замер 1.3 — жестом это недостижимо, клиент
# `skip_frame_removal` не зовёт, но клетка машины допускала). ПОСЛЕ Б9 такой
# повтор ещё и передиспатчил бы детекцию, поэтому заведён ранний выход,
# симметричный `/complete:187-188`.
SKIP_FROM_DONE_BEFORE = "repeat"
SKIP_FROM_DONE = "early_exit"

RAW = b"RAW-ORIGINAL"
CLEANED = b"CLEANED-PNG"
UPLOAD = b"UPLOADED-PNG"


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession`, которой пользуются четыре эндпоинта."""

    def __init__(self, diagram, cleaned=None):
        self.diagram = diagram
        self.cleaned = cleaned
        self.commits = 0
        self.deleted = []
        self.added = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        return _FakeResult(self.diagram if entity is Diagram else self.cleaned)

    async def commit(self):
        self.commits += 1

    async def delete(self, obj):
        self.deleted.append(obj)
        if obj is self.cleaned:
            self.cleaned = None

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Artifact):
            self.cleaned = obj


class FakeStage:
    """Строка `processing_stages` глазами эндпоинта: только `complete`/`fail`."""

    def __init__(self):
        self.completed = 0
        self.failed = 0

    def complete(self):
        self.completed += 1

    def fail(self, *args, **kwargs):
        self.failed += 1


class FakeUpload:
    """`UploadFile` в объёме, который читает `/save`."""

    def __init__(self, content=CLEANED, content_type="image/png"):
        self._content = content
        self.content_type = content_type

    async def read(self):
        return self._content


class Bench:
    """Один прогон эндпоинта на свежем состоянии диска и БД."""

    def __init__(self, root, status, *, cleaned_saved, raw_backup, canon):
        self.root = root
        self.orig = root / str(UID) / "original"
        self.orig.mkdir(parents=True, exist_ok=True)
        if canon is not None:
            (self.orig / "image.png").write_bytes(canon)
        if raw_backup is not None:
            (self.orig / "image_raw.png").write_bytes(raw_backup)

        diagram = Diagram()
        diagram.uid = UID
        diagram.status = status
        diagram.error_message = "прошлая ошибка"
        diagram.error_stage = "frame_removal"
        self.diagram = diagram

        artifact = None
        if cleaned_saved:
            artifact = Artifact()
            artifact.diagram_uid = UID
            artifact.artifact_type = ArtifactType.ORIGINAL_CLEANED
            artifact.file_path = f"{UID}/original/image.png"
        self.db = FakeDB(diagram, artifact)

        self.opened = []          # RUNNING-строки, заведённые за прогон
        self.running = None       # текущая открытая строка (её вернёт get_running)
        self.dispatched = []      # журнал отправленных задач вместо брокера
        self.broker_down = False

    # наблюдения ---------------------------------------------------------
    @property
    def canon(self):
        path = self.orig / "image.png"
        return path.read_bytes() if path.exists() else None

    @property
    def raw(self):
        path = self.orig / "image_raw.png"
        return path.read_bytes() if path.exists() else None

    @property
    def cleaned_artifact(self):
        return self.db.cleaned

    @property
    def stages_opened(self):
        return len(self.opened)

    @property
    def stages_completed(self):
        return sum(s.completed for s in self.opened)

    @property
    def tasks(self):
        return [c["name"] for c in self.dispatched]

    def call(self, endpoint, upload=None):
        """Вызвать эндпоинт; вернуть http-код (200 — прошло без HTTPException)."""
        coro = {
            "start": lambda: frame_api.start_frame_removal(UID, db=self.db),
            "save": lambda: frame_api.save_cleaned_image(
                UID, file=upload or FakeUpload(), db=self.db),
            "complete": lambda: frame_api.complete_frame_removal(UID, db=self.db),
            "skip": lambda: frame_api.skip_frame_removal(UID, db=self.db),
        }[endpoint]
        try:
            self.result = asyncio.run(coro())
            return 200
        except HTTPException as exc:
            self.result = None
            return exc.status_code


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Стенд: подменённые `StorageService`, хелперы стадии и брокер.

    Подмена `send_task` ОБЯЗАТЕЛЬНА, а не для удобства: с Б9 подтверждение
    этапа само ставит детекцию, и перебор по 31 статусу без неё долбил бы
    живой брокер (которого в тест-среде нет — прогон вис бы на retry-цикле).
    """

    def _make(status, *, cleaned_saved=False, raw_backup=None, canon=RAW,
              broker_down=False):
        b = Bench(tmp_path, status, cleaned_saved=cleaned_saved,
                  raw_backup=raw_backup, canon=canon)
        b.broker_down = broker_down

        class _AsyncResult:
            id = "task-0001"

        def _send_task(name, args=None, kwargs=None, **rest):
            b.dispatched.append({"name": name, "args": args, "kwargs": kwargs or {}})
            if b.broker_down:
                raise OSError("[Errno 111] Connection refused")
            return _AsyncResult()

        from worker.celery_app import celery_app
        monkeypatch.setattr(celery_app, "send_task", _send_task)

        class FakeStorage:
            base_path = tmp_path

            async def save_file(self, uid, stage, name, content):
                directory = tmp_path / str(uid) / stage
                directory.mkdir(parents=True, exist_ok=True)
                (directory / name).write_bytes(content)
                return f"{uid}/{stage}/{name}", len(content)

        async def fake_start(db, uid):
            stage = FakeStage()
            b.opened.append(stage)
            b.running = stage
            return stage

        async def fake_get_running(db, uid):
            return b.running

        monkeypatch.setattr(frame_api, "StorageService", FakeStorage)
        monkeypatch.setattr(frame_api, "start_frame_stage", fake_start)
        monkeypatch.setattr(frame_api, "get_running_frame_stage", fake_get_running)
        return b

    return _make


# ── сторожа самой таблицы ────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    """Машина ровно того размера, на который написана таблица.

    31, а не 29: число снято `ast`-разбором `app/models/diagram.py`. Доки
    (`GLOSSARY §`, `UI_GUIDE §`, `CLAUDE.md`) пишут 29 — их чинит пункт 0.6.
    """
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_gate_literal_matches_code():
    """`EDITABLE` — независимый литерал, но обязан совпадать с кортежем кода.

    Сам гейт таблица НЕ вычисляет из `_FRAME_EDITABLE` (иначе она осталась бы
    зелёной при любой его правке); этот сторож — единственное место, где они
    сверяются, и он краснеет ровно тогда, когда гейт двинули, а таблицу нет.
    """
    assert {s.value for s in frame_api._FRAME_EDITABLE} == EDITABLE


def test_table_keys_name_real_statuses():
    """Ключ таблицы — существующий статус, а не опечатка."""
    for value in EDITABLE:
        assert DiagramStatus(value).value == value
    for target, _task, _commits in (CONFIRM_BEFORE, CONFIRM_AFTER,
                                    CONFIRM_AFTER_DEAD_BROKER):
        assert DiagramStatus(target).value == target


def test_confirm_changed_by_exactly_the_declared_cells():
    """Б9 переписал ровно исход подтверждения этапа и ничего сверх того.

    Обе редакции — независимые литералы, поэтому правка одной без другой
    краснит этот сторож: «переход вне зафиксированного набора»
    (`PROTOCOL §Гейты`) ловится здесь, а не глазами ревизора.
    """
    assert CONFIRM_BEFORE == ("frame_cleaned", None, 1)
    assert CONFIRM_AFTER == ("detecting", DETECT_TASK, 2)
    assert CONFIRM_BEFORE != CONFIRM_AFTER, "правка не изменила ничего"

    # Отказ отправки возвращает РОВНО ту точку, из которой Б9 стартовал,
    # и она обязана лежать внутри гейта кнопки «Поиск элементов».
    assert CONFIRM_AFTER_DEAD_BROKER[0] == CONFIRM_BEFORE[0]
    assert CONFIRM_AFTER_DEAD_BROKER[2] == CONFIRM_AFTER[2] + 1
    assert CONFIRM_AFTER_DEAD_BROKER[0] in EDITABLE

    assert SKIP_FROM_DONE_BEFORE == "repeat"
    assert SKIP_FROM_DONE == "early_exit"
    assert SKIP_FROM_DONE != SKIP_FROM_DONE_BEFORE


# ── POST /{uid}/start — полный перебор статусов ──────────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_start_gate_over_every_status(status, bench):
    """Каждый статус машины: `/start` пропускает ровно те, что в таблице."""
    b = bench(status)
    code = b.call("start")

    if status.value not in START_ALLOWED:
        assert code == 400
        assert b.diagram.status is status, "статус сдвинут отказавшим эндпоинтом"
        assert b.db.commits == 0, "транзакция закоммичена при отказе"
        assert b.stages_opened == 0, "RUNNING-строка заведена при отказе"
        assert b.canon == RAW and b.raw is None, "файлы тронуты при отказе"
        return

    assert code == 200
    if status is DiagramStatus.UPLOADED:
        # Единственная клетка, где переход реально происходит.
        assert b.diagram.status is DiagramStatus.CLEANING_FRAME
        assert b.diagram.error_message is None
        assert b.diagram.error_stage is None
        assert b.db.commits == 1
        assert b.stages_opened == 1, "RUNNING-строка стадии не заведена"
    else:
        # cleaning_frame и frame_cleaned: эндпоинт не делает НИЧЕГО.
        assert b.diagram.status is status
        assert b.db.commits == 0
        assert b.stages_opened == 0
    assert b.canon == RAW and b.raw is None, "`/start` файлов не трогает"


def test_start_answer_is_cleaning_frame_even_when_status_is_not(bench):
    """ТЕКУЩЕЕ поведение: при `frame_cleaned` ответ говорит `cleaning_frame`.

    Зафиксировано как есть, до правки: тело ответа и статус в БД расходятся —
    вызвавший `/start` из `frame_cleaned` получает «стадия открыта», а машина
    осталась там же и RUNNING-строки не завела.
    """
    b = bench(DiagramStatus.FRAME_CLEANED)
    assert b.call("start") == 200
    assert b.result["status"] == "cleaning_frame"
    assert b.diagram.status is DiagramStatus.FRAME_CLEANED
    assert b.stages_opened == 0


def test_start_keeps_running_row_single_on_reentry(bench):
    """Повторный `/start` в `cleaning_frame` второй RUNNING-строки не плодит."""
    b = bench(DiagramStatus.UPLOADED)
    assert b.call("start") == 200
    assert b.stages_opened == 1
    assert b.call("start") == 200
    assert b.stages_opened == 1


def test_start_missing_diagram_is_404(bench):
    """Нет диаграммы — 404, а не 400 гейта."""
    b = bench(DiagramStatus.UPLOADED)
    b.db.diagram = None
    assert b.call("start") == 404
    assert b.stages_opened == 0


# ── POST /{uid}/save — полный перебор статусов ───────────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_save_gate_over_every_status(status, bench):
    """Каждый статус машины: `/save` пропускает ровно те, что в таблице."""
    b = bench(status)
    code = b.call("save")

    if status.value not in SAVE_ALLOWED:
        assert code == 400
        assert b.diagram.status is status
        assert b.db.commits == 0
        assert b.canon == RAW, "канон перезаписан отказавшим эндпоинтом"
        assert b.raw is None, "бэкап заведён отказавшим эндпоинтом"
        assert b.cleaned_artifact is None
        return

    assert code == 200
    # Сохранение неразрушающе: сырое ушло в бэкап, очищенное стало каноном.
    assert b.raw == RAW, "сырое изображение не забэкаплено"
    assert b.canon == CLEANED, "очищенное не стало каноном"
    assert b.cleaned_artifact is not None, "маркер ORIGINAL_CLEANED не заведён"
    assert b.db.commits == 1


def test_save_from_uploaded_moves_status_without_stage_row(bench):
    """ТЕКУЩЕЕ поведение: тот же переход `uploaded → cleaning_frame`, но без строки.

    Докстринг модуля обещает RUNNING-строку «на реальном переходе
    UPLOADED→CLEANING_FRAME». Переход этот делают ДВА места — `/start:102-106`
    и `/save:137-138`, — а строку заводит только первое. Диаграмма может
    оказаться в `cleaning_frame`, не имея ни одной строки стадии.
    """
    b = bench(DiagramStatus.UPLOADED)
    assert b.call("save") == 200
    assert b.diagram.status is DiagramStatus.CLEANING_FRAME
    assert b.stages_opened == 0


def test_save_from_frame_cleaned_keeps_status(bench):
    """Повторная очистка после завершения этапа статус не откатывает."""
    b = bench(DiagramStatus.FRAME_CLEANED, cleaned_saved=True, raw_backup=RAW,
              canon=b"CLEANED-1")
    assert b.call("save") == 200
    assert b.diagram.status is DiagramStatus.FRAME_CLEANED
    assert b.canon == CLEANED


def test_save_backs_up_raw_exactly_once(bench):
    """Второе сохранение бэкап НЕ перетирает — иначе «сырое» станет очищенным."""
    b = bench(DiagramStatus.CLEANING_FRAME)
    assert b.call("save") == 200
    assert b.raw == RAW
    assert b.call("save", upload=FakeUpload(b"CLEANED-2")) == 200
    assert b.raw == RAW, "бэкап перезаписан очищенным — сырое потеряно"
    assert b.canon == b"CLEANED-2"


def test_save_replaces_previous_marker(bench):
    """Маркер очистки один: старый снимается, новый добавляется."""
    b = bench(DiagramStatus.CLEANING_FRAME, cleaned_saved=True, raw_backup=RAW)
    old = b.cleaned_artifact
    assert b.call("save") == 200
    assert old in b.db.deleted
    assert b.cleaned_artifact is not old
    assert b.cleaned_artifact.artifact_type is ArtifactType.ORIGINAL_CLEANED


def test_save_rejects_foreign_content_type(bench):
    """Не PNG — 400, и ни файла, ни маркера."""
    b = bench(DiagramStatus.CLEANING_FRAME)
    assert b.call("save", upload=FakeUpload(b"%PDF", "application/pdf")) == 400
    assert b.canon == RAW
    assert b.raw is None
    assert b.cleaned_artifact is None
    assert b.db.commits == 0


# ── POST /{uid}/complete — полный перебор статусов ───────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_complete_gate_over_every_status(status, bench):
    """Каждый статус машины: `/complete` пропускает ровно те, что в таблице."""
    b = bench(status, cleaned_saved=True, raw_backup=RAW, canon=CLEANED)
    code = b.call("complete")

    if status.value not in COMPLETE_ALLOWED:
        assert code == 400
        assert b.diagram.status is status
        assert b.db.commits == 0
        assert b.stages_opened == 0
        assert b.cleaned_artifact is not None, "маркер снесён отказавшим эндпоинтом"
        assert b.tasks == [], "задача отправлена при отказе"
        return

    assert code == 200
    assert b.canon == CLEANED, "`/complete` подменил канон"
    assert b.raw == RAW, "`/complete` тронул бэкап"
    assert b.cleaned_artifact is not None, "`/complete` снёс очистку"

    if status is DiagramStatus.FRAME_CLEANED:
        # Уже завершено — ранний выход, ничего не делаем и НЕ передиспатчим.
        assert b.diagram.status is DiagramStatus.FRAME_CLEANED
        assert b.db.commits == 0
        assert b.stages_opened == 0
        assert b.tasks == []
    else:
        target, task, commits = CONFIRM_AFTER
        assert b.diagram.status is DiagramStatus(target)
        assert b.db.commits == commits
        assert b.diagram.error_message is None
        assert b.diagram.error_stage is None
        # Самовосстановление: `/start` могли не звать — строку заводим и закрываем.
        assert b.stages_opened == 1
        assert b.stages_completed == 1
        assert b.tasks == [task]


def test_complete_refuses_without_saved_cleaning(bench):
    """Без сохранённой очистки — 400 и ни одной строки стадии."""
    b = bench(DiagramStatus.CLEANING_FRAME)
    assert b.call("complete") == 400
    assert b.diagram.status is DiagramStatus.CLEANING_FRAME
    assert b.db.commits == 0
    assert b.stages_opened == 0
    assert b.tasks == [], "детекция отправлена на незавершённом этапе"


def test_complete_closes_row_opened_by_start(bench):
    """Строку, открытую в `/start`, закрывает `/complete`, а не заводит вторую."""
    b = bench(DiagramStatus.UPLOADED, cleaned_saved=True, raw_backup=RAW)
    assert b.call("start") == 200
    assert b.call("complete") == 200
    assert b.stages_opened == 1, "заведена вторая строка вместо закрытия первой"
    assert b.stages_completed == 1


# ── POST /{uid}/skip — полный перебор статусов ───────────────────────────

@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_skip_gate_over_every_status(status, bench):
    """Каждый статус машины: `/skip` пропускает ровно те, что в таблице."""
    b = bench(status, canon=RAW)
    code = b.call("skip")

    if status.value not in SKIP_ALLOWED:
        assert code == 400
        assert b.diagram.status is status
        assert b.db.commits == 0
        assert b.stages_opened == 0
        assert b.canon == RAW
        assert b.tasks == [], "задача отправлена при отказе"
        return

    assert code == 200

    if status is DiagramStatus.FRAME_CLEANED:
        # Ранний выход — тот же, что у `/complete`: этап уже завершён.
        assert b.diagram.status is DiagramStatus.FRAME_CLEANED
        assert b.db.commits == 0
        assert b.stages_opened == 0
        assert b.tasks == []
        return

    target, task, commits = CONFIRM_AFTER
    assert b.diagram.status is DiagramStatus(target)
    assert b.diagram.error_message is None
    assert b.diagram.error_stage is None
    assert b.db.commits == commits
    assert b.stages_opened == 1
    assert b.stages_completed == 1
    assert b.tasks == [task]


def test_skip_without_backup_leaves_canon_alone(bench):
    """Очистки не было — канон и есть сырое, трогать нечего."""
    b = bench(DiagramStatus.CLEANING_FRAME, canon=RAW)
    assert b.call("skip") == 200
    assert b.canon == RAW
    assert b.raw is None


@pytest.mark.parametrize("status", [
    DiagramStatus.UPLOADED,
    DiagramStatus.CLEANING_FRAME,
], ids=lambda s: s.value)
def test_skip_destroys_saved_cleaning(status, bench):
    """`/skip` стирает СОХРАНЁННУЮ очистку из НЕЗАВЕРШЁННОГО этапа.

    Поведение не менялось: «рамки нет» и есть команда вернуть сырое.
    Третий статус (`frame_cleaned`) из этого перебора УШЁЛ — см. тест ниже.
    """
    b = bench(status, cleaned_saved=True, raw_backup=RAW, canon=CLEANED)
    assert b.call("skip") == 200
    assert b.canon == RAW, "очищенное изображение уцелело — поведение изменилось"
    assert b.raw is None, "бэкап уцелел — поведение изменилось"
    assert b.cleaned_artifact is None, "маркер уцелел — поведение изменилось"


def test_skip_from_done_is_an_early_exit(bench):
    """`/skip` из `frame_cleaned` больше НЕ переигрывает завершённый этап.

    До Б9 повтор из этого статуса сносил сохранённую очистку и бэкап — откат
    завершённого этапа мимо `POST /diagrams/{uid}/rollback`, без вопроса
    оператору и без `preserve`-флагов (замер 1.3, §51.23: жестом недостижимо,
    `api_client.skip_frame_removal` не вызван ниоткуда, но клетка машины
    допускала). С Б9 цена стала выше — повтор передиспатчил бы вторую детекцию
    на тот же переход, — и ветка приведена к виду `/complete:187-188`.
    """
    b = bench(DiagramStatus.FRAME_CLEANED, cleaned_saved=True, raw_backup=RAW,
              canon=CLEANED)
    assert b.call("skip") == 200
    assert b.result["message"] == "Already completed"
    assert b.canon == CLEANED, "завершённый этап переигран: канон подменён"
    assert b.raw == RAW, "завершённый этап переигран: бэкап снесён"
    assert b.cleaned_artifact is not None, "завершённый этап переигран: маркер снесён"
    assert b.db.commits == 0
    assert b.stages_opened == 0
    assert b.tasks == [], "повтор отправил вторую детекцию"


def test_skip_missing_diagram_is_404(bench):
    """Нет диаграммы — 404 до всякой работы с файлами."""
    b = bench(DiagramStatus.CLEANING_FRAME, raw_backup=RAW, canon=CLEANED)
    b.db.diagram = None
    assert b.call("skip") == 404
    assert b.canon == CLEANED
    assert b.raw == RAW


# ── шов «оператор вышел из вкладки, не завершив этап» ────────────────────

def test_saved_cleaning_survives_leaving_the_tab(bench):
    """Сохранил очистку и ушёл — очистка на месте, и следующий вход её видит.

    Жест из замера 1.3 (§51.11): «Очистка рамки → 💾 → ← Назад». До пункта 1.3
    клиент на выходе звал `rollback_diagram(uid, 'uploaded')` и очистка гибла;
    откат снят, и сервер сам по себе её не трогает — вот это и утверждается.
    """
    b = bench(DiagramStatus.UPLOADED)
    assert b.call("start") == 200
    assert b.call("save") == 200
    saved = b.cleaned_artifact

    # «← Назад» — на сервер не уходит ничего. Следующее открытие вкладки:
    # статус уже `cleaning_frame`, клиент `/start` не зовёт, качает канон.
    assert b.diagram.status is DiagramStatus.CLEANING_FRAME
    assert b.canon == CLEANED, "очищенное изображение потеряно"
    assert b.raw == RAW, "сырой бэкап потерян"
    assert b.cleaned_artifact is saved, "маркер очистки потерян"


def test_running_row_survives_leaving_the_tab(bench):
    """ТЕКУЩЕЕ поведение: RUNNING-строка после ухода из вкладки остаётся открытой.

    Закрыть её некому: `/complete` и `/skip` не вызваны, а «← Назад» на сервер
    не ходит. Строка живёт до следующего завершения этапа.
    """
    b = bench(DiagramStatus.UPLOADED)
    assert b.call("start") == 200
    assert b.call("save") == 200
    assert b.stages_opened == 1
    assert b.stages_completed == 0, "строку кто-то закрыл — поведение изменилось"


# ── Б9: подтверждение этапа само ставит детекцию ─────────────────────────

@pytest.mark.parametrize("endpoint", ["complete", "skip"])
def test_confirm_dispatches_detection_with_the_default_model(endpoint, bench):
    """Обе двери этапа ставят ОДНУ задачу и НЕ выбирают модель за конфиг.

    `model_id=None` — дефолт разрешает сама задача (`worker/tasks/detection.py`:
    `project_config.detection.default_model`). Литерал `None` здесь абсолютный:
    вычисли его тест из кода — и он остался бы зелёным при любом значении.
    """
    b = bench(DiagramStatus.CLEANING_FRAME, cleaned_saved=True, raw_backup=RAW,
              canon=CLEANED)
    assert b.call(endpoint) == 200

    assert len(b.dispatched) == 1
    sent = b.dispatched[0]
    assert sent["name"] == DETECT_TASK
    assert sent["args"] == [str(UID)]
    assert sent["kwargs"]["model_id"] is None, "автозапуск выбрал модель за конфиг"
    assert sent["kwargs"]["project_code"] == "thermohydraulics"


@pytest.mark.parametrize("endpoint", ["complete", "skip"])
def test_second_confirm_does_not_dispatch_again(endpoint, bench):
    """Сценарий ПОСЛЕ чужого действия: повтор подтверждения дубля не плодит.

    Второй прогон идёт по состоянию, которое записал ПЕРВЫЙ (диаграмма уже
    в `detecting`), а не с чистого листа. Клетка достижима: клиент повторяет
    `/complete` при разрыве соединения, а `_FRAME_EDITABLE` до Б9 пускал
    повтор из `frame_cleaned` в обе двери.
    """
    b = bench(DiagramStatus.CLEANING_FRAME, cleaned_saved=True, raw_backup=RAW,
              canon=CLEANED)
    assert b.call(endpoint) == 200
    assert len(b.dispatched) == 1

    # Диаграмма ушла в `detecting` — обе двери отвечают 400 её же гейтом.
    assert b.call(endpoint) == 400
    assert len(b.dispatched) == 1, "повтор отправил вторую детекцию"

    # И та же проверка на клетке, которую гейт ПУСКАЕТ: `frame_cleaned`.
    b.diagram.status = DiagramStatus.FRAME_CLEANED
    assert b.call(endpoint) == 200
    assert len(b.dispatched) == 1, "ранний выход отправил вторую детекцию"


@pytest.mark.parametrize("endpoint", ["complete", "skip"])
def test_dead_broker_does_not_break_the_confirmation(endpoint, bench):
    """Брокер лёг: этап ПОДТВЕРЖДЁН, ответ 200, кнопка «Поиск элементов» жива.

    Это и есть политика best-effort: отправка — не часть подтверждения рамки.
    Точка возврата (`frame_cleaned`) лежит внутри гейта `/detect`, поэтому
    повтор после подъёма брокера проходит — иначе тупик класса 1.13.
    """
    b = bench(DiagramStatus.CLEANING_FRAME, cleaned_saved=True, raw_backup=RAW,
              canon=CLEANED, broker_down=True)
    assert b.call(endpoint) == 200

    target, task, commits = CONFIRM_AFTER_DEAD_BROKER
    assert b.diagram.status is DiagramStatus(target)
    assert b.diagram.error_stage is None, "мёртвый брокер оставил стадию ошибки"
    assert b.diagram.error_message is None
    assert b.db.commits == commits
    assert b.tasks == [task]
    assert b.stages_completed == 1, "строка стадии не закрыта из-за чужого отказа"


@pytest.mark.parametrize("endpoint", ["complete", "skip"])
def test_operator_button_still_works_after_a_dead_broker(endpoint, bench):
    """Сценарий ПОСЛЕ отказа: брокер поднялся, оператор жмёт «Поиск элементов».

    Судится НАСТОЯЩАЯ корутина `/detect` на состоянии, которое записал
    отказавший автозапуск, — а не представление теста о том, что она пускает.
    """
    from app.api.detection import start_detection

    b = bench(DiagramStatus.CLEANING_FRAME, cleaned_saved=True, raw_backup=RAW,
              canon=CLEANED, broker_down=True)
    assert b.call(endpoint) == 200
    assert b.diagram.status is DiagramStatus.FRAME_CLEANED

    b.broker_down = False
    b.dispatched.clear()

    result = asyncio.run(start_detection(UID, model_id=None, db=b.db))
    assert result["status"] == "detecting"
    assert b.diagram.status is DiagramStatus.DETECTING
    assert b.tasks == [DETECT_TASK]


@pytest.mark.parametrize("endpoint", ["complete", "skip"])
def test_dead_broker_leaves_a_trace_with_uid(endpoint, bench):
    """Д2: отказ автозапуска не молчит — строка с `uid` и точкой возврата."""
    import logging

    from app.core.logging import ContextFilter

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []
            self.addFilter(ContextFilter())

        def emit(self, record):
            self.records.append(record)

    b = bench(DiagramStatus.CLEANING_FRAME, cleaned_saved=True, raw_backup=RAW,
              canon=CLEANED, broker_down=True)

    handler = _Capture()
    api_logger = logging.getLogger("app.api.frame")
    previous_level = api_logger.level
    api_logger.addHandler(handler)
    api_logger.setLevel(logging.DEBUG)
    try:
        assert b.call(endpoint) == 200
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    trace = [r for r in handler.records
             if getattr(r, "event", None) == "dispatch_failed"]
    assert len(trace) == 1, "отказ автозапуска детекции не оставил следа"
    assert trace[0].uid == str(UID)
    assert trace[0].phase == "frame_removal"
    assert "frame_cleaned" in trace[0].getMessage(), "точка возврата не названа"
