# -*- coding: utf-8 -*-
"""Пункт 1.20 дороги — липкий флаг `_check_masks_completion`.

Зачем. Подтверждение маски труб отправляет на сервер `complete_mask_validation`
— это команда «валидация масок завершена»: по ней сервер переводит статус в
`validated_masks` и диспатчит `task_skeletonize_simple`
(`app/api/validation.py:493-503`). Отправляет её `_check_masks_completion`
(`ui/widgets/diagram_workspace.py`), а сторожит отправку один флаг
`_pipe_confirmed`.

Флаг ЛИПКИЙ: он поднимается при подтверждении (`_on_pipe_confirmed`) или
двойной страховкой (`_detect_saved_mask`), а снимается только в `__init__` и в
`load_diagram` — то есть при заходе в ДРУГУЮ диаграмму. Внутри одного сеанса он
остаётся поднятым навсегда. Второй адрес пункта — хвост `_close_active_tab`:
`_check_masks_completion()` там зовётся БЕЗУСЛОВНО, при закрытии ЛЮБОЙ вкладки,
а не только вкладки труб.

Отсюда дефект в формулировке пункта: **отказ оператора превращается в
подтверждение**. Оператор открывает следующую вкладку (перекрёстки, схема,
контуры, привязка), правит, жмёт «← Назад» и на вопрос «Есть несохранённые
изменения. Сохранить перед закрытием?» отвечает **Нет** — а клиент вместо
«ничего не делать» отправляет на сервер команду о завершении валидации масок.
Сам `_check_masks_completion` при этом ничего не спрашивает: вопрос задаёт
диалог закрытия (ловушка `PLAN_AUDIT §Этап 1`, п. 1.20).

Что эта отправка покупает на сервере (`app/api/validation.py:481-503`): пока
статус ещё `validating_masks` / `skeletonized` / `validated_masks`, повторный
вызов НЕ считается «ушли вперёд» и диспатчит скелетизацию заново — это питает
гонку двойной скелетизации (пункт 1.x2). Позже по цепочке он либо тихо вернёт
`task_id=None`, либо отдаст 400, который клиент проглотит в `except Exception`.

Что проверяется — только ДАННЫЕ (принцип набора 0.4): журнал команд,
отправленных клиентом на поддельный сервер. Ни одного утверждения про
`_pipe_confirmed`, `_active_tab_key` и прочие внутренние поля — тест обязан
пережить декомпозицию воркспейса. Числа абсолютные: одно подтверждение — ровно
одна команда, два подтверждения — ровно две; до правки жест из
`test_refusal_to_save_is_not_a_confirmation` давал ДВЕ команды на ОДНО
подтверждение.

Модальный диалог в пути закрытия подменяется утверждением о факте вызова, а не
таймаутом (`PROTOCOL §5`): без подмены набор повис бы, а не упал.
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
from ui.services.api_client import APIError, DiagramStatus   # noqa: E402

UID = "5f2c81ae-7777-8888-9999-aaaabbbbcccc"

# Контракт эндпоинта `POST /api/validation/{uid}/masks/complete`
# (`app/api/validation.py:406-417` и `:481-503`) — воспроизведён здесь, потому
# что от него зависит, чем обернётся лишняя команда клиента: вне списка
# принимаемых статусов это 400, внутри «ушли вперёд» — тихий `task_id=None`,
# а до него — ПОВТОРНЫЙ диспатч скелетизации (гонка пункта 1.x2).
_MASKS_COMPLETE_ACCEPTS = {
    DiagramStatus.VALIDATING_MASKS, DiagramStatus.SKELETONIZED,
    DiagramStatus.VALIDATED_MASKS, DiagramStatus.SKELETONIZING_FINAL,
    DiagramStatus.SKELETONIZED_FINAL, DiagramStatus.DETECTING_JUNCTIONS,
    DiagramStatus.DETECTED_JUNCTIONS, DiagramStatus.BUILT,
    DiagramStatus.VALIDATED_GRAPH, DiagramStatus.OCR_COMPLETED,
}
_MASKS_COMPLETE_ALREADY_PAST = {
    DiagramStatus.SKELETONIZING_FINAL, DiagramStatus.SKELETONIZED_FINAL,
    DiagramStatus.DETECTING_JUNCTIONS, DiagramStatus.DETECTED_JUNCTIONS,
    DiagramStatus.VALIDATED_JUNCTIONS, DiagramStatus.BUILDING_GRAPH,
    DiagramStatus.BUILT, DiagramStatus.VALIDATED_GRAPH,
    DiagramStatus.OCR_COMPLETED,
}


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeServer:
    """Сервер: статус + журнал команд, полученных от клиента.

    Переходы повторяют боевые ровно в той части, которая видна клиенту:
    `masks/complete` — по контракту выше, `junctions/start` уводит в
    `validating_junctions`, откат — в целевой статус.
    """

    def __init__(self, status):
        self.status = status
        self.mask_completions = []   # статус на момент каждой команды
        self.skeletonize_dispatches = 0
        self.rollbacks = []
        # Брокер лёг: с ноги 1.16 эндпоинт возвращает состояние и отвечает 503
        # (`app/api/validation.py: _restore_and_fail`), а не 200 с `task_id: null`.
        self.broker_down = False

    def complete_masks(self):
        # Команда засчитывается фактом отправки: сам отказ считается тоже.
        self.mask_completions.append(self.status)
        if self.broker_down and self.status in _MASKS_COMPLETE_ACCEPTS                 and self.status not in _MASKS_COMPLETE_ALREADY_PAST:
            # Состояние возвращено сервером — статус НЕ двигается.
            raise APIError(
                "Worker unavailable: не удалось поставить задачу (скелетизации)",
                503,
            )
        if self.status not in _MASKS_COMPLETE_ACCEPTS:
            raise APIError(
                f"Cannot complete validation: status is '{self.status.value}'",
                400,
            )
        if self.status in _MASKS_COMPLETE_ALREADY_PAST:
            return {"status": "validated_masks", "task_id": None}
        self.status = DiagramStatus.VALIDATED_MASKS
        self.skeletonize_dispatches += 1
        return {"status": "validated_masks",
                "task_id": f"task-{self.skeletonize_dispatches}"}


class FakeDiagram:
    def __init__(self, server):
        self.status = server.status
        self.error_stage = None
        self.project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность APIClient, которой воркспейс пользуется в этом сценарии."""

    def __init__(self, server):
        self.server = server

    def get_diagram(self, uid):
        return FakeDiagram(self.server)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_stages(self, uid):
        return []

    def complete_mask_validation(self, uid):
        return self.server.complete_masks()

    def start_mask_validation(self, uid):
        self.server.status = DiagramStatus.VALIDATING_MASKS
        return {"status": "validating_masks"}

    def start_junction_validation(self, uid):
        self.server.status = DiagramStatus.VALIDATING_JUNCTIONS
        return {"status": "validating_junctions"}

    def rollback_diagram(self, uid, target_status,
                         preserve_ocr=False, preserve_contours=False):
        self.server.rollbacks.append(target_status)
        self.server.status = DiagramStatus(target_status)
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


class StatusInfo:
    def __init__(self, status):
        self.status = status
        self.error_stage = None


class StubTab(QWidget):
    """Вкладка-заглушка: тулбар первым элементом — воркспейс врежет «← Назад».

    `unsaved` — то, что оператор наредактировал и не сохранил;
    `_confirmed` — флаг самой вкладки, который читает двойная страховка.
    """

    confirmed = Signal()
    status_message = Signal(str)

    def __init__(self, diagram_uid=None, diagram_name=None, api_client=None,
                 parent=None):
        super().__init__(parent)
        self._confirmed = False
        self.unsaved = False
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def has_unsaved_changes(self):
        return self.unsaved

    def set_project_code(self, code):
        pass


class FakeMsgBox:
    """Подмена модального диалога: он штатен в пути закрытия, и зонд по нему
    обязан падать, а не виснуть (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @staticmethod
    def _seen(kind, args):
        """(вид, заголовок, ТЕКСТ). Текст нужен, чтобы судить не факт окна,
        а что в нём написано: «Ошибка» стоит заголовком у всех четырёх."""
        return (kind,
                args[1] if len(args) > 1 else "",
                args[2] if len(args) > 2 else "")

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(cls._seen("question", args))
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(cls._seen("warning", args))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(cls._seen("information", args))
        return cls.StandardButton.Ok


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Воркспейс на поддельном сервере; трубы и перекрёстки — заглушки."""
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr("ui.tabs.pipe_tab.PipeTab", StubTab)
    monkeypatch.setattr("ui.tabs.junction_tab.JunctionTab", StubTab)

    made = []

    def _make(status):
        server = FakeServer(status)
        ws = dw.DiagramWorkspace(FakeAPI(server), FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        return ws, server

    yield _make

    # Свои воркспейсы набор сносит сам, детерминированно (`PROTOCOL §5`,
    # форма 1-36): брошенные виджеты живут до конца процесса и штрафуют
    # СОСЕДА — доставку событий и его же `processEvents()`.
    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ── жесты оператора ──────────────────────────────────────────────────────

def _open(ws, key):
    """Нажать кнопку этапа и получить открывшуюся вкладку."""
    ws._action_buttons[key].click()
    assert ws._active_tab is not None, f"вкладка «{key}» не открылась"
    return ws._active_tab


def _back(ws, answer):
    """«← Назад» с заранее выбранным ответом на вопрос о несохранённом."""
    FakeMsgBox.answer = answer
    ws._btn_back_injected.click()


def _poll(ws, server, status):
    """Опрос статуса принёс новое состояние с сервера."""
    server.status = status
    ws.status_provider.status_updated.emit(UID, StatusInfo(status))


# ── дефект пункта ────────────────────────────────────────────────────────

def test_refusal_to_save_is_not_a_confirmation(bench):
    """Отказ сохранять в ЧУЖОЙ вкладке не отправляет команду о масках.

    Жест: подтвердил трубы → цепочка дошла до перекрёстков → открыл
    перекрёстки, поправил, «← Назад» → «Нет, не сохранять».
    До правки клиент отправлял `complete_mask_validation` ДВАЖДЫ: второй раз —
    ровно на отказе оператора.
    """
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    _open(ws, "pipe").confirmed.emit()
    assert len(server.mask_completions) == 1, "подтверждение труб не ушло"

    _poll(ws, server, DiagramStatus.DETECTED_JUNCTIONS)

    tab = _open(ws, "junction")
    tab.unsaved = True
    _back(ws, QMessageBox.StandardButton.No)

    assert ws._active_tab is None, "вкладка перекрёстков не закрылась"
    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"жест прошёл мимо диалога об отказе: {FakeMsgBox.calls}"
    )
    assert len(server.mask_completions) == 1, (
        f"отказ оператора ушёл на сервер подтверждением масок: команд "
        f"{len(server.mask_completions)}, статусы "
        f"{[s.value for s in server.mask_completions]}"
    )


def test_second_look_into_pipe_tab_does_not_redispatch(bench):
    """«Подтвердил → тут же зашёл посмотреть → вышел» не запускает скелет дважды.

    Самое опасное окно: сервер уже в `validated_masks`, но это ещё НЕ «ушли
    вперёд» (`app/api/validation.py:481-491`), поэтому повторная команда
    диспатчит `task_skeletonize_simple` второй раз — та самая гонка двойной
    скелетизации. Вкладка здесь та же самая, труб: различить эти два выхода
    по ключу вкладки нельзя, разница только в том, было подтверждение или нет.
    """
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    _open(ws, "pipe").confirmed.emit()
    assert server.skeletonize_dispatches == 1, "подтверждение не запустило скелет"

    _open(ws, "pipe")                       # зашёл посмотреть на подтверждённое
    _back(ws, QMessageBox.StandardButton.Yes)

    assert FakeMsgBox.calls == [], f"лишние диалоги: {FakeMsgBox.calls}"
    assert server.skeletonize_dispatches == 1, (
        f"скелетизация запущена {server.skeletonize_dispatches} раз"
    )
    assert len(server.mask_completions) == 1, (
        f"выход без правок отправил команду повторно: команд "
        f"{len(server.mask_completions)}"
    )


def test_clean_close_of_other_tab_sends_nothing(bench):
    """Чистое «зашёл — вышел» в чужой вкладке тоже ничего не отправляет.

    Тот же липкий флаг, но без диалога: ни одного вопроса, ни одной команды
    сверх единственного подтверждения.
    """
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    _open(ws, "pipe").confirmed.emit()
    _poll(ws, server, DiagramStatus.DETECTED_JUNCTIONS)

    for _ in range(3):
        _open(ws, "junction")
        _back(ws, QMessageBox.StandardButton.Yes)

    assert FakeMsgBox.calls == [], f"лишние диалоги: {FakeMsgBox.calls}"
    assert len(server.mask_completions) == 1, (
        f"три выхода из чужой вкладки дали {len(server.mask_completions)} команд"
    )


# ── порог с другой стороны: что обязано продолжать работать ──────────────

def test_confirmation_sends_exactly_one_command(bench):
    """Подтверждение труб отправляет команду — ровно одну."""
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    _open(ws, "pipe").confirmed.emit()

    assert len(server.mask_completions) == 1
    assert server.mask_completions[0] == DiagramStatus.VALIDATING_MASKS


def test_double_safety_on_pipe_close_still_fires(bench):
    """Двойная страховка жива: сигнал не дошёл, но вкладка помечена подтверждённой.

    Ради этого пути `_check_masks_completion` и стоит в закрытии вкладки.
    Проверка запирает правку с другой стороны: убрать лишние отправки — не
    значит убрать нужную.
    """
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    tab = _open(ws, "pipe")
    tab._confirmed = True            # `_on_confirm` отработал, сигнал потерян
    _back(ws, QMessageBox.StandardButton.Yes)

    assert ws._active_tab is None, "вкладка труб не закрылась"
    assert len(server.mask_completions) == 1, (
        f"двойная страховка молчит: команд {len(server.mask_completions)}"
    )


def test_second_confirmation_sends_second_command(bench):
    """Оператор откатил этап и подтвердил заново — команда уходит второй раз.

    Жест целиком через кнопки: статус доехал до `validated_masks`, кнопка
    «Проверка труб» стала пройденной, оператор согласился на откат до
    `skeletonized`, вкладка открылась заново, подтвердил.
    """
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    _open(ws, "pipe").confirmed.emit()
    _poll(ws, server, DiagramStatus.VALIDATED_MASKS)

    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    _open(ws, "pipe").confirmed.emit()

    assert server.rollbacks == ["skeletonized"], (
        f"откат оператора ушёл не так: {server.rollbacks}"
    )
    assert len(server.mask_completions) == 2, (
        f"второе подтверждение не ушло: команд {len(server.mask_completions)}"
    )


# ── доработка ноги 1.16: отказ отправки виден оператору ──────────────────

def test_a_refused_dispatch_is_shown_to_the_operator(bench):
    """503 «задача не поставлена» больше не глотается молча.

    До ноги 1.16 отказ брокера приходил сюда как 200 с `task_id: null`, и
    комментарий в обработчике был прав: исключение и правда означало только
    «цепочка ушла вперёд», о котором оператору говорить нечего. Теперь 503
    означает обратное — задача НЕ поставлена, сервер вернул состояние, и
    повторять придётся оператору. Молчащий `logger.error` до него это не
    доносит: клиент — единственное место, где оператор вообще что-то видит.

    Форма взята у трёх соседей того же файла (`_on_junction_confirmed`,
    `_on_simple_graph_confirmed`, `_on_graph_confirmed`) — окно `warning`,
    а не строка статуса: у соседей оператор жмёт «Подтвердить» ровно так же.

    Утверждается РАЗНИЦА (`PROTOCOL §3`): тот же жест на живом брокере окна
    не показывает вовсе. Иначе тест был бы зелёным при любом поведении.
    """
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)
    server.broker_down = True

    _open(ws, "pipe").confirmed.emit()

    assert len(server.mask_completions) == 1, "команда до сервера не дошла"
    assert server.status is DiagramStatus.VALIDATING_MASKS, (
        "поддельный сервер сдвинул статус — сценарий не тот, что на бою"
    )
    assert [c[0] for c in FakeMsgBox.calls] == ["warning"], (
        f"оператору ничего не показали: {FakeMsgBox.calls}"
    )
    shown = FakeMsgBox.calls[0][2]
    assert "валидацию масок" in shown, (
        f"окно не называет, ЧТО не удалось: {shown}"
    )
    assert "не удалось поставить задачу" in shown, (
        f"причина сервера до оператора не доехала: {shown}"
    )


def test_a_successful_dispatch_stays_silent(bench):
    """Порог с другой стороны: удачное подтверждение окон не открывает."""
    ws, server = bench(DiagramStatus.VALIDATING_MASKS)

    _open(ws, "pipe").confirmed.emit()

    assert server.status is DiagramStatus.VALIDATED_MASKS
    assert FakeMsgBox.calls == [], f"лишние диалоги: {FakeMsgBox.calls}"
