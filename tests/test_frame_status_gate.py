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
    """Стенд: подменённые `StorageService` и хелперы стадии, диск во `tmp_path`."""

    def _make(status, *, cleaned_saved=False, raw_backup=None, canon=RAW):
        b = Bench(tmp_path, status, cleaned_saved=cleaned_saved,
                  raw_backup=raw_backup, canon=canon)

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
        return

    assert code == 200
    assert b.diagram.status is DiagramStatus.FRAME_CLEANED
    assert b.canon == CLEANED, "`/complete` подменил канон"
    assert b.raw == RAW, "`/complete` тронул бэкап"
    assert b.cleaned_artifact is not None, "`/complete` снёс очистку"

    if status is DiagramStatus.FRAME_CLEANED:
        # Уже завершено — ранний выход, ничего не делаем.
        assert b.db.commits == 0
        assert b.stages_opened == 0
    else:
        assert b.db.commits == 1
        assert b.diagram.error_message is None
        assert b.diagram.error_stage is None
        # Самовосстановление: `/start` могли не звать — строку заводим и закрываем.
        assert b.stages_opened == 1
        assert b.stages_completed == 1


def test_complete_refuses_without_saved_cleaning(bench):
    """Без сохранённой очистки — 400 и ни одной строки стадии."""
    b = bench(DiagramStatus.CLEANING_FRAME)
    assert b.call("complete") == 400
    assert b.diagram.status is DiagramStatus.CLEANING_FRAME
    assert b.db.commits == 0
    assert b.stages_opened == 0


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
        return

    assert code == 200
    assert b.diagram.status is DiagramStatus.FRAME_CLEANED
    assert b.diagram.error_message is None
    assert b.diagram.error_stage is None
    assert b.db.commits == 1
    assert b.stages_opened == 1
    assert b.stages_completed == 1


def test_skip_without_backup_leaves_canon_alone(bench):
    """Очистки не было — канон и есть сырое, трогать нечего."""
    b = bench(DiagramStatus.CLEANING_FRAME, canon=RAW)
    assert b.call("skip") == 200
    assert b.canon == RAW
    assert b.raw is None


@pytest.mark.parametrize("status", [
    DiagramStatus.UPLOADED,
    DiagramStatus.CLEANING_FRAME,
    DiagramStatus.FRAME_CLEANED,
], ids=lambda s: s.value)
def test_skip_destroys_saved_cleaning(status, bench):
    """ТЕКУЩЕЕ поведение: `/skip` стирает СОХРАНЁННУЮ очистку из всех трёх статусов.

    Зафиксировано как есть, до правки. Из `frame_cleaned` это откат уже
    завершённого этапа мимо `POST /diagrams/{uid}/rollback` — без вопроса
    оператору и без `preserve`-флагов. Клиент этот эндпоинт сегодня не зовёт
    (`api_client.skip_frame_removal` не вызван ниоткуда — замер 1.3, §51.23),
    поэтому дефект не достижим жестом, но клетка машины его допускает.
    """
    b = bench(status, cleaned_saved=True, raw_backup=RAW, canon=CLEANED)
    assert b.call("skip") == 200
    assert b.canon == RAW, "очищенное изображение уцелело — поведение изменилось"
    assert b.raw is None, "бэкап уцелел — поведение изменилось"
    assert b.cleaned_artifact is None, "маркер уцелел — поведение изменилось"


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
