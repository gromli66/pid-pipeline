# -*- coding: utf-8 -*-
"""Пункт 1.x17 — разрушение вкладки обязано гасить её фоновый поток.

Зачем. Оператор ходит по вкладкам весь сеанс, и клиент их РАЗРУШАЕТ: «← Назад»
ведёт в `DiagramWorkspace._close_active_tab` → `_remove_tab_widget`
(`diagram_workspace.py:1525-1536`), где вкладка снимается с раскладки,
теряет родителя и уходит в `deleteLater()`. То же самое делает закрытие
клиента (`main_window.py:304` → `workspace.cleanup()` → `_force_close_tab`).

Каждая из четырёх вкладок в `__init__` поднимает СВОЙ `QThread` без родителя
и тянет в нём артефакты (`junction_tab.py:215`, `base_graph_tab.py:858`,
`pipe_tab.py:243`, `ocr_binding_tab.py:628`). Погасить этот поток умеют ровно
два места — слоты `_on_downloaded` / `_on_download_error` самой вкладки; ни
один путь закрытия не зовёт `cleanup()`. А связи со слотами Qt рвёт при
разрушении получателя. Значит вкладка, закрытая ДО конца загрузки, оставляет
поток, который не погасит уже никто.

Тем же устроены и два потока РАСПОЗНАВАНИЯ (`advanced_graph_tab.py:471`,
`ocr_binding_tab.py:435`): гасит их `_cleanup_recog_thread`, а зовут его
только слоты `_on_recognize_done` / `_on_recognize_error` той же вкладки.
Потоков всего шесть, и здесь проверяются пять из них: у `OcrBindingTab`
взят загрузочный, у `AdvancedGraphTab` — распознавания.

Цена ЗАМЕРЕНА (§102), а не выведена: процесс, доживший до выхода с бегущим
`QThread`, падает на разрушении этого потока — `0xC0000409`, 8 прогонов
из 8 против 0 из 8, когда загрузка успела кончиться. Для оператора это
клиент, который «падает при закрытии» после того, как тот открыл вкладку
и сразу ушёл из неё.

Инвариант пункта: **вкладку разрушили → её фоновый поток кончился.**
Наблюдаемое — `QThread.wait()`, а не внутренние поля: поток либо завершился,
либо нет, и это видно снаружи.

⛔ Гонку из сценария вынесли: подставной сервер ДЕРЖИТ первый артефакт на
`threading.Event`, поэтому «загрузка ещё идёт» в момент «← Назад» — факт
обстановки, а не удача планировщика (`PROTOCOL §3`, запрет тестов по стенным
часам). Таймауты здесь — предохранители от зависания набора, утверждения
на них не строятся.

⛔ Контроль честности обязателен и утверждает РАЗНИЦУ (`PROTOCOL §3`): вкладка,
которую НЕ закрывали, обязана дойти до своего редактора. Без него правка
«гасить поток всегда» была бы зелёной и после того, как сломала бы загрузку.

⚠ Набор поднимает НАСТОЯЩИЕ вкладки с НАСТОЯЩИМ `_download_artifacts`. Все
остальные наборы `tests/ui` подменяют его на `QThread(self)`, который никогда
не стартует (16 попаданий в 7 файлах) — поэтому дефект и не был виден ни
одному из них.
"""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

import json                                                      # noqa: E402
from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import QEvent, QObject, Qt, Signal           # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen         # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

import ui.widgets.diagram_workspace as dw                        # noqa: E402
from tools import corpus                                         # noqa: E402
from ui.services.api_client import APIError, DiagramStatus       # noqa: E402

UID = "d74eb9f1"          # корпус-фикстура в git (пункт 0.8)
W, H = 400, 300
SQ = 20

#: Предохранители от зависания набора. Утверждения на них не строятся:
#: в исправном дереве поток кончается сразу, в дефектном — не кончается вовсе.
JOIN_MS = 5000
HOLD_S = 30


# ── подставной сервер, который умеет ДЕРЖАТЬ загрузку ────────────────────

class HoldingAPI:
    """Отдаёт артефакты, но первый — только после `release()`.

    Так «оператор ушёл, пока схема ещё качалась» становится обстановкой,
    а не совпадением: на момент «← Назад» рабочий поток заведомо внутри
    `download_artifact`.
    """

    def __init__(self, blobs, status, hold=True):
        self.blobs = dict(blobs)
        self.status = status
        self._gate = threading.Event()
        self._entered = threading.Event()
        #: у распознавания свой затвор: вкладка сначала обязана ДОГРУЗИТЬСЯ
        #: (иначе редактора нет и жеста «Распознать» тоже), и только потом
        #: её ловят на втором потоке.
        self._recog_gate = threading.Event()
        self._recog_entered = threading.Event()
        self.calls = []
        if not hold:
            self._gate.set()
            self._entered.set()

    # -- управление задержкой --
    def wait_until_downloading(self):
        """Дождаться, что рабочий поток ВОШЁЛ в загрузку и стоит в ней."""
        return self._entered.wait(HOLD_S)

    def release(self):
        self._gate.set()

    def wait_until_recognizing(self):
        return self._recog_entered.wait(HOLD_S)

    def release_recognition(self):
        self._recog_gate.set()

    # -- поверхность воркспейса --
    def get_diagram(self, uid):
        return _FakeDiagram(self.status)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_stages(self, uid):
        return []

    def start_junction_validation(self, uid):
        return {"status": "validating_junctions"}

    def start_graph_validation(self, uid):
        return {"status": "validating_graph"}

    def start_mask_validation(self, uid):
        return {"status": "validating_masks"}

    def complete_mask_validation(self, uid):
        return {"status": "validated_masks", "task_id": None}

    # -- поверхность вкладок --
    def download_artifact(self, uid, artifact_type, dest_path):
        self.calls.append(artifact_type)
        self._entered.set()
        self._gate.wait(HOLD_S)
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def download_contours_auto(self, uid, dest):
        raise APIError(f"contours_auto not found for {uid}", 404)

    # -- поверхность вкладки привязки OCR (свои методы, не download_artifact) --
    def _endpoint(self, key, dest_path):
        return self.download_artifact(None, key, dest_path)

    def download_ocr_result(self, uid, dest):
        return self._endpoint("ocr_result", dest)

    def download_ocr_binding(self, uid, dest):
        return self._endpoint("ocr_binding", dest)

    def download_ocr_validation(self, uid, dest):
        return self._endpoint("ocr_validation", dest)

    def recognize_boxes(self, uid, boxes):
        self.calls.append("recognize_boxes")
        self._recog_entered.set()
        self._recog_gate.wait(HOLD_S)
        return {"results": [{"text": "K1", "confidence": 0.9} for _ in boxes]}


class _FakeDiagram:
    def __init__(self, status):
        self.status = status
        self.error_stage = None
        self.project_code = "thermohydraulics"


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


class FakeMsgBox:
    """Модалка в пути закрытия подменяется утверждением о факте, не таймаутом
    (`PROTOCOL §5`, четвёртый исход зонда)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.No

    @classmethod
    def question(cls, *a, **kw):
        cls.calls.append("question")
        return cls.answer

    @classmethod
    def warning(cls, *a, **kw):
        cls.calls.append("warning")
        return cls.StandardButton.Ok

    @classmethod
    def critical(cls, *a, **kw):
        cls.calls.append("critical")
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *a, **kw):
        cls.calls.append("information")
        return cls.StandardButton.Ok


# ── данные ───────────────────────────────────────────────────────────────

def _png(img, path):
    assert img.save(str(path)), f"растр не сохранился: {path}"
    return path.read_bytes()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def blobs(qapp, tmp_path_factory) -> dict:
    """Всё, что четыре вкладки просят с сервера: растры масок и корпусный граф."""
    root = tmp_path_factory.mktemp("tab_close_blobs")

    def squares(centers):
        img = QImage(W, H, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        p.setPen(QPen(Qt.PenStyle.NoPen))
        p.setBrush(QColor("white"))
        for cx, cy in centers:
            p.drawRect(cx - SQ // 2, cy - SQ // 2, SQ, SQ)
        p.end()
        return img

    def lines():
        img = QImage(W, H, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        p.setPen(QPen(QColor("white"), 3, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.FlatCap))
        p.drawLine(40, 200, 360, 200)
        p.end()
        return img

    gpath = corpus.graph_path(UID)
    assert gpath is not None, f"корпус-фикстура {UID} не найдена (tools/corpus.py)"
    graph = gpath.read_bytes()
    gh, gw = json.loads(graph.decode("utf-8"))["graph"]["image_size"]
    big = QImage(gw, gh, QImage.Format.Format_RGB32)
    big.fill(QColor("white"))

    points = {"junctions": [{"x": 120, "y": 200, "size": SQ}],
              "bridges": [{"x": 80, "y": 80, "size": SQ}]}

    return {
        "original_image": _png(big, root / "original.png"),
        "graph_validated": graph,
        "graph_json": graph,
        "coco_validated": json.dumps({"annotations": []}).encode(),
        "junction_mask_validated": _png(squares([(120, 200)]),
                                        root / "junction.png"),
        "bridge_mask_validated": _png(squares([(80, 80), (200, 80)]),
                                      root / "bridge.png"),
        "skeleton_final": _png(lines(), root / "skeleton.png"),
        "junction_points_validated": json.dumps(points).encode(),
        "pipe_mask_validated": _png(lines(), root / "pipe.png"),
        # Привязка OCR: содержимое не разбирается — вкладку рвут раньше, чем
        # она доберётся до артефактов; важно лишь, что задание не отвалится
        # до входа в затвор.
        "ocr_result": json.dumps({"blocks": []}).encode(),
    }


#: Ключ этапа → статус, при котором оператор туда заходит.
#: Открывается тем же вызовом, что висит на кнопке этапа
#: (`diagram_workspace.py:638-656`).
TABS = {
    "junction": DiagramStatus.DETECTED_JUNCTIONS,
    "pipe": DiagramStatus.SKELETONIZED,
    "val_graph": DiagramStatus.BUILT,
    "ocr_binding": DiagramStatus.OCR_COMPLETED,
}


@pytest.fixture
def bench(qapp, monkeypatch, blobs):
    """Воркспейс на держащем сервере; вкладки — настоящие."""
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.No
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)

    made = []

    def _make(status, hold=True):
        api = HoldingAPI(blobs, status, hold=hold)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        ws.load_diagram(UID, "проба 1.x17")
        made.append((ws, api))
        return ws, api

    yield _make

    # Свои виджеты набор сносит сам и детерминированно (форма 1-36); держащий
    # сервер отпускается ДО сноса, иначе рабочий поток остался бы стоять
    # в `download_artifact` и утащил бы за собой весь прогон.
    for ws, api in made:
        api.release()
        ws.cleanup()
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ── жесты оператора ──────────────────────────────────────────────────────

def _open(ws, key):
    """Открыть этап тем же вызовом, что зарегистрирован за его кнопкой."""
    ws._original_handlers[key]()
    assert ws._active_tab is not None, f"вкладка «{key}» не открылась"
    return ws._active_tab


def _back(ws):
    """«← Назад» — боевой жест закрытия (кнопка врезана в тулбар вкладки).

    ⛔ Отложенное удаление доигрывается ЗДЕСЬ и это обязательная часть жеста,
    а не уборка. `_remove_tab_widget` зовёт `deleteLater()`, то есть на выходе
    из клика вкладка ЕЩЁ ЖИВА; в бою её добивает первый же оборот
    `QApplication.exec()`. В наборе оборота нет, а `processEvents()`
    `DeferredDelete` не доставляет вовсе (замер §95д) — без этой строки
    вкладка доживает до конца загрузки, штатно получает результат и гасит
    поток сама. Обстановка разошлась бы с боевой, и дефект стал бы невидим:
    первая редакция набора была зелёной на дефектном дереве именно так.
    """
    assert ws._btn_back_injected is not None, "кнопки «← Назад» во вкладке нет"
    ws._btn_back_injected.click()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _join(thread, ms=JOIN_MS) -> bool:
    """Дождаться конца потока, НЕ запирая главный цикл.

    Голый `QThread.wait()` здесь не годится и это не мелочь: живой вкладке
    результат загрузки приходит ОЧЕРЕДЬЮ в главный поток, а блокирующее
    ожидание её же туда и не пускает — контроль честности падал бы всегда.
    Крутить цикл обязан и бой: там он крутится сам.
    """
    deadline = time.monotonic() + ms / 1000.0
    while thread.isRunning() and time.monotonic() < deadline:
        QApplication.processEvents()
        thread.wait(20)
    return not thread.isRunning()


# ── дефект пункта ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", sorted(TABS))
def test_closing_tab_during_download_stops_its_thread(bench, key):
    """Ушёл из вкладки, пока она качалась → её поток кончился.

    До правки поток не кончался никогда: `quit()` живёт только в слотах
    вкладки, а связи с ними Qt рвёт вместе с самой вкладкой.
    """
    ws, api = bench(TABS[key])

    tab = _open(ws, key)
    thread = tab._download_thread
    assert api.wait_until_downloading(), "рабочий поток не дошёл до загрузки"
    assert thread.isRunning(), "обстановка не та: загрузка не идёт"

    _back(ws)
    assert ws._active_tab is None, "вкладка не закрылась"

    api.release()          # сервер наконец ответил — загрузка доигрывает
    assert _join(thread), (
        f"поток загрузки вкладки «{key}» пережил её разрушение: "
        "гасить его больше некому — процесс упадёт на выходе"
    )


def test_second_tab_after_early_exit_also_stops(bench):
    """Прогон ПОСЛЕ чужого действия оператора, а не с чистого листа.

    Оператор заглянул в перекрёстки и сразу вышел, потом открыл проверку
    схемы и вышел из неё тоже. Оба потока обязаны кончиться (`PROTOCOL §3`:
    хотя бы один сценарий идёт по уже пожившему состоянию).
    """
    ws, api = bench(DiagramStatus.DETECTED_JUNCTIONS)

    first = _open(ws, "junction")
    t1 = first._download_thread
    assert api.wait_until_downloading()
    _back(ws)

    api.release()
    assert _join(t1), "первый поток пережил свою вкладку"

    second = _open(ws, "val_graph")
    t2 = second._download_thread
    _back(ws)
    assert _join(t2), "второй поток пережил свою вкладку"


def test_closing_tab_does_not_touch_dead_widgets(bench, capfd):
    """Загрузчик закрытой вкладки не лезет в её разрушенные виджеты.

    Ход загрузки вкладка показывает лямбдой `progress` без получателя-QObject
    (`junction_tab.py:223`). У такой связи нет ни потока получателя, ни
    времени жизни: она исполняется в РАБОЧЕМ потоке (замер §102) и продолжает
    звать `status_label.setText` после того, как метку разрушили. Наружу это
    выходит потоком `RuntimeError: Internal C++ object ... already deleted`.
    """
    ws, api = bench(DiagramStatus.DETECTED_JUNCTIONS)

    tab = _open(ws, "junction")
    thread = tab._download_thread
    assert api.wait_until_downloading()
    capfd.readouterr()                     # отбросить всё, что было до жеста

    _back(ws)
    api.release()
    _join(thread)
    QApplication.processEvents()

    err = capfd.readouterr().err
    assert "already deleted" not in err, (
        "загрузчик пишет в разрушенные виджеты закрытой вкладки:\n" + err[:2000]
    )


def test_closing_tab_during_recognition_stops_its_thread(bench):
    """ВТОРОЙ поток вкладки — распознавания — обязан кончиться так же.

    Здесь вкладка живёт полной жизнью: догрузилась, собрала редактор,
    оператор запустил распознавание кнопкой и ушёл. Гасит поток
    `_cleanup_recog_thread`, а зовут его только слоты `_on_recognize_done` /
    `_on_recognize_error` — те же связи, которые Qt рвёт с вкладкой.
    """
    ws, api = bench(DiagramStatus.VALIDATED_GRAPH, hold=False)

    tab = _open(ws, "edit_graph")
    assert _join(tab._download_thread), "загрузка редактора не кончилась"
    QApplication.processEvents()
    assert tab._editor is not None, "обстановка не та: редактор не собрался"

    # Что именно распознавать — вход сценария, а не проверяемый механизм:
    # настоящие пустые блоки пришлось бы рисовать в редакторе мышью.
    tab._editor.get_pending_ocr_boxes = lambda: ([1], [[10, 10, 60, 30]])
    tab.btn_recognize.click()

    thread = tab._recog_thread
    assert thread is not None, "жест «Распознать» не поднял поток"
    assert api.wait_until_recognizing(), "поток не дошёл до распознавания"

    _back(ws)
    assert ws._active_tab is None, "вкладка не закрылась"

    api.release_recognition()
    assert _join(thread), (
        "поток распознавания пережил свою вкладку: гасить его больше некому"
    )


# ── контроль честности ───────────────────────────────────────────────────

def test_control_open_tab_still_gets_its_artifacts(bench):
    """Вкладку НЕ закрывали → она собралась и поток кончился штатно.

    Утверждается РАЗНИЦА, а не совпадение с состоянием «до»: без этого
    контроля правка «гасить поток всегда» осталась бы зелёной, даже если бы
    гасила его ДО того, как вкладка получила артефакты.
    """
    ws, api = bench(DiagramStatus.DETECTED_JUNCTIONS, hold=False)

    tab = _open(ws, "junction")
    thread = tab._download_thread
    assert _join(thread), "загрузка живой вкладки не кончилась"
    QApplication.processEvents()

    assert tab._editor is not None, "живая вкладка не собрала редактор"
    assert "original_image" in api.calls, "артефакты не запрашивались"
    assert ws._active_tab is tab, "вкладка закрылась сама"
