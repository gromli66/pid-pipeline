# -*- coding: utf-8 -*-
"""Фоновый поток обязан кончиться раньше, чем процесс начнёт разрушать объекты
(пункт 1-50).

⛔ **Класс тот же, что у сцен в 1-46, и здесь он доказан своим замером**
(`MEASUREMENTS §127`). `QThread`, созданный БЕЗ родителя и живущий в питоньем
поле владельца, переживает владельца: вкладку разрушает «← Назад»
(`diagram_workspace._remove_tab_widget`), рабочую область — закрытие клиента,
а поток продолжает бежать. Процесс, дошедший до выхода с бегущим `QThread`,
падает на его разрушении — `0xC0000409`. Замеры одной командой, коды возврата
сняты у самого процесса:

* уход из рабочей области при живой сборке `.prtx` — **6 крахов из 6**;
* закрытие клиента при живой сборке — **6 из 6**; без жеста вообще — **6 из 6**;
* уход из вкладки при МОЛЧАЩЕМ сервере — **4 из 4** у каждой из четырёх
  загрузочных вкладок;
* контроль (работа кончилась ДО жеста) — **0 из 6** и **0 из 4**.

⛔ **Два очевидных лекарства проверены и НЕ лечат** — поэтому дверь устроена
именно так, а не проще: удержать поток питоньей ссылкой в реестре — **4 краха
из 4**; дать ему родителя `QApplication` — **4 из 4**. Пережить выход процесса
бегущий поток не может в принципе; лечит только то, что он КОНЧАЕТСЯ раньше:
стоп + `quit` + ожидание при разрушении владельца — **0 из 6**.

Поэтому здесь две половины одной двери:

* `hand_over()` — разрушение владельца ПРОСИТ работника остановиться и гасит
  поток. Не ждёт: ожидание в слоте `destroyed` заморозило бы окно оператора;
* `install_exit_guard()` — выход клиента ДОЖИДАЕТСЯ всех, кого просили, с
  потолком в `EXIT_WAIT_MS`. Кто не успел (непрерываемый HTTP-вызов — граница
  названа в `docs/TESTING.md §6`), тот не получает шанса уронить процесс:
  клиент уходит, не разрушая объектов.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QObject, QThread, Slot
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

#: Один оборот ожидания на выходе. Считаются ОБОРОТЫ, а не секунды: в обороте
#: нет чужой работы, поэтому загруженность машины его не удорожает
#: (`PROTOCOL §3`, форма пункта 1-46).
TICK_MS = 20

#: Потолок ожидания при закрытии клиента. Число не новое — ровно столько уже
#: ждут `base_graph_tab.cleanup()` и `license_dialog.closeEvent`.
EXIT_WAIT_MS = 3000


class Stopped(RuntimeError):
    """Работник прекратил работу по просьбе клиента, а не из-за отказа.

    Отдельный класс, а не общий `RuntimeError`: у отказа сервера и у ухода
    оператора разная судьба — первый показывают окном, второй молча уводит
    поток, потому что получателя сигналов уже нет.
    """


@dataclass
class _Entry:
    """Один поток под присмотром: чем гасить и как назвать в логе."""

    thread: QThread
    worker: QObject | None
    name: str
    uid: str
    #: у работника попросили остановиться — чтобы не просить дважды
    asked: bool = field(default=False)


#: Все живые потоки клиента. Ссылка СИЛЬНАЯ и это существенно: словарь
#: разрушенной вкладки уходит вместе с ней, и без реестра бегущий поток снёс бы
#: питоний сборщик — в произвольный момент и чужим потоком.
_LIVE: list[_Entry] = []


def _running(thread: QThread) -> bool:
    """Бежит ли поток. `False`, если обёртка пережила свой C++-объект."""
    try:
        return bool(thread.isRunning())
    except RuntimeError:
        # «Internal C++ object already deleted» — поток снесли, гасить нечего
        return False


def _ask_to_stop(entry: _Entry) -> None:
    """Попросить работника остановиться и погасить цикл событий потока."""
    stop = getattr(entry.worker, "stop", None)
    if callable(stop):
        try:
            stop()
        except RuntimeError:
            logger.debug("работник %s уже снесён — просить некого", entry.name)
    entry.asked = True
    try:
        entry.thread.quit()
    except RuntimeError:
        logger.debug("поток %s уже снесён", entry.name)


class _Registry(QObject):
    """Получатель сигналов реестра — живёт в GUI-потоке.

    Нужен именно `QObject`: `QThread.finished` испускается ИЗ фонового потока,
    и связь с обычной питоньей функцией исполнилась бы там же — то есть список
    правился бы двумя потоками сразу. С получателем в GUI-потоке Qt переводит
    доставку в очередь сам.
    """

    @Slot()
    def sweep(self) -> None:
        for entry in list(_LIVE):
            if _running(entry.thread):
                continue
            _LIVE.remove(entry)
            # Д3: событие о НОРМАЛЬНОЙ работе, не только об отказе
            logger.info("фоновый поток «%s» кончился штатно (uid=%s), "
                        "под присмотром осталось %d",
                        entry.name, entry.uid or "-", len(_LIVE))


_registry = _Registry()


def hand_over(owner, thread, worker=None, *, name: str, uid: str = "") -> None:
    """Отдать фоновый поток под присмотр: владелец умер → поток гасится.

    `owner` — тот, чьё разрушение делает поток бесхозным (вкладка, рабочая
    область, диалог). `worker` — объект, переехавший в поток; если он умеет
    `stop()`, его просят остановиться первым, и только потом гасят цикл
    событий самого потока.

    ⛔ Ожидания здесь НЕТ сознательно: слот `destroyed` исполняется в
    GUI-потоке, и `wait()` в нём заморозил бы окно ровно в тот момент, когда
    оператор уходит. Ждёт выход клиента — `install_exit_guard()`.
    """
    if thread is None:
        return
    entry = _Entry(thread=thread, worker=worker, name=name, uid=uid)
    _LIVE.append(entry)

    # Разрушение владельца. Получатель связи — питонья функция, поэтому связь
    # принадлежит ОТПРАВИТЕЛЮ и переживает вкладку ровно до её последнего
    # сигнала; сам `destroyed` для того и существует.
    owner.destroyed.connect(lambda *_: _ask_to_stop(entry))

    # Конец потока: запись снимается в GUI-потоке (см. `_Registry`).
    thread.finished.connect(_registry.sweep)

    logger.info("фоновый поток «%s» под присмотром (uid=%s), всего %d",
                name, uid or "-", len(_LIVE))


def watched() -> list[str]:
    """Имена потоков, которые сейчас под присмотром (для логов и тестов)."""
    return [e.name for e in _LIVE]


def install_exit_guard(app) -> None:
    """Выход клиента обязан дождаться фоновых потоков или уйти, не падая."""
    app.aboutToQuit.connect(drain_on_exit)


def drain_on_exit() -> None:
    """Погасить всё, дождаться с потолком, при неудаче уйти жёстко.

    ⛔ Последний шаг — не грубость, а единственное, что работает: удержание
    ссылки и родительство `QApplication` проверены и дают крах 4 из 4
    (`MEASUREMENTS §127`). Непрерываемый работник (один HTTP-вызов до 200 с)
    иначе уронил бы клиент на закрытии — то есть ровно там, где оператор уже
    ничего не может сделать.
    """
    pending = [e for e in _LIVE if _running(e.thread)]
    if not pending:
        _LIVE.clear()
        return

    logger.info("закрытие клиента: гашу %d фоновых потока(ов): %s",
                len(pending), ", ".join(e.name for e in pending))
    for entry in pending:
        _ask_to_stop(entry)

    for _ in range(max(1, EXIT_WAIT_MS // TICK_MS)):
        pending = [e for e in pending if _running(e.thread)]
        if not pending:
            break
        for entry in pending:
            # Гасит поток связь `worker.finished → thread.quit`, а её получатель
            # живёт в ГЛАВНОМ потоке: без адресной доставки `quit()` так и лежал
            # бы в очереди (замер 1-46, §119.2).
            QApplication.sendPostedEvents(entry.thread, QEvent.Type.MetaCall)
            try:
                entry.thread.wait(TICK_MS)
            except RuntimeError:
                logger.debug("поток %s снесён во время ожидания", entry.name)

    stuck = [e for e in pending if _running(e.thread)]
    _LIVE.clear()
    if not stuck:
        logger.info("закрытие клиента: все фоновые потоки кончились")
        return

    logger.warning(
        "закрытие клиента: потоки не кончились за %d мс (%s) — ухожу, "
        "не разрушая объектов: их разрушение уронило бы процесс",
        EXIT_WAIT_MS, ", ".join(f"{e.name} (uid={e.uid or '-'})" for e in stuck))
    logging.shutdown()
    os._exit(0)
