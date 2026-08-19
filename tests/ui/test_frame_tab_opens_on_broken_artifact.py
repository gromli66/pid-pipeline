# -*- coding: utf-8 -*-
"""Пункт 1.x6 дороги — «Очистка рамки» открывается даже на битом артефакте.

Замер до правки (`ui/tabs/frame_tab.py:48` → `:126`): конструктор `FrameTab`
СИНХРОННО качает `original_image`, а на любом отказе открывает модальный
`QMessageBox` прямо из `__init__` (`:135`). Два следствия, оба класса волны 1:

* на бою модалка всплывает поверх ещё не открытой вкладки — воркспейс стоит
  на строке `FrameTab(...)`, вкладку в layout ещё не вставил, сигналы ещё не
  подключил (`ui/widgets/diagram_workspace.py:1752-1759`), и `status_message`
  из конструктора не слышит НИКТО;
* под offscreen диалог некому закрыть — процесс висит насмерть. Замерено
  зондом «класс на процесс» (§61.2): `TAB-OPENED` не печатается, `exit 124`.

Инвариант пункта: **вкладка открывается всегда, а отказ загрузки виден
СТРОКОЙ в уже открытой вкладке — не модалкой из конструктора.** Плюс отказ
обязан дойти до ФАЙЛА лога клиента (`ui/services/client_logging.py`, пункт
1.10): `QMessageBox` в собранном `.exe` — единственный след, а он исчезает
вместе с непрочитанным диалогом.

Что проверяется — только наблюдаемое оператором и данные: факт (не)вызова
диалога, текст видимых строк вкладки, что ушло в `status_message`, порядок
обращений к серверу, содержимое файла лога. Ни одного утверждения про
внутреннюю кухню вкладки.

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом:
модальный диалог в пути инъекции подвешивает набор вместо падения
(`PROTOCOL §5`, замеры 1.5, 1.3, 1-6). Подмена ставится ДО конструктора —
здесь диалог открывается именно из него. Сценарий «вкладка вообще
открывается» проверяется отдельным ПРОЦЕССОМ, где подмены нет.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (                                   # noqa: E402
    QApplication, QLabel, QMessageBox,
)
from PySide6.QtGui import QColor, QImage                          # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

# Литералы сценария. Намеренно НЕ импортируются из проверяемого модуля: тест,
# вычисляющий свой вход из проверяемой константы, зелен при любом её значении
# (запрет `PROTOCOL §3`).
UID = "0fc9d04c"
ART_TYPE = "original_image"
BROKEN_BYTES = b"NOT-A-PNG"
API_REASON = "сервер отказал: 502 битый артефакт"
IMG_W, IMG_H = 300, 200

SCENARIO_MARKER = "TAB-OPENED"
SCENARIO_TIMEOUT = 60          # висяк отличается от «долго» на два порядка

# Сценарий-процесс: живой QApplication, подмен нет, артефакт битый.
SCENARIO = """
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])

from PySide6.QtWidgets import QApplication
from ui.tabs.frame_tab import FrameTab


class BrokenAPI:
    def download_artifact(self, uid, art_type, dest):
        with open(dest, "wb") as fh:
            fh.write({broken!r})


app = QApplication([])
tab = FrameTab({uid!r}, "проба 1.x6", BrokenAPI())
tab.show()
for _ in range(50):
    app.processEvents()
print("{marker}")
""".format(broken=BROKEN_BYTES, uid=UID, marker=SCENARIO_MARKER)


class FakeAPI:
    """Сервер: помнит обращения, умеет отдать битый файл, отказ или картинку."""

    def __init__(self, mode: str):
        self.mode = mode
        self.calls: list[str] = []

    def download_artifact(self, uid, art_type, dest):
        from ui.services.api_client import APIError

        self.calls.append(art_type)
        if self.mode == "api_error":
            raise APIError(API_REASON, status_code=502)
        if self.mode == "broken":
            Path(dest).write_bytes(BROKEN_BYTES)
            return
        img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        assert img.save(str(dest)), "фикстура не смогла записать PNG"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    """Подмена модальных диалогов: набор не имеет права виснуть (`PROTOCOL §5`)."""
    seen = []

    def _warn(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_warn))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(_warn))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(_warn))
    return seen


@pytest.fixture
def client_log(tmp_path, monkeypatch):
    """Живой приёмник логов клиента (1.10) в tmp-каталог; отдаёт путь файла."""
    from ui.services import client_logging

    monkeypatch.setenv("PID_LOG_DIR", str(tmp_path / "logs"))
    root = logging.getLogger()
    before = list(root.handlers)
    path = client_logging.setup_client_logging()
    assert path is not None
    client_logging.bind_uid(UID)
    yield path
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    root.handlers = before


def _pump(qapp, times: int = 50):
    """Прокрутить очередь событий — столько же, сколько крутит открытая вкладка."""
    for _ in range(times):
        qapp.processEvents()


def _make_tab(mode: str):
    """Вкладка «Очистка рамки» с подставным сервером; показана, как у оператора.

    Сигналы подключаются ПОСЛЕ конструктора — ровно так их подключает
    воркспейс (`diagram_workspace.py:1758`).
    """
    from ui.tabs.frame_tab import FrameTab

    api = FakeAPI(mode)
    tab = FrameTab(UID, "проба 1.x6", api)
    said: list[str] = []
    tab.status_message.connect(said.append)
    tab.show()
    return tab, api, said


def _visible_lines(tab) -> list[str]:
    """Непустые строки, которые оператор видит в открытой вкладке."""
    return [lb.text() for lb in tab.findChildren(QLabel)
            if lb.isVisible() and lb.text().strip()]


# ── дефект пункта: загрузка идёт ИЗ КОНСТРУКТОРА ─────────────────────────

def test_constructor_does_not_touch_server(qapp, dialogs, client_log):
    """Конструктор не качает: вкладка должна успеть открыться и подключиться."""
    tab, api, _said = _make_tab("ok")
    try:
        assert api.calls == [], f"конструктор уже сходил на сервер: {api.calls}"

        _pump(qapp)

        assert api.calls == [ART_TYPE], f"загрузка не состоялась: {api.calls}"
    finally:
        tab.deleteLater()


# ── дефект пункта: отказ показывается модалкой ───────────────────────────

def test_broken_artifact_shows_line_and_no_dialog(qapp, dialogs, client_log):
    """Битый файл: вкладка открыта, ошибка — видимой строкой, диалогов ноль."""
    tab, api, said = _make_tab("broken")
    try:
        _pump(qapp)

        assert api.calls == [ART_TYPE]
        assert dialogs == [], f"модальный диалог всё ещё открывается: {dialogs}"
        assert tab.isVisible(), "вкладка не открылась"

        lines = _visible_lines(tab)
        assert any("Не удалось загрузить изображение" in t for t in lines), lines
        assert any("Не удалось загрузить изображение" in t for t in said), said
    finally:
        tab.deleteLater()


def test_api_failure_shows_reason_in_line(qapp, dialogs, client_log):
    """Отказ сервера: в строке — причина, а не только «что-то пошло не так»."""
    tab, api, said = _make_tab("api_error")
    try:
        _pump(qapp)

        assert dialogs == [], f"модальный диалог всё ещё открывается: {dialogs}"
        lines = _visible_lines(tab)
        assert any(API_REASON in t for t in lines), lines
    finally:
        tab.deleteLater()


def test_broken_artifact_leaves_trace_in_client_log(qapp, dialogs, client_log):
    """След отказа — в ФАЙЛЕ лога клиента, с трассировкой и `uid` (Д2)."""
    tab, _api, _said = _make_tab("api_error")
    try:
        _pump(qapp)

        for handler in logging.getLogger().handlers:
            handler.flush()
        content = client_log.read_text(encoding="utf-8", errors="replace")
        assert API_REASON in content
        assert "Traceback" in content
        assert f"uid={UID}" in content
    finally:
        tab.deleteLater()


# ── контроль: исправная загрузка остаётся исправной ──────────────────────

def test_successful_load_shows_no_error_line(qapp, dialogs, client_log):
    """Картинка на месте: изображение в редакторе, строки отказа нет, диалогов нет."""
    tab, api, said = _make_tab("ok")
    try:
        _pump(qapp)

        assert api.calls == [ART_TYPE]
        assert dialogs == []
        assert tab.editor.img_w == IMG_W
        assert tab.editor.img_h == IMG_H
        # Подсказки инструмента идут из редактора через `status_callback`,
        # последнее слово — за вкладкой. До правки терялись все три: сигнал
        # ещё не подключён (§61.3).
        assert said[-1] == "Обведите внутреннюю часть чертежа полигоном", said
        assert not any("Не удалось" in t for t in said), said
        assert _visible_lines(tab) == [], _visible_lines(tab)
    finally:
        tab.deleteLater()


def test_successful_load_is_logged(qapp, dialogs, client_log):
    """Д3: нормальная работа тоже оставляет событие, не только отказ."""
    tab, _api, _said = _make_tab("ok")
    try:
        _pump(qapp)

        for handler in logging.getLogger().handlers:
            handler.flush()
        content = client_log.read_text(encoding="utf-8", errors="replace")
        assert ART_TYPE in content
        assert f"uid={UID}" in content
    finally:
        tab.deleteLater()


# ── сценарий гейта: вкладка открывается в ЖИВОМ процессе ─────────────────

def test_broken_artifact_opens_tab_in_live_process(tmp_path):
    """Гейт пункта: битый артефакт → процесс доходит до конца, а не виснет.

    Подмен здесь нет намеренно: модалка из конструктора вешает процесс
    насмерть, и увидеть это можно только снаружи. Утверждение — о коде
    возврата и напечатанном маркере; таймаут лишь ограничивает висяк.
    """
    script = tmp_path / "open_frame_tab.py"
    script.write_text(SCENARIO, encoding="utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(script), str(REPO_ROOT)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=SCENARIO_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"вкладка не открылась за {SCENARIO_TIMEOUT} с — процесс висит "
            f"на модальном диалоге из конструктора (PROTOCOL §5)")

    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert SCENARIO_MARKER in proc.stdout, f"{proc.stdout}\n{proc.stderr}"
