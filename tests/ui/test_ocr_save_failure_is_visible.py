# -*- coding: utf-8 -*-
"""Пункт 1.9 дороги — ложь «✅ Сохранено» во вкладке «Привязка подписей».

Замер до правки (`ui/tabs/ocr_binding_tab.py:1653-1656`, адрес плана :1746-1751
уехал): последний шаг сохранения — заливка `ocr_validation.json` — обёрнут
собственным `except Exception: logger.warning(...)`, и сразу после него стоят
`self._saved = True` и статус «✅ Сохранено: …». То есть отказ сервера на этом
шаге оператор не видит НИКАК: вкладка объявляет работу сохранённой, дёрти-флаг
снимается, диалог о несохранённом при закрытии не появится — тихая потеря
работы, класс волны 1.

Инвариант пункта: **вкладка объявляет «Сохранено» только если на сервер ушло
всё, что она собиралась отправить.** Отказ любого из трёх шагов записи
(`save_ocr_binding`, `upload_validated_graph`, `save_ocr_validation`) обязан
дойти до оператора диалогом, оставить вкладку грязной и оставить след в ФАЙЛЕ
лога клиента (`ui/services/client_logging.py`, пункт 1.10) — `print_exc()` в
собранном `.exe` молча умирает (`sys.stderr = None`, замер §41.9), поэтому
единственный годный приёмник — логгер.

Что проверяется — только ДАННЫЕ и наблюдаемое оператором: что ушло на сервер,
текст статусной строки, значение дёрти-флага, факт вызова диалога, содержимое
файла лога. Ни одного утверждения про внутреннюю кухню вкладки.

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом:
модальный диалог в пути инъекции подвешивает набор вместо падения (PROTOCOL §5,
замеры 1.5 и 1.3).

Числа абсолютные и заперты с двух сторон: и «в статусе есть 1 привязка / 2
блока», и «в статусе нет ✅» — иначе тест остался бы зелёным на пустой вкладке.
"""
import json
import logging
import os
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox        # noqa: E402

UID = "1f9defec"

BLOCK_KKS = {"bbox": [60.0, 30.0, 140.0, 50.0], "text": "10LAB10AP001",
             "confidence": 0.9, "source": "paddle", "origin": "auto"}
BLOCK_DN = {"bbox": [40.0, 240.0, 90.0, 258.0], "text": "DN100",
            "confidence": 0.7, "source": "paddle", "origin": "auto"}

N_BLOCKS = 2       # оба блока с текстом — оба уходят на сервер
N_BINDINGS = 1     # одна привязка блока к узлу


@dataclass
class _Classification:
    """Заглушка `BlockClassification`: `_save_binding` зовёт по ней `asdict`."""
    block_idx: int
    category: str
    matched: bool


class FakeAPI:
    """Сервер: помнит разобранное содержимое заливок, умеет отказать на любом шаге."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []
        self.uploads = {}

    def _step(self, kind, path):
        self.calls.append(kind)
        if self.fail_on == kind:
            raise RuntimeError(f"сервер отказал на {kind}")
        with open(path, encoding="utf-8") as fh:
            self.uploads[kind] = json.load(fh)
        return True

    def save_ocr_binding(self, uid, path):
        return self._step("binding", path)

    def upload_validated_graph(self, uid, path):
        return self._step("graph", path)

    def save_ocr_validation(self, uid, path):
        return self._step("validation", path)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


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


@pytest.fixture
def dialogs(monkeypatch):
    """Подмена модальных диалогов: набор не имеет права виснуть (PROTOCOL §5)."""
    seen = []

    def _warn(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_warn))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(_warn))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(_warn))
    return seen


def _make_tab(qapp, monkeypatch, api):
    """Вкладка «Привязка подписей» с данными в редакторе и подставным сервером.

    Скачивание артефактов снято: сети в тесте нет, состояние ставится тем же
    набором полей, которым его оставляет боевая загрузка.
    """
    from ui.tabs.ocr_binding_tab import OcrBindingTab

    monkeypatch.setattr(OcrBindingTab, "_start_download", lambda self: None)
    tab = OcrBindingTab(UID, "проба 1.9", api)

    tab._graph_data = {
        "nodes": [{"id": "node_1", "class_name": "nasos",
                   "centroid": [100.0, 100.0], "bbox": [70.0, 80.0, 130.0, 120.0]}],
        "links": [{"id": "edge_1", "source": "node_1", "target": "node_1"}],
    }
    tab._classifications = [_Classification(0, "kks", True)]

    ed = tab.editor
    ed._ocr_blocks = [dict(BLOCK_KKS), dict(BLOCK_DN)]
    ed._bindings = [{"ocr_block_idx": 0, "node_id": "node_1", "text": "10LAB10AP001"}]
    ed._diameter_bindings = []
    ed._kks_bindings = []
    ed._validation_results = []
    tab._saved = False
    return tab


@pytest.fixture
def tab_ok(qapp, monkeypatch):
    api = FakeAPI()
    tab = _make_tab(qapp, monkeypatch, api)
    yield tab, api
    tab.cleanup()


@pytest.fixture
def tab_failing(qapp, monkeypatch):
    """Отказ ровно на последнем шаге — том, который вкладка глотала."""
    api = FakeAPI(fail_on="validation")
    tab = _make_tab(qapp, monkeypatch, api)
    yield tab, api
    tab.cleanup()


# ── контроль: успешное сохранение остаётся успешным ──────────────────────

def test_successful_save_reports_saved(tab_ok, dialogs, client_log):
    tab, api = tab_ok

    assert tab._save_binding() is True

    assert api.calls == ["binding", "graph", "validation"]
    assert len(api.uploads["binding"]["edited_blocks"]) == N_BLOCKS
    assert len(api.uploads["binding"]["bindings"]) == N_BINDINGS
    assert tab.has_unsaved_changes() is False
    text = tab.status_label.text()
    assert "✅ Сохранено" in text
    assert f"{N_BINDINGS} привязок" in text
    assert f"{N_BLOCKS} блоков" in text
    assert dialogs == []


# ── дефект пункта: отказ последнего шага записи ──────────────────────────

def test_failed_validation_upload_is_not_reported_as_saved(tab_failing, dialogs,
                                                           client_log):
    """Сервер отказал на ocr_validation — «Сохранено» появиться не имеет права."""
    tab, api = tab_failing

    assert tab._save_binding() is False

    assert "validation" in api.calls, "шаг записи вообще не выполнялся — сверять нечего"
    assert "✅" not in tab.status_label.text()
    assert "Сохранено" not in tab.status_label.text()


def test_failed_validation_upload_keeps_tab_dirty(tab_failing, dialogs, client_log):
    """Дёрти-флаг остаётся грязным: диалог при закрытии обязан появиться."""
    tab, api = tab_failing

    tab._save_binding()

    assert tab.has_unsaved_changes() is True


def test_failed_validation_upload_shows_dialog(tab_failing, dialogs, client_log):
    """Оператор видит ошибку — ровно один диалог, и в нём причина."""
    tab, api = tab_failing

    tab._save_binding()

    assert len(dialogs) == 1, f"диалогов {len(dialogs)}, ожидался один: {dialogs}"
    title, text = dialogs[0]
    assert "Ошибка" in title
    assert "сервер отказал на validation" in text


def test_failed_validation_upload_leaves_trace_in_client_log(tab_failing, dialogs,
                                                             client_log):
    """След ошибки — в ФАЙЛЕ лога клиента, с uid и трассировкой."""
    tab, api = tab_failing

    tab._save_binding()

    for handler in logging.getLogger().handlers:
        handler.flush()
    content = client_log.read_text(encoding="utf-8", errors="replace")
    assert "сервер отказал на validation" in content
    assert "Traceback" in content
    assert f"uid={UID}" in content


# ── тот же инвариант для шагов, которые вкладка уже роняла ───────────────

@pytest.mark.parametrize("step", ["binding", "graph"])
def test_failed_earlier_step_is_not_reported_as_saved(qapp, monkeypatch, dialogs,
                                                      client_log, step):
    api = FakeAPI(fail_on=step)
    tab = _make_tab(qapp, monkeypatch, api)
    try:
        assert tab._save_binding() is False
        assert "Сохранено" not in tab.status_label.text()
        assert tab.has_unsaved_changes() is True
        assert len(dialogs) == 1
    finally:
        tab.cleanup()
