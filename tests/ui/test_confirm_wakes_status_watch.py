# -*- coding: utf-8 -*-
"""Доработка pains-2 по приёмке глазами: после подтверждения этапа клиент СЛЕПНЕТ.

Дефект (замерен архитектором исполнением и логами). Блок 2 научил сервер
продолжать конвейер сам: подтверждение рамки ставит детекцию (Б9), подтверждение
разметки CVAT — сегментацию (Б8). Задача уходит, воркер её считает, статус в БД
живой — а оператор видит неподвижную схему до ручного «Обновить».

Механизм — четыре шага, ни один из которых сам по себе не дефект:

  1. `StatusProvider` снимает слежение на ФИНАЛЬНОМ статусе без бегущих стадий
     (`_FINAL_STATUSES`: `detected`, `validated_bbox`, `built`, …). Так и задумано;
  2. оператор открывает вкладку этапа — `_open_tab` запоминает
     `_was_status_watching = self.status_provider.is_watching(uid)`, то есть
     **False**;
  3. `_close_tab_and_restore_header` восстанавливает слежение «как было» —
     а было никак;
  4. `_refresh_status` разовый: он показывает статус на момент закрытия вкладки
     и больше не спрашивает.

Итог: статус стал живым (`detecting` / `segmenting`), а спрашивать о нём некому.
До блока 2 дыры не было видно, потому что следующее звено двигал сам клиент —
`_run_detection`/`_start_segmentation` звали `watch` в своём теле.

⭐ **Третья дверь добавлена mefx-8 (блок 8, 8.2): «Контуры».** Механизм тот же,
цена выше. `POST /contours/{uid}/complete` — единственное подтверждение фазы B,
которое ставит раскладку ВСЕГДА (`app/api/contours.py` → `dispatch_layout`),
и ровно оно же было единственным обработчиком, который слежение не будил:
статус после него часто не меняется вовсе (`_accept_contours` оставляет его как
есть, если фаза B пройдена дальше), а раскладка при этом считается 2–5 минут.
Без слежения о её готовности не узнавали ни кнопка «Ручной правки» (гейт живёт
СТАДИЕЙ, а стадии приезжают только опросом), ни экран ожидания самой вкладки —
дверь «вернитесь в „Контуры“ и подтвердите» не отпиралась бы без перезахода
в диаграмму.

Что проверяется — ДАННЫЕ, а не внутренние поля (принцип набора 0.4): факт
слежения за диаграммой у `StatusProvider`. Ни одного утверждения про
`_was_status_watching`, `_active_tab_key` и прочую механику — тест обязан пережить
декомпозицию воркспейса (волна 10-8 трогает `_close_tab_and_restore_header`,
и этот набор ей мешать не должен).

Оба прогона идут ПОСЛЕ чужого действия, а не с чистого листа (`PROTOCOL §3`):
предысторию — «поллер снял слежение на финальном статусе» — устраивает сам тест,
потому что без неё дефект недостижим: у свежей диаграммы слежение живо, и
восстановление «как было» случайно оказывается верным.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                            # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal       # noqa: E402
from PySide6.QtWidgets import (                          # noqa: E402
    QApplication, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget,
)

import ui.widgets.diagram_workspace as dw                # noqa: E402
from ui.services.api_client import DiagramStatus         # noqa: E402
from ui.services.status_provider import StatusProvider   # noqa: E402

UID = "b7e41d02-3333-4444-5555-666677778888"

# Двери блока 2: кнопка этапа -> вкладка -> `confirmed` -> сервер сам двигает
# конвейер. Значения — статус, в котором оператор входит в этап, и статус,
# который сервер ставит своим автозапуском. Литералы, снятые чтением
# `app/api/frame.py` и `app/api/cvat.py`, а не вычисленные из клиента.
DOORS = {
    # Вход в рамку — `uploaded`: ровно так оператор в неё и возвращается, откатом
    # с более позднего этапа (кнопка пройденного этапа из `frame_cleaned` ведёт
    # в диалог отката, а не во вкладку, — это другой жест и другой пункт).
    "frame": (DiagramStatus.UPLOADED, DiagramStatus.DETECTING),
    "cvat": (DiagramStatus.VALIDATING_BBOX, DiagramStatus.SEGMENTING),
    # Вход в контуры — `contours_extracted`: SAM2 посчитал, ждёт оператора.
    # Сервер на подтверждении ставит `contours_validated` И раскладку; статус
    # здесь двигает сам обработчик клиента через `complete_contour_validation`.
    "contours": (DiagramStatus.CONTOURS_EXTRACTED,
                 DiagramStatus.CONTOURS_VALIDATED),
}


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeServer:
    """Статус плюс журнал: чем ответил сервер на подтверждение этапа."""

    def __init__(self, status):
        self.status = status
        self.confirmations = []

    def confirm(self, next_status):
        self.confirmations.append(self.status)
        self.status = next_status


class FakeDiagram:
    def __init__(self, server):
        self.status = server.status
        self.error_stage = None
        self.error_message = None
        self.project_code = "thermohydraulics"
        self.cvat_task_id = 777
        self.cvat_job_id = 42


class FakeAPI:
    """Поверхность APIClient, которой воркспейс пользуется в этом сценарии."""

    def __init__(self, server):
        self.server = server

    def get_diagram(self, uid):
        return FakeDiagram(self.server)

    def get_stages(self, uid):
        return []

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_stage_durations(self, uid):
        return {}

    def start_frame_removal(self, uid):
        return {"status": "cleaning_frame"}

    def get_cvat_url(self, uid):
        return "http://cvat.local/tasks/777/jobs/42"

    def complete_contour_validation(self, uid):
        """`app/api/contours.py`: статус + ДИСПАТЧ РАСКЛАДКИ (8.2).

        Ответ повторяет боевой (`{"status": "ok", "layout": {...}}`) — клиент
        его не читает, но подделка не должна быть удобнее правды.
        """
        self.server.confirm(DiagramStatus.CONTOURS_VALIDATED)
        return {"status": "ok", "nodes_accepted": 3,
                "layout": {"status": "dispatched", "task_id": "task-layout-1"}}

    def fetch_cvat_annotations(self, uid):
        """`app/api/cvat.py`: аннотации + СЕРВЕРНЫЙ автозапуск сегментации (Б8)."""
        self.server.confirm(DiagramStatus.SEGMENTING)
        return {"status": "segmenting", "annotation_count": 12,
                "segmentation_task_id": "task-0001"}


class WatchProbe(QObject):
    """`StatusProvider` в объёме, который читает воркспейс, с ЧЕСТНЫМ множеством.

    Не заглушка «`is_watching` всегда False»: слежение здесь живёт ровно так же,
    как в бою (множество uid, `unwatch` его чистит), иначе тест судил бы
    собственную подделку, а не решение клиента.
    """

    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.watched = set()
        self.calls = []

    def watch(self, uid):
        self.calls.append(("watch", uid))
        self.watched.add(uid)

    def unwatch(self, uid):
        self.calls.append(("unwatch", uid))
        self.watched.discard(uid)

    def is_watching(self, uid):
        return uid in self.watched


class StubTab(QWidget):
    """Вкладка-заглушка: тулбар первым элементом — воркспейс врежет «← Назад».

    Принимает ЛЮБЫЕ kwargs: у настоящих вкладок этапа они разные
    (`FrameTab` — api_client, `CvatTab` — cvat_url/cvat_task_id/cvat_job_id),
    а сценарий здесь про то, что происходит ПОСЛЕ `confirmed`.
    """

    confirmed = Signal()
    status_message = Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(kwargs.get("parent"))
        self._confirmed = False
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def has_unsaved_changes(self):
        return False

    def set_project_code(self, code):
        pass


class FakeMsgBox:
    """Подмена модального диалога: зонд обязан падать, а не виснуть (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append("question")
        return cls.StandardButton.Yes

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append("warning")
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append("information")
        return cls.StandardButton.Ok


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Воркспейс на поддельном сервере; обе вкладки этапа — заглушки."""
    FakeMsgBox.calls = []
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr("ui.tabs.frame_tab.FrameTab", StubTab)
    monkeypatch.setattr("ui.tabs.cvat_tab.CvatTab", StubTab)
    monkeypatch.setattr("ui.tabs.contour_tab.ContourTab", StubTab)

    made = []

    def _make(status):
        server = FakeServer(status)
        provider = WatchProbe()
        ws = dw.DiagramWorkspace(FakeAPI(server), provider)
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        return ws, server, provider

    yield _make

    # Свои виджеты и таймеры набор сносит сам, детерминированно (`PROTOCOL §5`,
    # форма 1-36): брошенные живут до конца процесса и штрафуют СОСЕДА.
    for ws in made:
        ws._stop_ocr_poll()
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _poller_dropped_the_watch(provider):
    """ЧУЖОЕ действие-предыстория: опрос увидел финальный статус и снял слежение.

    Это не выдумка стенда, а боевая ветка `StatusProvider._poll`: на статусе из
    `_FINAL_STATUSES` без бегущих стадий он зовёт `unwatch` сам.
    """
    provider.unwatch(UID)
    provider.calls.clear()
    assert not provider.is_watching(UID)


def _open(ws, key):
    """Нажать кнопку этапа и получить открывшуюся вкладку."""
    ws._action_buttons[key].click()
    assert ws._active_tab is not None, f"вкладка «{key}» не открылась"
    return ws._active_tab


# ── сторожа стенда ───────────────────────────────────────────────────────

def test_final_statuses_really_drop_the_watch():
    """Предыстория теста — боевое поведение, а не допущение стенда.

    Оба статуса входа в двери блока 2 достижимы ПОСЛЕ финального: `detected`
    и `validated_bbox` в перечне есть, и на них опрос снимает слежение сам.
    Литералы независимы от клиента; сторож — единственное место сверки.
    """
    final = {s.value for s in StatusProvider._FINAL_STATUSES}
    assert "detected" in final, "вход в разметку недостижим после снятия слежения"
    assert "validated_bbox" in final
    assert "contours_extracted" in final, (
        "вход в контуры недостижим после снятия слежения — сценарий не тот")
    assert "detecting" not in final, "детекция объявлена финальной — опрос бы не шёл"
    assert "segmenting" not in final


def test_doors_name_real_statuses():
    """Ключи таблицы дверей — существующие статусы, а не опечатки."""
    for entry, target in DOORS.values():
        assert DiagramStatus(entry.value) is entry
        assert DiagramStatus(target.value) is target


# ── дефект приёмки: обе двери ────────────────────────────────────────────

@pytest.mark.parametrize("door", sorted(DOORS))
def test_confirm_wakes_the_status_watch(door, bench):
    """После подтверждения этапа клиент СЛЕДИТ за схемой, которую двинул сервер.

    Утверждается РАЗНИЦА, а не совпадение с состоянием «до»: слежение снято
    предысторией (`_poller_dropped_the_watch`), и зелёным тест может стать
    только если его вернуло само подтверждение.
    """
    entry, target = DOORS[door]
    ws, server, provider = bench(entry)

    _poller_dropped_the_watch(provider)

    tab = _open(ws, door)
    assert not provider.is_watching(UID), (
        "слежение живо ещё до подтверждения — предыстория не сложилась")

    if door == "frame":
        # Сервер уже перевёл диаграмму: `/frame/complete` ставит детекцию (Б9).
        server.confirm(target)
    tab.confirmed.emit()

    assert server.status is target, "сервер не двинул конвейер — сценарий не тот"
    assert provider.is_watching(UID), (
        "после подтверждения слежение мертво: сервер считает, клиент слеп "
        "до ручного «Обновить»")


@pytest.mark.parametrize("door", sorted(DOORS))
def test_confirm_does_not_unwatch(door, bench):
    """Обратная полярность: подтверждение слежение не СНИМАЕТ.

    Без этого утверждения «лечением» сошёл бы любой лишний вызов `watch`
    вперемешку с `unwatch` — множество осталось бы непустым случайно.
    """
    entry, target = DOORS[door]
    ws, server, provider = bench(entry)
    _poller_dropped_the_watch(provider)

    tab = _open(ws, door)
    if door == "frame":
        server.confirm(target)
    tab.confirmed.emit()

    assert ("unwatch", UID) not in provider.calls[-2:], (
        "последним действием подтверждения оказалось снятие слежения")


@pytest.mark.parametrize("door", sorted(DOORS))
def test_live_watch_survives_the_confirm(door, bench):
    """Смешанное состояние: слежение БЫЛО живо — оно остаётся живым.

    Правка обязана быть идемпотентной: `watch` — множество uid, повторный вызов
    ничего не ломает. Прогон с ЖИВЫМ слежением обязателен рядом с прогоном
    с мёртвым, иначе агрегатный вердикт «слежение есть» верен по построению.
    """
    entry, target = DOORS[door]
    ws, server, provider = bench(entry)

    assert provider.is_watching(UID), "`load_diagram` не включил слежение"

    tab = _open(ws, door)
    if door == "frame":
        server.confirm(target)
    tab.confirmed.emit()

    assert provider.is_watching(UID)
    assert provider.watched == {UID}, "в слежение попал чужой uid"
