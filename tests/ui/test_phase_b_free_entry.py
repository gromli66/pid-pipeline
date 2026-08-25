# -*- coding: utf-8 -*-
"""Вход в пройденный этап фазы B ≠ откат (блок 3, пункты Н3+, Н4, 5-1).

Дефект. Оператор, кликнувший по уже пройденной бусине, получал ОДИН диалог на
все кнопки: «Этап «val_graph» уже пройден. Откатить до «built»?». Ответ «Да»
разрушал конвейер (артефакты последующих этапов удалялись), ответ «Нет» не
открывал ничего — то есть посмотреть на пройденный этап было НЕЛЬЗЯ. Для фазы B
(«Проверка схемы» ⇄ «Контуры» ⇄ «Распознавание» ⇄ «Привязка») это прямо против
решений Максима №7/№8: этапы там разные, а вход в пройденный — не откат.

Что судится. Ветка `DiagramWorkspace._on_button_click` на живом воркспейсе:
какие клики уходят в обработчик молча, какие спрашивают и о чём именно.
Обработчик подменён журналом — тяжёлые вкладки к решению «откат или вход»
отношения не имеют, а их конструкторы качают артефакты.

Класс правки — [сма]: первая редакция файла снята с НЕТРОНУТОГО клиента и
зелена на нём, поэтому по диффу видно, какие именно клетки сменили исход.

⚠ `QMessageBox` подменён (`PROTOCOL §5`): без подмены красный прогон не падает,
а ВИСНЕТ на живой модалке, и на CI это выглядит как «долго».
"""
import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import DiagramStatus           # noqa: E402

UID = str(uuid.UUID("b7a5e011-1111-2222-3333-444455556666"))

# Ключи фазы B: свободный вход по решениям №7/№8.
PHASE_B_KEYS = ("val_graph", "contours", "ocr_binding", "edit_graph")

# Ключи фазы A: конвейер там линейный, повторный проход разрушающий —
# диалог отката остаётся.
PHASE_A_KEYS = ("frame", "detect", "segment", "pipe", "junction", "graph")

# Два ключа со своим поведением, к откату отношения не имеющие.
OWN_BRANCH_KEYS = ("cvat", "fxml")

# Статус, при котором ВСЕ тринадцать этапов числятся пройденными.
ALL_DONE = DiagramStatus.COMPLETED


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeMsgBox:
    """Подмена модалки: журнал (вид, заголовок, текст) + заданный ответ."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("question", title, text))
        return cls.answer

    @classmethod
    def warning(cls, parent, title, text="", *args, **kwargs):
        cls.calls.append(("warning", title, text))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, parent, title, text="", *args, **kwargs):
        cls.calls.append(("information", title, text))
        return cls.StandardButton.Ok


class FakeDiagramInfo:
    def __init__(self, status):
        self.status = status
        self.error_stage = None
        self.project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность APIClient, которой воркспейс пользуется в этом сценарии."""

    def __init__(self, status):
        self.status = status
        self.rollbacks = []

    def get_diagram(self, uid):
        return FakeDiagramInfo(self.status)

    def get_stages(self, uid):
        return []

    def get_ocr_status(self, uid):
        return {"has_ocr_result": True}

    def rollback_diagram(self, uid, target_status, preserve_ocr=False,
                         preserve_contours=False):
        self.rollbacks.append({
            "target": target_status,
            "preserve_ocr": preserve_ocr,
            "preserve_contours": preserve_contours,
        })
        return {"deleted_artifacts": 0}


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Воркспейс на заданном статусе + журнал кликов.

    Свои виджеты набор сносит сам, детерминированно (`PROTOCOL §5`, правило
    1-32): брошенные на сборщик мусора виджеты оставляют отложенные удаления,
    которые детонируют у соседа, первым провернувшего очередь событий.
    """
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)

    made = []

    def _make(status=ALL_DONE):
        api = FakeAPI(status)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        opened = []
        return ws, api, opened

    yield _make

    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _click(ws, key, opened):
    """Клик по кнопке этапа с подменённым обработчиком."""
    ws._on_button_click(key, lambda: opened.append(key))


# ── сторожа стенда ───────────────────────────────────────────────────────

def test_bench_really_puts_every_stage_into_completed(bench):
    """Все тринадцать кнопок при `completed` числятся пройденными.

    Без этого утверждения тесты ниже могли бы проходить просто потому, что
    ветка «этап пройден» не достигается вовсе.
    """
    ws, _api, _opened = bench()
    _available, completed, _processing = dw._buttons_for_status(ALL_DONE)
    assert set(PHASE_A_KEYS) | set(PHASE_B_KEYS) | {"ocr"} <= completed
    assert ws._last_status is ALL_DONE


def test_key_sets_cover_every_button():
    """Перебор ведётся полным списком кнопок, а не выборкой (`PROTOCOL §3`)."""
    declared = {k for k, _label in dw.DiagramWorkspace._BUTTON_DEFS}
    assert declared == set(PHASE_A_KEYS) | set(PHASE_B_KEYS) | set(OWN_BRANCH_KEYS) | {"ocr"}


# ── фаза B: вход свободный ───────────────────────────────────────────────

@pytest.mark.parametrize("key", PHASE_B_KEYS)
def test_phase_b_entry_asks_nothing_and_opens(key, bench):
    """Четыре ключа фазы B: ни диалога, ни отката — сразу в обработчик."""
    ws, api, opened = bench()
    _click(ws, key, opened)

    assert FakeMsgBox.calls == [], f"{key}: оператора о чём-то спросили"
    assert api.rollbacks == [], f"{key}: конвейер откатили"
    assert opened == [key], f"{key}: обработчик не позвали"


def test_phase_b_entry_is_free_after_a_foreign_action(bench):
    """Тот же вход ПОСЛЕ чужого действия оператора, а не с чистого листа.

    `PROTOCOL §3`: тест на свежем объекте не проверяет взаимодействие с
    предысторией. Здесь оператор сначала ходит по фазе B кругом — контуры,
    проверка схемы, привязка, ручная правка — и только потом повторяет вход.
    """
    ws, api, opened = bench()
    for key in ("contours", "val_graph", "ocr_binding", "edit_graph", "contours"):
        _click(ws, key, opened)

    assert FakeMsgBox.calls == []
    assert api.rollbacks == []
    assert opened == ["contours", "val_graph", "ocr_binding", "edit_graph",
                      "contours"]


# ── распознавание: свой диалог, не откатный ──────────────────────────────

def test_ocr_asks_about_restart_not_rollback(bench):
    """`ocr` — не вкладка, а POST: спрашиваем про ПЕРЕЗАПУСК (решение №6).

    `_start_ocr` сносит сырой результат распознавания и жжёт минуты CPU,
    поэтому молчаливый клик здесь неуместен. Но и конвейер назад не идёт.
    """
    ws, api, opened = bench()
    _click(ws, "ocr", opened)

    assert len(FakeMsgBox.calls) == 1
    kind, title, text = FakeMsgBox.calls[0]
    assert kind == "question"
    assert "Откат" not in title, f"диалог всё ещё откатный: {title}"
    assert api.rollbacks == [], "перезапуск распознавания откатил конвейер"
    assert opened == ["ocr"]


def test_ocr_restart_refused_does_nothing(bench):
    """Порог заперт с другой стороны: «Нет» — и распознавание не трогается."""
    ws, api, opened = bench()
    FakeMsgBox.answer = QMessageBox.StandardButton.No
    _click(ws, "ocr", opened)

    assert len(FakeMsgBox.calls) == 1
    assert opened == [], "отказ оператора не остановил перезапуск"
    assert api.rollbacks == []


def test_restarting_ocr_does_not_touch_contours(bench):
    """Репро-гейт пункта 5-1: «Переделать OCR» не сносит SAM2-контуры.

    После Н3+ этот клик откатов не делает ВОВСЕ, поэтому контурам ничего не
    грозит по построению. Страховка на случай, если путь отката до этой
    кнопки когда-нибудь вернётся, — в `_PRESERVE_CONTOURS_KEYS`.
    """
    ws, api, opened = bench()
    _click(ws, "ocr", opened)

    assert api.rollbacks == [], "контуры сносит только откат — а его нет"
    assert "ocr" in dw.DiagramWorkspace._PRESERVE_CONTOURS_KEYS
    assert "ocr" not in dw.DiagramWorkspace._PRESERVE_OCR_KEYS, (
        "при «Переделать OCR» старые OCR-артефакты обязаны сноситься"
    )


# ── фаза A: откат остаётся, но диалог честный (Н4) ───────────────────────

@pytest.mark.parametrize("key", PHASE_A_KEYS)
def test_phase_a_still_offers_rollback(key, bench):
    """Фаза A линейна, повторный проход разрушающий — диалог отката на месте."""
    ws, api, opened = bench()
    _click(ws, key, opened)

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], key
    assert len(api.rollbacks) == 1, key
    assert api.rollbacks[0]["target"] == ws._ROLLBACK_TARGET[key], key
    assert opened == [key], key


@pytest.mark.parametrize("key", PHASE_A_KEYS)
def test_rollback_dialog_speaks_russian(key, bench):
    """Н4: в диалоге нет технических ключей — ни snake_case, ни подчёркиваний.

    Проверка именно на ПОДЧЁРКИВАНИЯ, а не на латиницу: сами подписи статусов
    легально содержат «✓ Bbox валидированы» и «OCR завершён».
    """
    ws, _api, opened = bench()
    _click(ws, key, opened)

    _kind, _title, text = FakeMsgBox.calls[0]
    assert "_" not in text, f"{key}: технический ключ в диалоге:\n{text}"
    assert dw.DiagramWorkspace._KEY_LABELS[key] in text, key


@pytest.mark.parametrize("key", PHASE_A_KEYS)
def test_rollback_dialog_names_the_cost_for_the_canvas(key, bench):
    """Н4: диалог прямо говорит, что станет с «Ручной правкой».

    Единственное место, где живут правки оператора, — `graph_canvas.json`, и
    до Н4 диалог о нём молчал. Формулировка зависит от цели: у всех кнопок
    фазы A цель раньше привязки, значит холст погибнет.
    """
    ws, _api, opened = bench()
    _click(ws, key, opened)

    _kind, _title, text = FakeMsgBox.calls[0]
    assert "Ручной правке" in text, f"{key}: о холсте ни слова:\n{text}"


def test_dialog_wording_follows_the_canvas_border(bench):
    """Обе формулировки существуют и выбираются по той же границе, что Н1.

    Утверждение о НАШЕМ решении: тексты выбирает `_rollback_consequence`, и
    порог у него — `OCR_BOUND`, тот же, что у сервера (`canvas_dies`).
    """
    ws, _api, _opened = bench()
    lost = ws._rollback_consequence("validated_graph")
    kept = ws._rollback_consequence("ocr_bound")
    assert lost != kept
    assert "потеря" in lost.lower() or "потерян" in lost.lower()
    assert "сохранит" in kept.lower()


# ── два ключа со своей веткой ────────────────────────────────────────────

@pytest.mark.parametrize("key", OWN_BRANCH_KEYS)
def test_own_branch_keys_never_rollback(key, bench):
    """`cvat` и `fxml` откат не предлагают — у них свои диалоги внутри."""
    ws, api, opened = bench()
    _click(ws, key, opened)

    assert api.rollbacks == [], key
    assert opened == [key], key


# ── сценарный круг фазы B: клиент решает, судят НАСТОЯЩИЕ гейты ──────────
#
# Дефект живёт на ШВЕ «кнопка клиента → гейт сервера»: и кнопка права, и
# каждый гейт сам по себе непротиворечив. Поэтому здесь живой воркспейс, а
# сохранения и подтверждения судят настоящие корутины эндпоинтов — не наше
# представление о том, что они пропускают (образец — `test_no_silent_rollback`).

import asyncio                                             # noqa: E402

from fastapi import HTTPException                          # noqa: E402

import app.api.contours as contours_api                    # noqa: E402
import app.services.storage as storage_mod                 # noqa: E402
from app.api.contours import complete_contour_validation   # noqa: E402
from app.api.ocr import save_ocr_binding                   # noqa: E402
from app.api.validation import (                           # noqa: E402
    complete_simple_graph_validation,
    save_validated_graph,
)
from app.models import Artifact, ArtifactType, Diagram      # noqa: E402
from app.models import DiagramStatus as SrvStatus           # noqa: E402
from ui.services.api_client import APIError                 # noqa: E402


class _Res:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _SrvDB:
    def __init__(self, diagram, artifacts):
        self.diagram = diagram
        self.artifacts = artifacts

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _Res(self.diagram)
        params = stmt.compile().params
        return _Res(self.artifacts.get(params.get("artifact_type_1")))

    async def commit(self):
        pass

    async def flush(self):
        pass

    def add(self, obj):
        pass

    async def delete(self, obj):
        pass


class _Upload:
    async def read(self):
        return b'{"nodes": [], "edges": []}'


def _art(path):
    a = Artifact()
    a.file_path = path
    a.file_size = 1
    a.mime_type = "application/json"
    return a


class LiveServerAPI(FakeAPI):
    """FakeAPI, у которой сохранения и подтверждения — настоящие корутины."""

    def __init__(self, status):
        super().__init__(DiagramStatus(status.value))
        self.diagram = Diagram()
        self.diagram.uid = uuid.UUID(UID)
        self.diagram.status = status
        self.diagram.error_stage = None
        self.diagram.error_message = None
        self.diagram.project_code = "thermohydraulics"
        self.db = _SrvDB(self.diagram, {
            ArtifactType.GRAPH_VALIDATED: _art(f"{UID}/graph/graph_validated.json"),
            ArtifactType.OCR_RESULT: _art(f"{UID}/ocr/ocr_result.json"),
            ArtifactType.CONTOURS_VALIDATED: _art(
                f"{UID}/contours/contours_validated.json"),
        })
        self.refusals = []

    # клиент читает статус отсюда — он живой, его меняют эндпоинты
    def get_diagram(self, uid):
        return FakeDiagramInfo(DiagramStatus(self.diagram.status.value))

    def _call(self, coro):
        try:
            return asyncio.run(coro)
        except HTTPException as exc:
            self.refusals.append((exc.status_code, str(exc.detail)))
            raise APIError(str(exc.detail), exc.status_code) from None

    def save_validated_graph(self):
        return self._call(save_validated_graph(
            uuid.UUID(UID), file=_Upload(), db=self.db))

    def complete_simple_graph_validation(self, uid=None):
        return self._call(complete_simple_graph_validation(
            uuid.UUID(UID), db=self.db))

    def complete_contours(self):
        return self._call(complete_contour_validation(uuid.UUID(UID), db=self.db))

    def save_binding(self):
        return self._call(save_ocr_binding(
            uuid.UUID(UID), file=_Upload(), db=self.db))


@pytest.fixture
def live(qapp, monkeypatch, tmp_path):
    """Живой воркспейс + настоящие серверные гейты на временном хранилище."""
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr(storage_mod.settings, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(contours_api, "_ocr_enabled", lambda code: True)

    async def _no_layout(uid, db, force=False):
        return {"status": "stub"}

    monkeypatch.setattr(contours_api, "dispatch_layout", _no_layout)

    for stage, name in (("graph", "graph_validated.json"),
                        ("ocr", "ocr_result.json"),
                        ("contours", "contours_validated.json")):
        d = tmp_path / UID / stage
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text('{"nodes": []}', encoding="utf-8")

    made = []

    def _make(status):
        api = LiveServerAPI(status)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        return ws, api, []

    yield _make

    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_full_circle_of_phase_b_without_a_single_rollback(live):
    """Контуры → Проверка схемы → правка → подтверждение → Привязка.

    Круг оператора целиком, без единого отката и без единого 400. До блока 3
    он разваливался трижды: клик по «Проверке схемы» предлагал откат,
    подтверждение из `contours_validated` отвечало 400, а подтверждение
    контуров при возврате тянуло статус на два шага назад.
    """
    ws, api, opened = live(SrvStatus.CONTOURS_VALIDATED)

    _click(ws, "contours", opened)          # чужое действие оператора первым
    api.complete_contours()
    assert api.diagram.status is SrvStatus.CONTOURS_VALIDATED

    _click(ws, "val_graph", opened)
    api.save_validated_graph()
    api.complete_simple_graph_validation()
    assert api.diagram.status is SrvStatus.CONTOURS_VALIDATED, (
        "подтверждение «Проверки схемы» увезло статус"
    )

    _click(ws, "ocr_binding", opened)
    api.save_binding()

    assert api.refusals == [], f"сервер отбил шаг круга: {api.refusals}"
    assert api.rollbacks == [], "круг по фазе B откатил конвейер"
    assert FakeMsgBox.calls == [], f"оператора о чём-то спросили: {FakeMsgBox.calls}"
    assert opened == ["contours", "val_graph", "ocr_binding"]


def test_tail_from_completed_enters_binding_and_saves(live):
    """Хвост из готовой схемы: вход в привязку → правка → сохранение → 200.

    Самая дорогая клетка блока: раньше и вход был откатом, и сохранение
    отвечало 400 — час работы оператора уходил в никуда.
    """
    ws, api, opened = live(SrvStatus.COMPLETED)

    _click(ws, "ocr_binding", opened)
    result = api.save_binding()

    assert result["status"] == "saved"
    assert api.diagram.status is SrvStatus.COMPLETED, "сохранение сдвинуло статус"
    assert api.refusals == []
    assert api.rollbacks == []
    assert FakeMsgBox.calls == []


def test_the_bench_judges_by_real_endpoints(live):
    """Сторож стенда: гейты настоящие, а не наше представление о них.

    Без этой клетки круг был бы зелёным и на подделке, которая пускает всё.
    """
    _ws, api, _opened = live(SrvStatus.BUILT)

    with pytest.raises(APIError) as exc:
        api.save_binding()
    assert exc.value.status_code == 400
    assert api.refusals and api.refusals[0][0] == 400
