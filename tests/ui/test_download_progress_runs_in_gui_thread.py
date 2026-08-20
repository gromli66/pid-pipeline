# -*- coding: utf-8 -*-
"""Пункт 1.x17 — ход загрузки обязан рисоваться в GUI-потоке.

Зачем. Все четыре вкладки показывали ход загрузки так::

    self._downloader.progress.connect(
        lambda msg: self.status_label.setText(msg)          # junction_tab:223

Получателя-`QObject` у такой связи нет, поэтому Qt не может ни вычислить
поток получателя, ни привязать её к чьему-то времени жизни: связь
принадлежит ОТПРАВИТЕЛЮ, а отправитель (`ArtifactDownloader`) переехал
`moveToThread` в рабочий поток. Значит соединение прямое, и `setText`
у `QLabel` исполняется В РАБОЧЕМ ПОТОКЕ — на каждом артефакте, у каждой
из четырёх вкладок, в бою.

Виджеты Qt не потокобезопасны: рисовать их можно только из GUI-потока
(`QWidget` документирован как не reentrant). Это класс `access violation`,
а не косметика, и лечится он получателем-`QObject`: со `@Slot`-ом вкладки
`Qt.AutoConnection` разворачивается в очередь и вызов уезжает в GUI-поток.

Наблюдаемое здесь — **идентификатор потока, в котором исполнился вызов**
(`threading.get_ident()`), а не текст метки: текст одинаков в обеих
редакциях, и тест на нём был бы зелёным всегда. Замер «до» — в §102.

⛔ Обёртка ставится на ЭКЗЕМПЛЯР метки уже после старта загрузки: первая
строка прогресса летит из `run()` раньше, чем `__init__` вкладки вернёт
управление, и перехватить её нечем. Поэтому сервер ДЕРЖИТ артефакты на
`threading.Event` — к моменту установки обёртки рабочий поток заведомо
стоит внутри `download_artifact`, а все строки «Загружен …» ещё впереди.
Утверждения строятся на факте перехвата, а не на времени.
"""
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent                                # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

import ui.tabs.base_graph_tab as base_graph_tab                  # noqa: E402
import ui.tabs.junction_tab as junction_tab                      # noqa: E402
import ui.tabs.ocr_binding_tab as ocr_binding_tab                # noqa: E402
import ui.tabs.pipe_tab as pipe_tab                              # noqa: E402
from ui.tabs.junction_tab import JunctionTab                     # noqa: E402
from ui.tabs.ocr_binding_tab import OcrBindingTab                # noqa: E402
from ui.tabs.pipe_tab import PipeTab                             # noqa: E402
from ui.tabs.simple_graph_tab import SimpleGraphTab              # noqa: E402

from tests.ui.test_tab_close_stops_threads import (              # noqa: E402
    UID, FakeMsgBox, HoldingAPI, blobs, qapp,
)

__all__ = ["blobs", "qapp"]      # фикстуры, взятые у соседнего набора

#: Предохранитель от зависания набора; утверждения на нём не строятся.
PUMP_S = 20.0

#: вкладка → (модуль с её `QMessageBox`, класс, имя метки хода загрузки).
#: `SimpleGraphTab` взят представителем `BaseGraphTab`: лямбда живёт
#: в базовом `_download_artifacts` (`base_graph_tab.py:866`).
TABS = {
    "junction": (junction_tab, JunctionTab, "status_label"),
    "pipe": (pipe_tab, PipeTab, "status_label"),
    "val_graph": (base_graph_tab, SimpleGraphTab, "status_label"),
    "ocr_binding": (ocr_binding_tab, OcrBindingTab, "loading_label"),
}


@pytest.fixture
def tab_bench(qapp, blobs, monkeypatch):
    """Настоящая вкладка на держащем сервере; её метка — под обёрткой."""
    made = []

    def _make(key):
        module, cls, label_name = TABS[key]
        monkeypatch.setattr(module, "QMessageBox", FakeMsgBox)
        FakeMsgBox.calls = []
        FakeMsgBox.answer = QMessageBox.StandardButton.No

        api = HoldingAPI(blobs, None)
        tab = cls(UID, "проба 1.x17", api)
        made.append(tab)
        assert api.wait_until_downloading(), "рабочий поток не дошёл до загрузки"

        label = getattr(tab, label_name)
        seen = []
        original = label.setText

        def recording(msg):
            seen.append((threading.get_ident(), msg))
            original(msg)

        label.setText = recording
        return tab, api, seen

    yield _make

    for tab in made:
        api = tab.api_client
        api.release()
        if hasattr(tab, "cleanup"):
            tab.cleanup()
        # ⛔ Поток перечитывается КАЖДЫЙ РАЗ: вкладка привязки OCR на отказе
        # заводит НОВЫЙ загрузчик через `QTimer.singleShot(5000, ...)`, и
        # ссылка, взятая один раз, дождалась бы не того. Бегущий `QThread`
        # на выходе процесса — abort, уносящий весь прогон (§102).
        _pump(lambda: not _running(tab))
        tab.hide()
        tab.setParent(None)
        tab.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _running(tab) -> bool:
    thread = getattr(tab, "_download_thread", None)
    return thread is not None and thread.isRunning()


def _pump(done, seconds=PUMP_S):
    """Крутить цикл событий, пока не выполнится условие. Не утверждение."""
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if done():
            return True
        time.sleep(0.005)
    return False


def _artifact_line(seen):
    """Пришла ли строка про КОНКРЕТНЫЙ загруженный артефакт.

    Ждать «хоть чего-нибудь» здесь нельзя: первая строка («Загрузка
    артефактов…») летит из `run()` ещё до затвора, и в исправленном дереве
    она приезжает очередью — то есть ловилась бы раньше всех остальных
    и сокращала замер до одного вызова. Строки «Загружен …» идут по одной
    на артефакт и есть в ОБЕИХ редакциях.
    """
    return any(m.startswith("Загружен ") for _, m in seen)


@pytest.mark.parametrize("key", sorted(TABS))
def test_progress_paints_label_from_gui_thread(tab_bench, key):
    """Ход загрузки рисуется тем же потоком, что и всё остальное окно.

    До правки идентификатор был РАБОЧИМ (связь без получателя = прямая),
    после — совпадает с GUI-потоком.
    """
    gui_ident = threading.get_ident()
    tab, api, seen = tab_bench(key)

    api.release()
    assert _pump(lambda: _artifact_line(seen)), (
        "ход загрузки не показался ни разу — обстановка не та, "
        "утверждать нечего"
    )

    foreign = sorted({ident for ident, _ in seen if ident != gui_ident})
    assert not foreign, (
        f"вкладка «{key}»: метку хода загрузки красит НЕ GUI-поток.\n"
        f"GUI={gui_ident}, чужие={foreign}\n"
        f"строки={[m for _, m in seen]}"
    )


def test_control_progress_is_actually_shown(tab_bench):
    """Контроль честности: строки прогресса вообще доходят до метки.

    Утверждается РАЗНИЦА, а не совпадение с состоянием «до»: правка
    «отключить progress совсем» тоже убрала бы чужой поток из замера,
    и тест выше остался бы зелёным на сломанной вкладке.
    """
    tab, api, seen = tab_bench("junction")

    api.release()
    assert _pump(lambda: _artifact_line(seen)), "прогресс не дошёл до метки"

    messages = [m for _, m in seen]
    assert any(m.startswith("Загружен ") for m in messages), (
        f"вкладка не сообщила ни об одном загруженном артефакте: {messages}"
    )
