# -*- coding: utf-8 -*-
"""Пункт ДН3 дороги — отчёт оператора: холст и заметка к error_report_dialog.

Дефект уровня пункта (замерен ревизией связки 2026-08-19, перемерен здесь,
`MEASUREMENTS §96`): отказ инструмента редактора до файла лога клиента НЕ
ДОХОДИЛ. `advanced_graph_tab._auto_fix` печатал трассировку в `sys.stderr`,
которого в собранном `.exe` нет (`console=False`), логгер в `except` не звался
вовсе — прирост `client.log` на отказе 0 байт. Оператор оставался с одной
строкой `QMessageBox`, и единственный след ошибки терялся.

Что здесь запирается:

1. отказ инструмента оставляет след В ФАЙЛЕ лога — с трассировкой и `uid`;
2. оператору открывается ОТЧЁТ, а не одна строка, и в отчёт вложен ХОЛСТ —
   то, на чём упало (гейт пункта);
3. заметка оператора доезжает до отчёта — он единственный, кто знает, что
   делал перед отказом;
4. фокус возвращается редактору и на пути отказа: холст уже изменён, шаг
   отмены открыт (пункт 1.1), и Ctrl+Z оператору нужен тем более.

⚠ Обстановка теста. Файл лога проверяется при ОДНОМ файловом хендлере на корне
(`only_file_handler`): фильтры висят на хендлерах и правят саму запись, поэтому
консольный, отработав первым, проставляет корреляционные поля и за файловый —
и снятие `ContextFilter` с файлового прошло бы незамеченным (замер 1.10,
перемер 1-15: обнажает подмену именно один хендлер на корне, а не мёртвая
консоль — немой хендлер `.exe` штампует поля так же). Эмуляция `.exe`
(`sys.stdout = sys.stderr = None`) живёт в замере §96 отдельным процессом:
внутри pytest подмена потоков ломает его собственный захват, а для здешних
утверждений она ничего не различает — про файл они верны при любой консоли.

⚠ Ловушка модального диалога (`PROTOCOL §5`), замерена здесь дважды. Подменяются
ОБЕ двери: `ErrorReportDialog.exec` (нынешняя) и `QMessageBox.warning` (прежняя,
одностроковая). Вторая — не перестраховка: зонд «вернуть старый `except`» без
неё не краснеет, а ВЕШАЕТ набор — 600 с вместо красного, четвёртый исход зонда.
Утверждения — о ФАКТЕ вызова, не по таймауту.

⚠ Движок подменяется в `modules.graph.core.edit_smooth` — редактор зовёт
`edit_smooth.smooth(...)` атрибутом модуля в момент вызова
(`advanced_graph_editor.py:887`).

⛔ Уборка (пятый исход зонда, `PROTOCOL §5`, замер §96з). Набору нужен ПОКАЗАННЫЙ
контейнер — фокус наблюдаем только внутри активного окна. Первая редакция сносила
его немедленным `shiboken6.delete()`, и весь `tests/ui` умирал
`Windows fatal exception: access violation` на 94 % прогона — в ЧУЖОМ
`test_unsaved_question.py`, на его `processEvents()`, 2 раза из 2 против 0 из 1 без
этого файла. Живое активное окно сносить нельзя: приложение продолжает держать
на него указатель, и детонирует он у того, кто первым крутит очередь. Порядок
здесь: `close()` → `deleteLater()` → адресный слив ТОЛЬКО отложенных удалений
(`sendPostedEvents(None, DeferredDelete)`), а не общий `processEvents()` — общий
в teardown оплачивает чужой мусор трёхзначной ценой (замер 1-36).

Координаты двойственны (CODING_GUIDE §6): centroid/source_point = [y, x],
bbox = [x1, y1, x2, y2].
"""
import json
import logging
import os
import zipfile
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent                    # noqa: E402
from PySide6.QtWidgets import (                      # noqa: E402
    QApplication, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QColor, QImage, QPixmap    # noqa: E402

# Литералы сценария намеренно НЕ импортируются из проверяемого модуля: тест,
# вычисляющий свой вход из проверяемой константы, зелен при любом её значении
# (запрет PROTOCOL §3).
UID = "dn3a5c41"
DIAGRAM_NAME = "проба ДН3"
BOOM_TEXT = "list index out of range"
NOTE_TEXT = "жал «Авто-выравнивание» сразу после правки трубы"
REPORT_NAME = "report.txt"
CANVAS_NAME = "canvas.png"
IMG_W, IMG_H = 800, 600
BOX_W, BOX_H = 1400, 900
# Пол снимка: `grab()` вырожденного виджета отдаёт 1x1, и такой «холст»
# прошёл бы любую проверку на присутствие файла в архиве.
CANVAS_MIN_W, CANVAS_MIN_H = 800, 500


# ── харнесс ──────────────────────────────────────────────────────────────


class FakeAPI:
    """Сервер вкладке здесь не нужен: проверяется путь отказа, а не запись."""


def _graph():
    """Косая труба рамка → рамка: сглаживанию есть что чинить."""
    nodes = [
        {"id": "low", "type": "equipment", "centroid": [400.0, 220.0],
         "bbox": [200.0, 380.0, 240.0, 420.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "high", "type": "equipment", "centroid": [120.0, 260.0],
         "bbox": [240.0, 100.0, 280.0, 140.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [{"id": "e1", "source": "low", "target": "high",
              "source_point": [380.0, 220.0], "target_point": [140.0, 260.0],
              "waypoints": []}]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _boom(graph, **_kwargs):
    """Движок, который успел испортить холст и упал.

    `IndexError` — ровно то, чем падал `shift_run` (`edit_smooth.py:322`)
    на укоротившемся снимке; порча ложится ДО падения, как и в бою.
    """
    graph["links"][0]["waypoints"] = [[999.0, 999.0]]
    raise IndexError(BOOM_TEXT)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_root():
    """Вернуть корневой логгер в исходное состояние после теста.

    `setup_client_logging` штатно чистит хендлеры корня — вместе с
    перехватчиком pytest; без восстановления следующие тесты сессии теряют
    caplog, а открытый файл лога не даёт удалить tmp_path на Windows.
    """
    from app.core import obs

    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    obs.reset()          # контекст в contextvars — иначе uid течёт между тестами
    yield root
    for handler in root.handlers[:]:
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = saved
    root.setLevel(level)


@contextmanager
def only_file_handler(root):
    """Оставить на корне один файловый хендлер (см. шапку модуля)."""
    from logging.handlers import RotatingFileHandler

    files = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(files) == 1, f"ожидался ровно один файловый хендлер: {root.handlers!r}"
    others = [h for h in root.handlers if h is not files[0]]
    for handler in others:
        root.removeHandler(handler)
    try:
        yield files[0]
    finally:
        for handler in others:
            root.addHandler(handler)


@pytest.fixture
def client_log(monkeypatch, tmp_path, isolated_root):
    """Поднять приёмник логов клиента в tmp_path и вернуть путь файла."""
    from ui.services.client_logging import bind_uid, setup_client_logging

    monkeypatch.setenv("PID_LOG_DIR", str(tmp_path / "logs"))
    path = setup_client_logging()
    assert path is not None, "приёмник логов клиента не поднялся"
    bind_uid(UID)
    return path


@pytest.fixture
def reports(monkeypatch):
    """Подменённый `exec` окна отчёта + журнал открытых окон."""
    import ui.widgets.error_report_dialog as erd

    opened = []
    monkeypatch.setattr(
        erd.ErrorReportDialog, "exec",
        lambda self: (opened.append(self), 0)[1],
    )
    return opened


@pytest.fixture
def modals(monkeypatch):
    """Подменённая одностроковая модалка + журнал её вызовов (см. шапку)."""
    calls = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **kw: calls.append(a[1:3])),
    )
    return calls


@pytest.fixture
def tool(qapp, monkeypatch, tmp_path, reports, modals):
    """Вкладка «Ручная правка» с холстом и движком, падающим посреди работы.

    Редактор живёт в ПОКАЗАННОМ контейнере рядом с чужим полем ввода: фокус
    наблюдаем только внутри активного окна, а без соседа «фокус вернулся»
    не отличить от «фокус никуда не уходил».
    """
    from modules.graph.core import canvas_state, edit_smooth
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.tabs.base_graph_tab import BaseGraphTab

    image = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    raster = tmp_path / "raster.png"
    assert image.save(str(raster))
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(_graph()), encoding="utf-8")

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)
    monkeypatch.setattr(edit_smooth, "smooth", _boom)

    tab = AdvancedGraphTab(UID, DIAGRAM_NAME, FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(str(raster), str(graph))
    editor.resize(BOX_W, BOX_H)
    # Ветка кнопки выбирается меткой холста: с раскладкой — сглаживание
    # (`smooth_canvas`), без неё — родной `auto_fix`.
    editor.graph_data.setdefault("graph", {})["canvas_transform"] = {
        "layout_applied": True,
        "layout_version": canvas_state.layout_version(),
    }
    assert canvas_state.has_layout(editor.graph_data)
    tab._editor = editor

    box = QWidget()
    box.resize(BOX_W, BOX_H)
    layout = QVBoxLayout(box)
    thief = QPlainTextEdit()
    thief.setMaximumHeight(40)
    layout.addWidget(editor)
    layout.addWidget(thief)
    box.show()
    QApplication.processEvents()   # окно активируется одним проходом очереди
    thief.setFocus()
    assert thief.hasFocus(), "харнесс не смог отдать фокус соседу"

    yield tab, editor, thief

    # Свои виджеты набор сносит САМ и детерминированно (замеры 1-32, 1-36,
    # 96з): сначала снять окно с экрана, потом отложенное удаление, потом
    # адресный слив очереди удалений. Окна отчёта — дети вкладки, уходят с ней.
    box.close()
    box.deleteLater()
    tab.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _unpack(archive_path):
    """Разобрать архив отчёта: состав, холст как QImage, текст отчёта."""
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        canvas = QImage.fromData(archive.read(CANVAS_NAME))
        report = archive.read(REPORT_NAME).decode("utf-8")
    return names, canvas, report


# ── след отказа в логе клиента ───────────────────────────────────────────


def test_failed_tool_writes_traceback_into_the_client_log(tool, client_log,
                                                          isolated_root):
    """Д2: отказ инструмента виден в ФАЙЛЕ — с трассировкой и uid диаграммы."""
    tab, _editor, _thief = tool

    with only_file_handler(isolated_root) as handler:
        grown_from = client_log.stat().st_size
        tab._auto_fix()
        handler.flush()

    text = client_log.read_text(encoding="utf-8")
    assert client_log.stat().st_size > grown_from, \
        "отказ инструмента не дописал в лог ни байта"
    assert "Traceback" in text, f"в файле нет трассировки: {text[-400:]!r}"
    assert "IndexError" in text and BOOM_TEXT in text, \
        f"в файле нет причины отказа: {text[-400:]!r}"
    line = [ln for ln in text.splitlines() if "Авто-выравнивание" in ln]
    assert line and f"uid={UID}" in line[0], \
        f"строка отказа без uid — с серверной не сшить: {line!r}"


# ── гейт пункта: отчёт содержит холст ────────────────────────────────────


def test_failed_tool_opens_report_with_the_canvas(tool, reports, modals,
                                                 tmp_path):
    """Гейт: оператору открыт ОТЧЁТ, и в нём холст — то, на чём упало."""
    tab, editor, _thief = tool

    tab._auto_fix()

    assert len(reports) == 1, f"окно отчёта не открылось: {reports!r}"
    assert not modals, \
        f"оператору осталась одна строка модалки вместо отчёта: {modals!r}"
    archive = tmp_path / "report.zip"
    reports[0].save_report(archive)

    names, canvas, report = _unpack(archive)
    assert CANVAS_NAME in names, f"в отчёте нет холста: {names}"
    assert canvas.width() >= CANVAS_MIN_W and canvas.height() >= CANVAS_MIN_H, \
        f"снимок выродился: {canvas.width()}x{canvas.height()}"
    assert (canvas.width(), canvas.height()) == (editor.width(), editor.height()), \
        f"в отчёт уехал не весь холст: {canvas.width()}x{canvas.height()} "\
        f"против {editor.width()}x{editor.height()}"
    assert canvas.convertToFormat(QImage.Format.Format_RGB32) == \
        editor.grab().toImage().convertToFormat(QImage.Format.Format_RGB32), \
        "в отчёт уехал не тот холст, который видел оператор"
    small = canvas.scaled(16, 16)
    colours = {small.pixel(x, y) for x in range(16) for y in range(16)}
    assert len(colours) > 1, "холст в отчёте пуст — приложен чистый лист"
    assert BOOM_TEXT in report and "Traceback" in report, \
        f"в отчёте нет причины отказа: {report!r}"


def test_report_carries_the_note_the_operator_typed(tool, reports, tmp_path):
    """Заметка оператора — единственное, чего нет ни в логе, ни в холсте."""
    tab, _editor, _thief = tool

    tab._auto_fix()
    dialog = reports[0]
    dialog._note_view.setPlainText(NOTE_TEXT)

    archive = tmp_path / "report.zip"
    dialog.save_report(archive)

    _names, _canvas, report = _unpack(archive)
    assert NOTE_TEXT in report, f"заметки нет в отчёте: {report!r}"
    assert NOTE_TEXT in dialog.report_text(), "заметки нет в буфере обмена"


def test_empty_note_says_so_instead_of_disappearing(tool, reports):
    """Незаполненная заметка названа явно: «пусто» и «поля не было» — разное."""
    tab, _editor, _thief = tool

    tab._auto_fix()

    assert "не заполнена" in reports[0].report_text(), \
        f"отчёт молчит о заметке: {reports[0].report_text()!r}"


# ── фокус на пути отказа (находка 1-31) ──────────────────────────────────


def test_focus_returns_to_the_editor_after_a_failed_tool(tool):
    """Холст изменён, шаг отмены открыт (1.1) — Ctrl+Z обязан дойти.

    До ДН3 `setFocus()` стоял ВНУТРИ `try` последней строкой удачного пути,
    поэтому на пути падения пропускался, а сверху ещё вставала модалка.
    """
    tab, editor, thief = tool
    assert thief.hasFocus(), "предусловие: фокус у соседа, не у редактора"

    tab._auto_fix()

    assert editor.hasFocus(), "после отказа фокус не вернулся редактору"
    assert not thief.hasFocus()


def test_focus_returns_to_the_editor_after_a_successful_run(tool, monkeypatch,
                                                            reports):
    """Удачный путь не потерян переносом строки в `finally`."""
    from modules.graph.core import edit_smooth

    tab, editor, thief = tool
    monkeypatch.setattr(
        edit_smooth, "smooth",
        lambda graph, **kw: dict.fromkeys(
            ("колено", "излом", "скольжение", "коннектор", "узел", "осталось"), 0),
    )

    tab._auto_fix()

    assert not reports, f"удачный прогон открыл отчёт: {reports!r}"
    assert editor.hasFocus(), "после удачного прогона фокус не у редактора"


# ── границы: отказ снимка и путь этапа сервера ───────────────────────────


def test_report_survives_a_canvas_that_cannot_be_drawn(tool, reports,
                                                       monkeypatch):
    """Отрисовка испорченного холста может сама бросить — отчёт важнее снимка."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    tab, _editor, _thief = tool

    def _no_grab(self, *a, **kw):
        raise RuntimeError("нечего рисовать")

    monkeypatch.setattr(AdvancedGraphEditor, "grab", _no_grab)

    tab._auto_fix()

    assert len(reports) == 1, "без снимка отчёт не открылся вовсе"
    assert "IndexError" in reports[0].report_text(), \
        "отчёт без снимка потерял и причину отказа"


def test_stage_report_of_the_server_is_unchanged(qapp, tmp_path):
    """Отказ ЭТАПА приходит без холста: отчёт остаётся текстом, перезапуск жив."""
    from ui.widgets.error_report_dialog import ErrorReportDialog

    stage = {"stage_type": "detection", "error_code": "E_TIMEOUT",
             "error_message": "этап не уложился в лимит",
             "error_traceback": "Traceback (most recent call last):\n  ..."}
    dialog = ErrorReportDialog(stage, diagram_name=DIAGRAM_NAME)
    try:
        assert dialog._canvas is None
        assert dialog._allow_retry, "у отказа этапа перезапуск обязан остаться"

        path = tmp_path / "stage.txt"
        dialog.save_report(path)

        assert not zipfile.is_zipfile(path), "текстовый отчёт стал архивом"
        text = path.read_text(encoding="utf-8")
        assert "E_TIMEOUT" in text and "этап не уложился в лимит" in text
        assert CANVAS_NAME not in text, "отчёт обещает холст, которого нет"
    finally:
        dialog.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_client_failure_report_offers_no_dead_retry(qapp, reports):
    """Падение ВНУТРИ клиента перезапускать нечем — кнопки быть не должно."""
    from ui.widgets.error_report_dialog import report_exception

    try:
        raise IndexError(BOOM_TEXT)
    except IndexError as exc:
        dialog = report_exception(None, "Авто-выравнивание", exc,
                                  canvas=QPixmap(8, 8))
    try:
        assert reports == [dialog], "окно отчёта не показано оператору"
        assert not dialog._allow_retry
        assert not dialog.exec_retry(), "мёртвая кнопка «Перезапустить» жива"
        assert "IndexError" in dialog.report_text()
    finally:
        dialog.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
