# -*- coding: utf-8 -*-
"""Пункт 1.18 дороги — `closeEvent` главного окна клиента.

Зачем. У `MainWindow` (`ui/windows/main_window.py`) не было `closeEvent` вовсе:
крестик окна убивал процесс мимо всего. Три следствия, каждое замерено этим
набором ДО правки (MEASUREMENTS §74):

1. **Правки открытой вкладки уходили молча.** Оператор правит схему, жмёт ✕ —
   вопроса нет, вкладка сносится вместе с процессом. Путь «← Назад» такой
   вопрос задаёт (`diagram_workspace._close_active_tab`), путь «закрыть
   приложение» — не задавал.
2. **Фон оставался поднятым.** `DiagramWorkspace.cleanup()` (опрос статуса,
   таймер OCR, автосохранение) звался только из «← Назад», при выходе из
   приложения — никогда.
3. **В логе клиента (пункт 1.10) не оставалось ни строки о конце сеанса.**
   В собранном `.exe` (`console=False`) файл лога — единственный след:
   `sys.stdout` и `sys.stderr` там `None`, консольный хендлер молча пустеет.

Гейт пункта — сценарный (`test_close_line_lands_in_the_log_file`): клиент
поднят своей же точкой входа `ui.main.main()` ОТДЕЛЬНЫМ ПРОЦЕССОМ с мёртвой
консолью (условия `.exe`), окно закрывается в живом цикле событий, строка
ищется В ФАЙЛЕ. Юниты рядом запирают развилки вопроса о несохранённом.

Проверяются ДАННЫЕ (принцип набора 0.4): что вернул `QWidget.close()`, журнал
диалогов, журнал сохранений вкладки, журнал подписок провайдера статуса. Ни
одного утверждения про внутренние флаги окна — тест обязан пережить
декомпозицию UI (этап 10).

Модальный диалог в пути закрытия подменяется утверждением о факте вызова, а не
таймаутом (`PROTOCOL §5`): без подмены набор повис бы, а не упал.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal                 # noqa: E402
from PySide6.QtWidgets import (                            # noqa: E402
    QApplication, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget,
)

import ui.windows.main_window as mw                        # noqa: E402
from ui.services.api_client import DiagramStatus           # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

UID = "0fc9d04c-1111-2222-3333-444455556666"

# Литералы сценария. Намеренно НЕ импортируются из проверяемого модуля: тест,
# вычисляющий свой вход из проверяемой константы, зелен при любом её значении
# (запрет `PROTOCOL §3`).
CLOSING_LINE = "Клиент закрывается"
LOG_FILE_NAME = "client.log"
SCENARIO_UID = "0fc9d04c"


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeDiagram:
    status = DiagramStatus.UPLOADED
    error_stage = None
    project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность `APIClient`, которой пользуются окно и воркспейс."""

    def __init__(self, *args, **kwargs):
        self.base_url = "http://fake"
        self.closed = 0

    def health_check(self):
        return True

    def list_projects(self):
        return [{"code": "thermohydraulics", "name": "Термогидравлика"}]

    def get_diagram(self, uid):
        return FakeDiagram()

    def get_stages(self, uid):
        return []

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def close(self):
        self.closed += 1


class FakeStatusProvider(QObject):
    """Провайдер с журналом подписок: по нему видно, погашен ли фон."""

    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)
    error_occurred = Signal(str, str)

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.watched = []
        self.unwatched = []

    def watch(self, uid):
        self.watched.append(uid)

    def unwatch(self, uid):
        self.unwatched.append(uid)

    def is_watching(self, uid):
        return uid in self.watched and uid not in self.unwatched


class StubTab(QWidget):
    """Вкладка-заглушка: тулбар первым элементом — воркспейс врежет «← Назад».

    `unsaved` — то, что оператор наредактировал; `saves` — журнал вызовов
    сохранения (по нему видно, ушёл ли батч перед выходом); `save_ok` —
    ответил ли сервер успехом.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.unsaved = False
        self.save_ok = True
        self.saves = []
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def has_unsaved_changes(self):
        return self.unsaved

    def _save_graph(self):
        self.saves.append(self.unsaved)
        if self.save_ok:
            self.unsaved = False
        return self.save_ok


class FakeMsgBox:
    """Подмена модального диалога (`PROTOCOL §5`: зонд обязан падать, не виснуть)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[1] if len(args) > 1 else ""))
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Ok


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch):
    """Главное окно на поддельном сервере, с открытой диаграммой."""
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(mw, "APIClient", FakeAPI)
    monkeypatch.setattr(mw, "StatusProvider", FakeStatusProvider)
    monkeypatch.setattr(mw, "QMessageBox", FakeMsgBox)

    win = mw.MainWindow()
    win.show()
    win.workspace.load_diagram(UID, "схема оператора")
    yield win
    win.hide()
    win.deleteLater()


def _open_tab(win, unsaved=False, save_ok=True):
    """Оператор открыл вкладку редактора и (может быть) наредактировал."""
    tab = StubTab()
    tab.unsaved = unsaved
    tab.save_ok = save_ok
    win.workspace._open_tab(tab, "val_graph")
    return tab


# ── дефект пункта: правки оператора ──────────────────────────────────────

def test_dirty_tab_is_asked_before_exit(window):
    """✕ при несохранённых правках задаёт вопрос — ровно один раз."""
    _open_tab(window, unsaved=True)

    window.close()

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"выход прошёл мимо вопроса о несохранённом: {FakeMsgBox.calls}"
    )


def test_yes_saves_before_exit(window):
    """«Сохранить» → правка ушла на сервер, и только потом клиент закрылся."""
    tab = _open_tab(window, unsaved=True)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    closed = window.close()

    assert tab.saves == [True], f"сохранение не вызвано: {tab.saves}"
    assert closed is True, "клиент не закрылся после успешного сохранения"


def test_cancel_keeps_client_open_and_tab_alive(window):
    """«Отмена» → клиент открыт, вкладка на месте, ничего не сохранено."""
    tab = _open_tab(window, unsaved=True)
    FakeMsgBox.answer = QMessageBox.StandardButton.Cancel

    closed = window.close()

    assert closed is False, "отмена не удержала клиент открытым"
    assert window.workspace._active_tab is tab, "вкладка снесена при отмене выхода"
    assert tab.saves == [], f"отмена вызвала сохранение: {tab.saves}"
    assert tab.unsaved is True, "правки оператора потеряны при отмене выхода"


def test_failed_save_does_not_close_the_client(window):
    """Сервер отказал в сохранении → клиент не закрывается, работа не потеряна."""
    tab = _open_tab(window, unsaved=True, save_ok=False)
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes

    closed = window.close()

    assert tab.saves == [True], f"сохранение не вызвано: {tab.saves}"
    assert closed is False, "клиент закрылся, унеся несохранённую работу"
    assert window.workspace._active_tab is tab, "вкладка снесена при неудаче сохранения"


def test_no_closes_without_saving(window):
    """«Не сохранять» → выход без обращения к серверу, это выбор оператора."""
    tab = _open_tab(window, unsaved=True)
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    closed = window.close()

    assert tab.saves == [], f"отказ сохранять всё равно сохранил: {tab.saves}"
    assert closed is True, "клиент не закрылся на явном отказе сохранять"


def test_clean_tab_closes_without_a_question(window):
    """Сохранённая вкладка вопроса не поднимает."""
    _open_tab(window, unsaved=False)

    closed = window.close()

    assert FakeMsgBox.calls == [], f"лишний вопрос на чистой вкладке: {FakeMsgBox.calls}"
    assert closed is True, "клиент не закрылся при чистой вкладке"


def test_close_without_any_tab_is_silent(window):
    """Закрытие из списка диаграмм — без диалогов."""
    closed = window.close()

    assert FakeMsgBox.calls == [], (
        f"лишний диалог при закрытии без вкладки: {FakeMsgBox.calls}"
    )
    assert closed is True, "клиент не закрылся из списка диаграмм"


# ── дефект пункта: фон и след ────────────────────────────────────────────

def test_close_stops_background_work(window):
    """Выход гасит опрос статуса и закрывает вкладку (`cleanup()`)."""
    _open_tab(window, unsaved=False)

    window.close()

    assert window.status_provider.unwatched == [UID], (
        f"опрос статуса не погашен при выходе: {window.status_provider.unwatched}"
    )
    assert window.workspace._active_tab is None, "вкладка осталась открытой после выхода"


def test_close_writes_a_line_into_the_log(window, caplog):
    """Конец сеанса виден в логе (Д3: подсистема сообщает и о нормальной работе)."""
    with caplog.at_level(logging.INFO):
        window.close()

    assert any(CLOSING_LINE in r.getMessage() for r in caplog.records), (
        f"о закрытии клиента в лог не написано: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_cancelled_close_is_also_visible_in_the_log(window, caplog):
    """Отменённый выход тоже оставляет след — иначе «клиент не закрылся» нечем объяснить."""
    _open_tab(window, unsaved=True)
    FakeMsgBox.answer = QMessageBox.StandardButton.Cancel

    with caplog.at_level(logging.INFO):
        window.close()

    messages = [r.getMessage() for r in caplog.records]
    assert any("отмен" in m.lower() for m in messages), (
        f"отменённое закрытие не оставило следа: {messages}"
    )


# ── сценарий уровня дефекта (гейт пункта) ────────────────────────────────

SCENARIO = '''
import os, sys, traceback
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import ui.main as m
import ui.windows.main_window as mw
from ui.services.client_logging import bind_uid


class FakeAPI:
    def __init__(self, *a, **kw):
        self.base_url = "http://fake"

    def health_check(self):
        return True

    def list_projects(self):
        return [{{"code": "thermohydraulics", "name": "ТГ"}}]

    def close(self):
        pass


class BootApp(QApplication):
    """Тот же старт, что у оператора, только сеанс короткий: открылся — закрыли."""

    def exec(self):
        QTimer.singleShot(0, self._close_window)
        QTimer.singleShot(20000, self.quit)   # страховка от висяка
        return super().exec()

    def _close_window(self):
        for w in self.topLevelWidgets():
            if type(w).__name__ == "MainWindow":
                w.close()


mw.APIClient = FakeAPI
m.QApplication = BootApp

# Условия собранного клиента: console=False -> ни stdout, ни stderr.
sys.stdout = None
sys.stderr = None
try:
    bind_uid({uid!r})
    m.main()
except SystemExit as exc:
    open({crash!r}, "w", encoding="utf-8").write("SystemExit=%r\\n" % (exc.code,))
except BaseException:
    open({crash!r}, "w", encoding="utf-8").write(traceback.format_exc())
'''


def test_close_line_lands_in_the_log_file(tmp_path):
    """ГЕЙТ ПУНКТА: клиент закрыт в живом цикле Qt → строка В ФАЙЛЕ лога.

    Мёртвая консоль — не декорация: в собранном `.exe` (`console=False`)
    консольный хендлер молча пустеет, и файл обязан быть самодостаточным.
    """
    logs = tmp_path / "logs"
    crash = tmp_path / "crash.txt"
    script = tmp_path / "boot_and_close.py"
    script.write_text(
        SCENARIO.format(uid=SCENARIO_UID, crash=str(crash)), encoding="utf-8"
    )
    env = dict(os.environ, PID_LOG_DIR=str(logs), QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    note = crash.read_text(encoding="utf-8") if crash.exists() else ""
    path = logs / LOG_FILE_NAME
    if not path.exists():
        pytest.fail(f"файла лога нет: {path}\nrc={proc.returncode}\n{note}")
    text = path.read_text(encoding="utf-8")

    assert CLOSING_LINE in text, f"закрытие клиента не записано в файл:\n{text}\n{note}"
    closing = [ln for ln in text.splitlines() if CLOSING_LINE in ln]
    assert f"uid={SCENARIO_UID}" in closing[0], (
        f"строка закрытия без корреляции по uid: {closing[0]!r}"
    )
    assert proc.returncode == 0, (
        f"клиент не пережил закрытие: rc={proc.returncode}\n{note}"
    )
