"""Приёмник логов клиента (пункт 1.10 дороги).

Дефект уровня пункта: логи клиента пишутся и выбрасываются — в собранном
``.exe`` ``console=False``, а необработанное исключение в слоте оставляло
замерший UI без единого следа. Значит главный тест здесь — сценарный:
поднять клиент его же точкой входа ``ui.main.main()``, уронить слот в живом
цикле событий Qt и найти запись В ФАЙЛЕ (``test_slot_exception_lands_in_log_file``).
Юниты рядом запирают то, что сценарий не различает: кодировку файла (эмодзи и
кириллица роняли StreamHandler на cp1251), ротацию, корреляцию по ``uid``,
отказ каталога.

Qt поднимается ОТДЕЛЬНЫМ ПРОЦЕССОМ (offscreen): цикл событий и подмена
``sys.excepthook`` в общем процессе pytest — источник висяков и утечек.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Литералы сценария. Намеренно НЕ импортируются из проверяемого модуля:
# тест, вычисляющий свой вход из проверяемой константы, зелен при любом её
# значении (запрет PROTOCOL §3 дороги).
LOG_FILE_NAME = "client.log"
SLOT_MESSAGE = "ИСКУССТВЕННОЕ падение слота 💥"
SCENARIO_UID = "0fc9d04c"


@pytest.fixture
def isolated_root():
    """Вернуть корневой логгер в исходное состояние после теста.

    ``setup_client_logging`` штатно чистит хендлеры корня — вместе с
    перехватчиком pytest. Без восстановления следующие тесты сессии теряют
    caplog, а открытый файл лога не даёт удалить tmp_path на Windows.
    """
    from app.core import obs

    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    obs.reset()  # контекст живёт в contextvars — иначе uid течёт между тестами
    yield root
    for handler in root.handlers[:]:
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = saved
    root.setLevel(level)


@pytest.fixture
def setup(monkeypatch, tmp_path, isolated_root):
    """Настроить логи клиента в tmp_path и вернуть путь файла."""
    from ui.services.client_logging import setup_client_logging

    monkeypatch.setenv("PID_LOG_DIR", str(tmp_path / "logs"))

    def _setup(**kw):
        return setup_client_logging(**kw)

    return _setup


def _file_handler(root):
    from logging.handlers import RotatingFileHandler

    handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(handlers) == 1, f"ожидался ровно один файловый хендлер: {root.handlers!r}"
    return handlers[0]


@contextmanager
def only_file_handler(root):
    """Оставить на корне один файловый хендлер.

    Фильтры висят на хендлерах и правят саму запись: консольный, отработав
    первым, проставляет корреляционные поля и за файловый тоже. В собранном
    ``.exe`` консольного вывода нет вовсе (``console=False``), поэтому файл
    обязан быть самодостаточным — иначе снятие фильтра с него никто не заметит.
    """
    handler = _file_handler(root)
    others = [h for h in root.handlers if h is not handler]
    for h in others:
        root.removeHandler(h)
    try:
        yield handler
    finally:
        for h in others:
            root.addHandler(h)


# --- приёмник ---------------------------------------------------------------


def test_setup_creates_file_and_returns_path(setup, tmp_path, isolated_root):
    path = setup()
    assert path == tmp_path / "logs" / LOG_FILE_NAME
    assert path.exists(), "файл лога должен создаваться сразу, а не по первой записи"


def test_startup_line_names_the_log_file(setup, isolated_root):
    """Д3: подсистема сообщает и о нормальной работе, не только об отказе."""
    path = setup()
    logging.getLogger().handlers[-1].flush()
    text = path.read_text(encoding="utf-8")
    assert str(path) in text, f"первая строка обязана называть путь: {text!r}"


def test_log_dir_honours_env(monkeypatch, tmp_path):
    from ui.services.client_logging import log_dir

    monkeypatch.setenv("PID_LOG_DIR", str(tmp_path / "custom"))
    assert log_dir() == tmp_path / "custom"


def test_log_dir_is_absolute_and_outside_cwd(monkeypatch):
    """``client_main.py:42`` делает chdir в _MEIPASS — относительный путь
    положил бы лог во временный каталог распаковки (родня дефекта 1.19)."""
    from ui.services.client_logging import log_dir

    monkeypatch.delenv("PID_LOG_DIR", raising=False)
    assert log_dir().is_absolute()


def test_unwritable_dir_does_not_break_startup(monkeypatch, tmp_path, isolated_root):
    """Нет прав на каталог — клиент обязан подняться с одной консолью."""
    from ui.services import client_logging

    monkeypatch.setenv("PID_LOG_DIR", str(tmp_path / "logs"))

    def _boom(*a, **kw):
        raise PermissionError("нет прав")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert client_logging.setup_client_logging() is None


# --- кодировка и ротация ----------------------------------------------------


def test_file_is_utf8_with_replace(setup, isolated_root):
    setup()
    handler = _file_handler(isolated_root)
    assert handler.encoding.lower().replace("-", "") == "utf8"
    assert handler.errors == "replace"


def test_cyrillic_and_emoji_survive_in_file(setup, isolated_root):
    """Тот же дефект, что чинит ``ui/main.py:12-18`` для консоли: без явной
    utf-8 эмодзи роняет запись ``UnicodeEncodeError``'ом, и строка теряется."""
    path = setup()
    logging.getLogger("ui.test").error("Сохранено 💾 контуры ✅ обновление 🔄")
    _file_handler(isolated_root).flush()
    text = path.read_text(encoding="utf-8")
    assert "Сохранено 💾 контуры ✅ обновление 🔄" in text


def test_rotation_is_bounded_from_both_sides(setup, isolated_root):
    """Ротация есть и настроена вменяемо: без верхней границы «ротация» может
    оказаться формальной (файл растёт гигабайтами), без нижней — лог рвётся."""
    setup()
    handler = _file_handler(isolated_root)
    assert 1_000_000 <= handler.maxBytes <= 100_000_000
    assert 1 <= handler.backupCount <= 20


def test_rotation_actually_rolls(setup, tmp_path, isolated_root):
    path = setup()
    handler = _file_handler(isolated_root)
    handler.maxBytes = 2048
    for i in range(200):
        logging.getLogger("ui.test").error("строка ротации %d %s", i, "x" * 100)
    handler.flush()
    assert (path.parent / f"{LOG_FILE_NAME}.1").exists(), (
        f"ротация не сработала: {sorted(p.name for p in path.parent.iterdir())}"
    )


# --- корреляция по uid ------------------------------------------------------


def test_uid_of_open_diagram_lands_in_every_line(setup, isolated_root):
    """Д2: строка клиента несёт uid — сшивается с серверной по нему же."""
    from ui.services.client_logging import bind_uid

    path = setup()
    bind_uid("1a2b3c4d")
    with only_file_handler(isolated_root) as handler:
        logging.getLogger("ui.test").error("после открытия диаграммы")
        handler.flush()
    line = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "после открытия" in ln]
    assert line and "uid=1a2b3c4d" in line[0], f"нет uid в строке: {line!r}"


def test_open_diagram_binds_its_uid(setup, isolated_root):
    """Точка привязки — открытие диаграммы, а не «где-нибудь в клиенте».

    Метод зовётся несвязанным с заглушкой вместо self: живая вкладка требует
    сервера и QApplication, а проверяется здесь один эффект — контекст лога.
    """
    from unittest.mock import MagicMock

    from ui.widgets.diagram_workspace import DiagramWorkspace

    path = setup()
    DiagramWorkspace.load_diagram(MagicMock(), "7f3c9a11", "схема оператора")
    with only_file_handler(isolated_root) as handler:
        logging.getLogger("ui.test").error("работа после открытия")
        handler.flush()
    line = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "работа после" in ln]
    assert line and "uid=7f3c9a11" in line[0], f"открытие диаграммы не проставило uid: {line!r}"


def test_no_uid_before_any_diagram_is_open(setup, isolated_root):
    path = setup()
    with only_file_handler(isolated_root) as handler:
        logging.getLogger("ui.test").error("до открытия диаграммы")
        handler.flush()
    line = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "до открытия" in ln]
    assert line and "uid=-" in line[0], f"чужой/пустой uid: {line!r}"


# --- перехват падений -------------------------------------------------------


def test_excepthook_logs_traceback_and_chains(setup, isolated_root, monkeypatch):
    from ui.services.client_logging import install_excepthook

    path = setup()
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a[0]))
    install_excepthook()
    try:
        raise ValueError("прямой вызов хука")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    _file_handler(isolated_root).flush()
    text = path.read_text(encoding="utf-8")
    assert "прямой вызов хука" in text and "Traceback" in text
    assert seen == [ValueError], "прежний хук обязан получить управление"


def test_excepthook_installs_once(setup, isolated_root, monkeypatch):
    """Двойная установка дала бы две записи об одном падении."""
    from ui.services.client_logging import install_excepthook

    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    install_excepthook()
    first = sys.excepthook
    install_excepthook()
    assert sys.excepthook is first


# --- сценарий уровня дефекта (гейт пункта) ----------------------------------


SCENARIO = '''
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

import ui.main as m
from ui.services.client_logging import bind_uid


class BootApp(QApplication):
    """Тот же старт, что у оператора, только цикл событий короткий."""

    def exec(self):
        QTimer.singleShot(0, self._slot)
        QTimer.singleShot(2000, self.quit)
        return super().exec()

    def _slot(self):
        raise RuntimeError({message!r})


m.QApplication = BootApp
m.MainWindow = QWidget
bind_uid({uid!r})
try:
    m.main()
except SystemExit as exc:
    sys.stderr.write("SystemExit=%r\\n" % (exc.code,))
'''


# Условие собранного клиента: при console=False у процесса нет ни stdout, ни
# stderr (оба None). Ровно тот случай, ради которого заведён файловый приёмник.
NO_CONSOLE = '''
import logging, sys

from ui.services.client_logging import setup_client_logging, install_excepthook

sys.stdout = None
sys.stderr = None
setup_client_logging()
install_excepthook()
logging.getLogger("ui.probe").error({message!r})
try:
    raise RuntimeError("падение при мёртвой консоли")
except RuntimeError:
    sys.excepthook(*sys.exc_info())
logging.shutdown()
'''


def _run_client(tmp_path, code, name):
    """Прогнать код клиента отдельным процессом; вернуть (proc, текст лога)."""
    script = tmp_path / name
    script.write_text(code, encoding="utf-8")
    logs = tmp_path / "logs"
    env = dict(os.environ, PID_LOG_DIR=str(logs), QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    path = logs / LOG_FILE_NAME
    if not path.exists():
        pytest.fail(
            f"файла лога нет: {path}\n--- rc={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc, path.read_text(encoding="utf-8")


@pytest.fixture
def scenario_log(tmp_path):
    """Клиент поднят своей же точкой входа, слот падает в живом цикле Qt."""
    return _run_client(
        tmp_path,
        SCENARIO.format(message=SLOT_MESSAGE, uid=SCENARIO_UID),
        "boot_and_crash.py",
    )


def test_slot_exception_lands_in_log_file(scenario_log):
    """ГЕЙТ ПУНКТА: искусственное исключение в слоте → строка в файле лога."""
    proc, text = scenario_log
    assert SLOT_MESSAGE in text, f"нет сообщения слота в логе:\n{text}"
    assert "RuntimeError" in text and "Traceback" in text, f"нет трассировки:\n{text}"
    assert "CRITICAL" in text, f"падение записано не как критическое:\n{text}"


def test_scenario_line_carries_uid_and_client_survives(scenario_log):
    proc, text = scenario_log
    crash = [ln for ln in text.splitlines() if "CRITICAL" in ln]
    assert crash and f"uid={SCENARIO_UID}" in crash[0], f"нет uid в строке падения: {crash!r}"
    assert proc.returncode == 0, (
        f"клиент не пережил исключение в слоте: rc={proc.returncode}\n{proc.stderr}"
    )


def test_log_survives_dead_console(tmp_path):
    """Собранный .exe идёт с console=False: stdout и stderr — None.

    Замер этого пункта: консольный хендлер там молча пустеет, а файл пишется
    полностью, и процесс не падает. Пока приёмника не было, вся диагностика
    клиента заканчивалась ровно здесь.
    """
    proc, text = _run_client(
        tmp_path, NO_CONSOLE.format(message=SLOT_MESSAGE), "no_console.py"
    )
    assert SLOT_MESSAGE in text, f"строка не дошла до файла:\n{text}"
    assert "падение при мёртвой консоли" in text and "Traceback" in text
    assert proc.returncode == 0, f"процесс упал: {proc.returncode}\n{proc.stderr}"
