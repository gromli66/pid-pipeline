# -*- coding: utf-8 -*-
"""Пункт 1-50 — фоновый поток обязан кончиться раньше, чем процесс выйдет.

Зачем. `QThread`, созданный без родителя и живущий в питоньем поле владельца,
владельца ПЕРЕЖИВАЕТ: вкладку разрушает «← Назад», рабочую область — закрытие
клиента, а поток бежит дальше. Процесс, дошедший до выхода с бегущим потоком,
падает на его разрушении — `0xC0000409`. Замеры (`MEASUREMENTS §127`), одна
машина, коды возврата сняты у самого процесса:

* уход из рабочей области при живой сборке `.prtx` — **6 крахов из 6**,
  закрытие клиента — **6 из 6**, без жеста вообще — **6 из 6**;
* уход из вкладки при МОЛЧАЩЕМ сервере — **4 из 4** у каждой из четырёх
  загрузочных вкладок; контроль (сервер ответил) — **0 из 4**.

⛔ Два очевидных лекарства проверены и НЕ лечат: удержать поток питоньей
ссылкой — 4 краха из 4; дать ему родителя `QApplication` — 4 из 4. Пережить
выход процесса бегущий поток не может; лечит только то, что он кончается
раньше. Отсюда дверь `ui/services/thread_lifetime.py`.

⛔ Почему сторож СТАТИЧЕСКИЙ, а не перечень адресов: три возврата пункта 1-6
подряд ушли на утверждение «таких мест ровно N». Здесь список снимается с кода
при каждом прогоне, поэтому новый `QThread()` без двери краснеет сам —
ошибка в числе «девять» перестаёт быть дефектом.

⚠ Названная граница, которую этот набор НЕ закрывает: работник, стоящий в
ОДНОМ HTTP-вызове (`RecognizeWorker`, `_ServerProbe`), не прерывается — его
закрывает только страховка выхода, и это проверяется отдельным тестом
(`test_exit_guard_reports_a_thread_that_outlived_the_wait`).
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, QThread, Signal      # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

import ui.services.thread_lifetime as tl                         # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = REPO_ROOT / "ui"

#: Один оборот ожидания. Считаются ОБОРОТЫ, а не секунды (`PROTOCOL §3`).
TICK_MS = 20
JOIN_TICKS = 250


# ── статический сторож: каждый поток клиента проходит через дверь ────────

def _thread_subclasses() -> set[str]:
    """Имена локальных классов-наследников `QThread`.

    Без них перебор был бы у́же класса: `= QThread()` не видит
    `class _ContourExtractWorker(QThread)`, а это тот же носитель — его и
    пропустил греп, которым пункт заводили.
    """
    names = {"QThread"}
    for path in sorted(UI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if ast.unparse(base).split(".")[-1] == "QThread":
                    names.add(node.name)
    return names


def _own_nodes(scope):
    """Узлы охвата БЕЗ вложенных функций: у тех свой охват и своя дверь."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield child
        yield from _own_nodes(child)


def _sites():
    """Все места, где клиент поднимает фоновый поток, и что там передано двери.

    Возвращает список `(файл, строка, кому присвоен поток, отдан ли двери)`.
    """
    thread_names = _thread_subclasses()
    found = []
    for path in sorted(UI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        scopes = [tree] + [n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            created, handed = [], set()
            for node in _own_nodes(scope):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    callee = ast.unparse(node.value.func).split(".")[-1]
                    if callee in thread_names:
                        created.append((ast.unparse(node.targets[0]), node.lineno))
                if isinstance(node, ast.Call) \
                        and ast.unparse(node.func).split(".")[-1] == "hand_over":
                    handed.update(ast.unparse(a) for a in node.args)
            for target, lineno in created:
                found.append((path.relative_to(REPO_ROOT).as_posix(), lineno,
                              target, target in handed))
    return found


def test_every_background_thread_in_the_client_is_handed_over():
    """Новый `QThread()` без гашения — красный, а не «ещё один адрес в списке».

    Список снимается с кода прямо здесь: `= QThread()` ПЛЮС наследники
    `QThread`. Перечня, который можно проглядеть, у этого сторожа нет.
    """
    sites = _sites()
    assert sites, "сторож ослеп: в ui/ не нашлось ни одного фонового потока"

    orphans = [f"{f}:{n} — {t}" for f, n, t, ok in sites if not ok]
    assert not orphans, (
        "фоновый поток создан мимо `ui/services/thread_lifetime.hand_over`:\n  "
        + "\n  ".join(orphans)
        + "\nтакой поток переживает своего владельца, и процесс падает на его "
          "разрушении при выходе (0xC0000409, замер §127)"
    )


def test_the_guard_sees_all_nine_carriers():
    """Контроль честности сторожа: он видит ИМЕННО столько, сколько в коде.

    Число абсолютное и не вычисляется из проверяемого кода (`PROTOCOL §3`):
    девять носителей класса снято командами `grep -rnE "=\\s*QThread\\(\\s*\\)"`
    (8 в 7 файлах) плюс один наследник `QThread` в `contour_tab.py`.
    Поднялось число — значит появился новый поток, и он обязан пройти дверь;
    упало — значит сторож перестал видеть часть кода.
    """
    assert len(_sites()) == 9, [f"{f}:{n}" for f, n, _, _ in _sites()]


# ── дверь: разрушение владельца гасит поток ──────────────────────────────

class _Worker(QObject):
    """Работник, который умеет останавливаться, как боевые."""

    finished = Signal()

    def __init__(self):
        super().__init__()
        self.stopped = False

    def stop(self):
        self.stopped = True

    def run(self):
        while not self.stopped:
            QThread.msleep(5)
        self.finished.emit()


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp):
    """Владелец + поток + работник; за собой набор убирает сам (форма 1-36)."""
    made = []

    def _make():
        owner = QObject()
        thread = QThread()
        worker = _Worker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        made.append((owner, thread, worker))
        return owner, thread, worker

    yield _make

    for owner, thread, worker in made:
        worker.stop()
        thread.quit()
        for _ in range(JOIN_TICKS):
            QApplication.sendPostedEvents(thread, QEvent.Type.MetaCall)
            if thread.wait(TICK_MS):
                break
    tl._LIVE.clear()


def _join(thread) -> bool:
    for _ in range(JOIN_TICKS):
        QApplication.sendPostedEvents(thread, QEvent.Type.MetaCall)
        if thread.wait(TICK_MS):
            return True
    return not thread.isRunning()


def test_destroying_the_owner_asks_the_worker_to_stop(bench):
    """Владельца разрушили → работника попросили уйти, поток кончился.

    Утверждается РАЗНИЦА, а не совпадение с состоянием «до»: до разрушения
    работник заведомо бежит и не остановлен — это проверяется здесь же.
    """
    owner, thread, worker = bench()
    tl.hand_over(owner, thread, worker, name="проба", uid="0fc9d04c")
    thread.start()

    assert "проба" in tl.watched(), "поток не встал под присмотр"
    assert not worker.stopped, "обстановка не та: работника уже остановили"
    assert thread.isRunning(), "обстановка не та: поток не бежит"

    owner.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert worker.stopped, "разрушение владельца не дошло до работника"
    assert _join(thread), "поток пережил владельца — процесс упадёт на выходе"


def test_a_thread_that_ends_by_itself_leaves_the_registry(bench):
    """Д3: штатный конец потока снимает запись — присмотр не копится.

    Без этого реестр рос бы весь сеанс оператора, и выход клиента ждал бы
    потоков, которых давно нет.
    """
    owner, thread, worker = bench()
    tl.hand_over(owner, thread, worker, name="штатная", uid="0fc9d04c")
    thread.start()
    assert "штатная" in tl.watched()

    worker.stop()                       # работа кончилась сама, без ухода
    assert _join(thread), "поток не кончился"
    QApplication.sendPostedEvents(None, QEvent.Type.MetaCall)
    qapp = QApplication.instance()
    for _ in range(JOIN_TICKS):
        if "штатная" not in tl.watched():
            break
        qapp.processEvents()

    assert "штатная" not in tl.watched(), (
        f"кончившийся поток остался под присмотром: {tl.watched()}"
    )


# ── прерываемость: просьба уйти обязана доходить до работы, а не до флага ─

def test_prtx_worker_leaves_the_poll_when_asked():
    """Сборка `.prtx` опрашивает сервер до 15 минут — просьба уйти рвёт опрос.

    Утверждается НАШЕ решение (`PROTOCOL §3`), а не свойство библиотеки: при
    остановке `_wait_for_build` поднимает `Stopped`, то есть уходит по нашей
    ветке, а не по таймауту сборки и не по отказу сервера. Без этого закрытие
    клиента упиралось бы в потолок ожидания и уходило жёсткой веткой.
    """
    from ui.services.prtx_converter import PrtxWorker
    from ui.services.thread_lifetime import Stopped

    class _Server:
        """Сервер, который на первый же вопрос отвечает ОТКАЗОМ.

        ⛔ Так выбрано по четвёртому исходу зонда (`PROTOCOL §5`): с вечным
        «ещё считаю» инъекция «снять прерываемость» не краснела, а ВЕШАЛА
        набор на пятнадцатиминутный дедлайн сборки. С отказом та же инъекция
        даёт `PrtxError` вместо `Stopped` — красное, и через один опрос.
        """

        def __init__(self):
            self.asked = 0

        def prtx_status(self, uid):
            self.asked += 1
            return {"state": "error", "error": "зонд: сервер отказал"}

    worker = PrtxWorker(_Server(), "0fc9d04c", None)
    worker.stop()                       # оператор закрыл клиент

    with pytest.raises(Stopped):
        worker._wait_for_build()
    assert worker.api_client.asked == 0, (
        "после просьбы уйти работник всё равно спросил сервер "
        f"{worker.api_client.asked} раз(а)"
    )


def test_downloader_starts_no_new_job_after_being_asked_to_stop(tmp_path):
    """Загрузка вкладки: просьба уйти снимает ОСТАВШИЕСЯ задания.

    Наблюдаемое — журнал сервера: ни одного обращения. Текущий HTTP-вызов
    отсюда не прерывается, и это названная граница, а не недосмотр.
    """
    from ui.services.artifact_downloader import ArtifactDownloader, artifact, one

    class _Server:
        def __init__(self):
            self.calls = []

        def download_artifact(self, uid, art_type, dest):
            self.calls.append(art_type)
            return dest

    server = _Server()
    jobs = (one(artifact("graph_validated", "graph.json"), required=True),)
    downloader = ArtifactDownloader(server, "0fc9d04c", tmp_path, jobs)
    finished, errors = [], []
    downloader.finished.connect(finished.append)
    downloader.error.connect(errors.append)

    downloader.stop()
    downloader.run()

    assert server.calls == [], f"загрузка пошла после просьбы уйти: {server.calls}"
    assert not finished and not errors, (
        "прекращённая загрузка разбудила слоты разрушенной вкладки: "
        f"finished={finished}, error={errors}"
    )


# ── сценарий уровня дефекта: жест оператора в ОТДЕЛЬНОМ процессе ─────────

SCENARIO = '''# -*- coding: utf-8 -*-
"""Жест оператора в БОЕВОМ старте клиента; вердикт — код возврата процесса.

Клиент поднимается своей же точкой входа `ui.main.main()`, а не сборкой окна
руками: страховку выхода обязан ставить боевой код, и тест, ставящий её сам,
остался бы зелёным после её сноса.
"""
import os, sys, threading, traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, {root!r})

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

import ui.main as m
import ui.services.prtx_converter as pc
import ui.services.prtx_license as pl
import ui.widgets.diagram_workspace as dw
import ui.windows.main_window as mw
from ui.services.api_client import DiagramStatus

SCENARIO = {scenario!r}
STUBBORN = {stubborn!r}
RUNNING, RELEASE = threading.Event(), threading.Event()
ENTERED, GATE = threading.Event(), threading.Event()


class FakeDiagram:
    def __init__(self, status):
        self.status, self.error_stage = status, None
        self.project_code = "thermohydraulics"


class HoldingAPI:
    """Сервер, который ДЕРЖИТ первый артефакт: «ушёл, пока качалось»."""

    status = DiagramStatus.UPLOADED

    def __init__(self, *a, **kw):
        self.base_url = "http://fake"

    def health_check(self):
        return True

    def list_projects(self):
        return [{{"code": "thermohydraulics", "name": "ТГ"}}]

    def list_diagrams(self, *a, **kw):
        return []

    def get_diagram(self, uid):
        return FakeDiagram(HoldingAPI.status)

    def get_stages(self, uid):
        return []

    def get_ocr_status(self, uid):
        return {{"has_ocr_result": False}}

    def start_graph_validation(self, uid):
        return {{"status": "validating_graph"}}

    def download_artifact(self, uid, art_type, dest):
        ENTERED.set()
        GATE.wait(120)
        raise RuntimeError("сервер так и не ответил")

    def close(self):
        pass


class FakeProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)
    error_occurred = Signal(str, str)

    def __init__(self, *a, **kw):
        super().__init__()

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


class FakeMsgBox:
    StandardButton = QMessageBox.StandardButton
    question = classmethod(lambda cls, *a, **k: cls.StandardButton.Yes)
    warning = classmethod(lambda cls, *a, **k: cls.StandardButton.Ok)
    critical = classmethod(lambda cls, *a, **k: cls.StandardButton.Ok)
    information = classmethod(lambda cls, *a, **k: cls.StandardButton.Ok)


class HoldingWorker(QObject):
    """Сборка .prtx, которая «идёт минутами» — цитата боевого комментария."""

    finished, error, progress = Signal(str), Signal(str), Signal(str)

    def __init__(self, api_client, uid, export_path):
        super().__init__()

    def stop(self):
        if not STUBBORN:
            RELEASE.set()

    def run(self):
        RUNNING.set()
        RELEASE.wait(120)
        self.finished.emit("готово")


class OkReport:
    ok = True
    first_problem = None


class BootApp(QApplication):
    """Тот же старт, что у оператора, только сеанс короткий."""

    def exec(self):
        QTimer.singleShot(0, self._play)
        QTimer.singleShot(60000, self.quit)     # страховка от висяка
        return super().exec()

    def _window(self):
        for w in self.topLevelWidgets():
            if type(w).__name__ == "MainWindow":
                return w
        raise AssertionError("окна клиента нет среди верхнеуровневых")

    def _play(self):
        win = self._window()
        ws = win.workspace
        if SCENARIO == "prtx":
            ws.load_diagram({uid!r}, "схема оператора")
            ws._start_prtx_conversion()
            assert RUNNING.wait(30), "обстановка не та: сборка не пошла"
            assert ws._prtx_thread.isRunning(), "сборка уже кончилась"
            ws._on_back_to_list()               # ушёл из рабочей области
        else:
            HoldingAPI.status = DiagramStatus.BUILT
            ws.load_diagram({uid!r}, "схема оператора")
            ws._original_handlers["val_graph"]()
            tab = ws._active_tab
            assert ENTERED.wait(30), "обстановка не та: загрузка не пошла"
            assert tab._download_thread.isRunning(), "загрузка уже кончилась"
            ws._btn_back_injected.click()       # «← Назад» разрушает вкладку
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        win.close()                             # и закрыл клиент


mw.APIClient = HoldingAPI
mw.StatusProvider = FakeProvider
mw.QMessageBox = FakeMsgBox
dw.QMessageBox = FakeMsgBox
pc.PrtxWorker = HoldingWorker
pl.diagnose_local = lambda: OkReport()
m.QApplication = BootApp

try:
    m.main()
except SystemExit as exc:
    if exc.code:
        open({crash!r}, "w", encoding="utf-8").write("SystemExit=%r" % (exc.code,))
except BaseException:
    open({crash!r}, "w", encoding="utf-8").write(traceback.format_exc())
    raise
'''


def _play(tmp_path, scenario, uid, stubborn=False):
    """Проиграть жест в отдельном процессе и вернуть (код возврата, лог)."""
    crash = tmp_path / "crash.txt"
    logs = tmp_path / "logs"
    script = tmp_path / f"gesture_{scenario}.py"
    script.write_text(SCENARIO.format(
        root=str(REPO_ROOT), scenario=scenario, uid=uid, stubborn=stubborn,
        crash=str(crash)), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PID_LOG_DIR=str(logs),
                 PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8"),
    )
    log = logs / "client.log"
    note = crash.read_text(encoding="utf-8") if crash.exists() else ""
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert not note, f"сценарий сорвался до жеста:\n{note}"
    return proc.returncode, text


def test_leaving_the_workspace_during_a_prtx_build_does_not_kill_the_client(tmp_path):
    """ГЕЙТ ПУНКТА: ушёл из рабочей области при живой сборке — клиент цел.

    До правки этот же жест давал `0xC0000409` — 6 крахов из 6 (§127).
    """
    rc, log = _play(tmp_path, "prtx", "0fc9d04c-1111-2222-3333-444455556666")

    assert rc == 0, f"клиент не пережил уход при живой сборке .prtx: rc={rc:#x}"
    assert "сборка .prtx" in log, f"поток не встал под присмотр:\n{log}"


def test_leaving_a_tab_while_the_server_is_silent_does_not_kill_the_client(tmp_path):
    """ГЕЙТ ПУНКТА: «← Назад» при МОЛЧАЩЕМ сервере — клиент цел.

    Обстановка выбрана по замеру: набор 1.x17 отпускает сервер перед своей
    проверкой, поэтому его инвариант держится только на отвечающем сервере.
    На молчащем этот жест давал 4 краха из 4 у каждой из четырёх вкладок.
    """
    rc, log = _play(tmp_path, "tab", "d74eb9f1")

    assert rc == 0, f"клиент не пережил уход из вкладки: rc={rc:#x}"
    assert "загрузка графовой вкладки" in log, f"поток мимо присмотра:\n{log}"


def test_exit_guard_reports_a_thread_that_outlived_the_wait(tmp_path):
    """Непрерываемый работник: клиент всё равно цел, и это ВИДНО в логе.

    Граница названа вслух: работник в одном HTTP-вызове не прерывается
    (`RecognizeWorker` ждёт до 200 с). Тогда выход уходит жёсткой веткой —
    и обязан сказать об этом строкой, иначе молчаливый `os._exit` не отличить
    от штатного закрытия.
    """
    rc, log = _play(tmp_path, "prtx", "0fc9d04c-1111-2222-3333-444455556666",
                    stubborn=True)

    assert rc == 0, f"клиент упал на непрерываемом потоке: rc={rc:#x}"
    assert "не кончились" in log, (
        f"жёсткий выход прошёл молча — в логе нет строки о нём:\n{log}"
    )
    assert "uid=0fc9d04c" in log, f"строка без корреляции по uid:\n{log}"
