# -*- coding: utf-8 -*-
"""Пункт 1.x17 — закрытая вкладка привязки OCR не оставляет сирот у оператора.

Зачем. `ocr_binding_tab.py:312` и `:345` создают `kks_toolbar` и `diam_toolbar`
со всеми кнопками и связями, но в раскладку их не кладёт НИКТО: обе строки
`sub_tabs.addTab(...)` закомментированы (`:341`, `:373`, пометка «П3:
подвкладка KKS убрана»), а самого `sub_tabs` в коде уже нет — только
в комментариях. Виджет без родителя и без раскладки — ВЕРХНЕУРОВНЕВЫЙ,
и разрушение вкладки его не касается: он живёт до конца процесса.

Это не тестовая мелочь: вкладку открывает ОПЕРАТОР, и каждое открытие
оставляет ему две панели с кнопками. Замер 1-42 (§95л): 62 таких тулбара
после одного набора — 31 вкладка × 2.

Наблюдаемое — РАЗНИЦА множеств `QApplication.topLevelWidgets()` до создания
вкладки и после её разрушения (`PROTOCOL §3`: утверждать изменение, а не
совпадение с состоянием «до»). Совпадение здесь зелено и на дефектном
дереве, если считать «сколько всего верхнеуровневых» без привязки к моменту.
"""
import os
from collections import Counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent                                # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

import ui.tabs.ocr_binding_tab as ocr_binding_tab                # noqa: E402
from ui.tabs.ocr_binding_tab import OcrBindingTab                # noqa: E402

from tests.ui.test_tab_close_stops_threads import (              # noqa: E402
    UID, FakeMsgBox, HoldingAPI, blobs, qapp,
)

__all__ = ["blobs", "qapp"]      # фикстуры, взятые у соседнего набора


def _top_level():
    """СЧЁТ верхнеуровневых виджетов по классам.

    ⛔ Ключом здесь нельзя брать `id()` питоньего объекта, и это замерено
    базовым гейтом: `topLevelWidgets()` отдаёт КАЖДЫЙ РАЗ НОВЫЕ обёртки
    над теми же C++ объектами, поэтому адреса двух снимков не сравнимы.
    В одиночку набор был зелёным (обёрток мало, они переживали снимок),
    а в компании давал «утекло 10 QFrame», которых никто не создавал.
    Счёт по классам от личности обёртки не зависит вовсе.
    """
    return Counter(type(w).__name__ for w in QApplication.topLevelWidgets())


@pytest.fixture
def open_and_close(qapp, blobs, monkeypatch):
    """Открыть настоящую вкладку и разрушить её так, как это делает клиент.

    Сервер ДЕРЖИТ артефакты: вкладку рвут до конца загрузки — то есть тем
    самым жестом, которым оператор из неё уходит. Поток отпускается и
    дожидается уже после замера, иначе он утащил бы за собой весь прогон.
    """
    monkeypatch.setattr(ocr_binding_tab, "QMessageBox", FakeMsgBox)
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.No
    made = []

    def _run():
        before = _top_level()
        api = HoldingAPI(blobs, None)
        tab = OcrBindingTab(UID, "проба 1.x17", api)
        made.append((api, tab._download_thread))
        assert api.wait_until_downloading(), "рабочий поток не дошёл до загрузки"

        # Ровно то, что делает `_remove_tab_widget` (`diagram_workspace.py:1529`).
        tab.hide()
        tab.setParent(None)
        tab.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return before, _top_level()

    yield _run

    # ⛔ Дождаться КОНЦА потока обязательно: процесс, доживший до выхода
    # с бегущим `QThread`, падает на его разрушении (0xC0000409) — и это
    # выглядит как «набор сломан», хотя сломана обвязка. Ссылку на поток
    # держим отдельно: сама вкладка к этому моменту уже разрушена.
    for api, thread in made:
        api.release()
        _join(thread)


def _join(thread, seconds=20.0):
    """Крутить цикл событий, пока поток не кончится. Предохранитель, не утверждение."""
    import time
    deadline = time.monotonic() + seconds
    while thread.isRunning() and time.monotonic() < deadline:
        QApplication.processEvents()
        thread.wait(20)
    return not thread.isRunning()


def test_closed_ocr_tab_leaves_no_top_level_widgets(open_and_close):
    """Вкладку разрушили → верхнеуровневых виджетов стало не больше.

    До правки оставались два `_SubTabToolbar`; после — ни одного.
    """
    before, after = open_and_close()

    leaked = after - before          # Counter: остаются только приросты
    assert not leaked, (
        "закрытая вкладка привязки OCR оставила у оператора "
        f"{sum(leaked.values())} верхнеуровневых виджет(ов): {dict(leaked)}"
    )


def test_control_toolbars_are_still_built(open_and_close, blobs, qapp,
                                          monkeypatch):
    """Контроль честности: панели по-прежнему создаются и обслуживаются.

    Утверждается РАЗНИЦА с «просто удалить их»: `_update_other_stats`
    (`:576`, `:584`) и `_reset_all_modes` (`:1294`) обращаются к обеим
    панелям по имени, и снос вместо переподчинения свалил бы вкладку
    там, а не здесь.
    """
    monkeypatch.setattr(ocr_binding_tab, "QMessageBox", FakeMsgBox)
    api = HoldingAPI(blobs, None)
    tab = OcrBindingTab(UID, "проба 1.x17", api)
    thread = tab._download_thread
    try:
        assert api.wait_until_downloading()
        for name in ("kks_toolbar", "diam_toolbar"):
            panel = getattr(tab, name, None)
            assert panel is not None, f"панель {name} не построена"
            panel.stats_label.setText("проба")
            assert panel.stats_label.text() == "проба"
        tab._reset_all_modes()
    finally:
        api.release()
        tab.cleanup()
        tab.hide()
        tab.setParent(None)
        tab.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _join(thread)
