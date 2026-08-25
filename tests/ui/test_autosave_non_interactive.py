# -*- coding: utf-8 -*-
"""Пункт 1-38 дороги — сохранение ПО ТАЙМЕРУ не задаёт вопросов оператору.

Зачем. `AutoSaveService` тикает раз в 120 с и включён по умолчанию
(`ui_settings.py:24-33`), а зовёт он ровно те методы, которые оператор жмёт
кнопкой (`autosave._SAVE_METHODS`). У всех этих методов на пути стоят модалки,
и на фоновом тике каждая из них — дефект:

* **вопрос о слепой перезаписи** (`_confirm_blind_overwrite`, пункты 1-33
  и 1-39) на тике встаёт посреди работы оператора, а «Да» в нём СНИМАЕТ запрет
  НАСОВСЕМ (`self._unreadable_on_server.discard(...)`). То есть автосохранение
  способно молча отменить только что купленную защиту — и цена этого больше
  цены самого вопроса;
* **предупреждение об отказе** (`QMessageBox.warning` в `except`) на тике
  всплывает без единого жеста оператора.

Лечить в `autosave.py` нечего: сервис диалогов не поднимает и заглушить их
не может — они сидят у самих вкладок. Поэтому режим неинтерактивного
сохранения живёт у КЛАССОВ ВКЛАДОК: по таймеру вкладка не спрашивает,
а отказывается и говорит об этом СТРОКОЙ; ручное сохранение по-прежнему
спрашивает.

⛔ Запрет 1-33/1-39 режим НЕ обходит: отказ по таймеру оставляет запрет
взведённым, и ручной заход спросит снова. Проверяется с двух сторон — иначе
«не спрашивает» лечилось бы разрешением записи вслепую.

Полная популяция дверей (снята по коду 2026-08-20, `MEASUREMENTS §93`):
шесть классов карты `_SAVE_METHODS` и восемь мест диалога —
`BaseGraphTab._confirm_blind_overwrite`, `BaseGraphTab._save_graph`,
`ContourTab._save_graph`, `JunctionTab._save_masks`,
`JunctionTab._upload_points`, `PipeTab._save_mask`,
`OcrBindingTab._confirm_blind_overwrite`, `OcrBindingTab._save_binding`.

⚠ Тик подаётся ДЕТЕРМИНИРОВАННО — сигналом таймера, а не ожиданием по стенным
часам (`PROTOCOL §3`, замер 1-39: ожидание тика `QTimer(10 мс)` с дедлайном
по `time.monotonic()` даёт красное без дефекта на загруженной машине).

⚠ `QMessageBox` подменяется с утверждением о ФАКТЕ вызова, а не таймаутом
(`PROTOCOL §5`): без подмены набор повис бы на модалке, а не упал. Подмена
ставится на КЛАСС, а не на алиас модуля, — переезд диалога между модулями
делает такую подмену немой (замер 1-18).

⚠ Виджеты сносятся детерминированно (ловушка 1-32).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                     # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                          # noqa: E402

from PySide6.QtCore import QPointF, Qt, QThread                   # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox           # noqa: E402

from tools import corpus                                          # noqa: E402
from ui.services.api_client import APIError                       # noqa: E402
from ui.services.autosave import AutoSaveService, _SAVE_METHODS   # noqa: E402
from ui.services.ui_settings import UISettings                    # noqa: E402

GRAPH_UID = "d74eb9f1"        # корпус-фикстура в git (пункт 0.8)
MASK_UID = "1c38aa01"
BIND_UID = "1c38bb02"
W, H = 400, 300

#: узел корпусной схемы, которым делается ЧЕСТНАЯ правка через стек команд
NID = "node_11"
DRAG_TO = (500.0, 400.0)

SQ = 20
BRIDGE_SQUARES = [(80, 80), (200, 80), (320, 80)]
POINTS_SAVED = {
    "junctions": [{"x": 120, "y": 200, "size": 21}],
    "bridges": [{"x": x, "y": y, "size": SQ} for x, y in BRIDGE_SQUARES],
}

N_SAVED = 3                   # узлов в сохранённом графе привязок
N_BINDINGS = 2                # привязок в сохранённой работе оператора


# ── общая обстановка ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки → список (title, text). Пустой список = вкладка молчала.

    Отвечает «Да»: если вопрос о слепой перезаписи всё-таки будет задан
    на тике, ответ «Да» воспроизведёт ровно тот дефект, ради которого пункт
    и заведён, — запрет снимется насовсем. Тест это увидит.
    """
    seen = []

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Yes

    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_rec))
    # Вопрос «да / отмена» с пункта 5.3 собирается своими кнопками (русскими),
    # мимо статической двери `QMessageBox.question`, — подменяется отдельно.
    from ui.tabs.blind_overwrite import BlindOverwriteGuard
    monkeypatch.setattr(
        BlindOverwriteGuard, "_ask_yes_cancel",
        lambda self, title, text:
            _rec(self, title, text) == QMessageBox.StandardButton.Yes)
    return seen


@pytest.fixture(autouse=True)
def autosave_on(monkeypatch):
    """Автосохранение включено — как в бою (дефолт `True`), но без QSettings."""
    monkeypatch.setattr(UISettings, "autosave_enabled",
                        property(lambda self: True))


def _tick(tab):
    """Тик автосохранения по живой вкладке — тем же кодом, что в бою.

    Детерминированно: `start()` заводит таймер, а тик подаётся сигналом
    `timeout`. Проверяется ШОВ — таймер заведён и его сигнал доходит
    до обработчика; срабатывание таймера ПО ВРЕМЕНИ — контракт Qt, а не наш,
    и ожидание его по стенным часам красит набор без дефекта (замер 1-39).
    """
    service = AutoSaveService()
    service.start(tab)
    assert service._timer.isActive(), "start() не завёл таймер автосохранения"
    service._timer.timeout.emit()
    service.stop()
    service.deleteLater()


def _line(tab) -> str:
    return tab.status_label.text()


def _titles(dialogs):
    return [title for title, _ in dialogs]


# ══════════════════════════════════════════════════════════════════════════
# ПОПУЛЯЦИЯ: карта сохранений и режим
# ══════════════════════════════════════════════════════════════════════════

def test_every_autosave_class_carries_the_mode():
    """Перебор ПОЛНОЙ карты `_SAVE_METHODS`, а не примера из неё.

    Режим — свойство КЛАССА вкладки; класс, попавший в карту без режима,
    вернёт фоновую модалку молча (политика копируется вместе со строкой карты
    и теряется незаметно — та же механика, что у `swallow`/`failure_key`).
    """
    import importlib

    modules = {
        "PipeTab": "ui.tabs.pipe_tab",
        "JunctionTab": "ui.tabs.junction_tab",
        "SimpleGraphTab": "ui.tabs.simple_graph_tab",
        "AdvancedGraphTab": "ui.tabs.advanced_graph_tab",
        "ContourTab": "ui.tabs.contour_tab",
        "OcrBindingTab": "ui.tabs.ocr_binding_tab",
    }
    assert set(modules) == set(_SAVE_METHODS), (
        f"карта сохранений разошлась с перебором: {set(_SAVE_METHODS)}")

    without = []
    for name, mod in modules.items():
        cls = getattr(importlib.import_module(mod), name)
        assert hasattr(cls, _SAVE_METHODS[name]), (
            f"{name} не несёт {_SAVE_METHODS[name]}")
        if not hasattr(cls, "non_interactive_save"):
            without.append(name)

    assert without == [], (
        f"классы карты без неинтерактивного режима: {without}")


# ══════════════════════════════════════════════════════════════════════════
# ГРАФОВЫЕ ВКЛАДКИ
# ══════════════════════════════════════════════════════════════════════════

class GraphAPI:
    """Подставной сервер графовой вкладки: что скачали и что залили."""

    def __init__(self, blobs, failures=None, contours=None):
        self.blobs = dict(blobs)
        self.failures = dict(failures or {})
        self.contours = contours
        self.uploads = []
        self.refuse_upload = None

    def download_artifact(self, uid, artifact_type, dest_path):
        exc = self.failures.get(artifact_type)
        if exc is not None:
            raise exc
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def _upload(self, kind, path):
        if self.refuse_upload is not None:
            raise self.refuse_upload
        self.blobs[kind] = Path(path).read_bytes()
        self.uploads.append(kind)
        return True

    def upload_canvas_graph(self, uid, path):
        return self._upload("graph_canvas", path)

    def upload_validated_graph(self, uid, path):
        return self._upload("graph_validated", path)

    def upload_contours_validated(self, uid, path):
        return self._upload("contours_validated", path)

    def upload_contours_training(self, uid, path):
        return self._upload("contours_training", path)

    def download_contours_auto(self, uid, dest):
        if self.contours is None:
            raise APIError(f"contours_auto not found for {uid}", 404)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.contours)
        return dest


@pytest.fixture(scope="module")
def graph_blobs(qapp, tmp_path_factory):
    path = corpus.graph_path(GRAPH_UID)
    assert path is not None, f"корпус-фикстура {GRAPH_UID} не найдена"
    graph = path.read_bytes()
    h, w = json.loads(graph.decode("utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("graph_raster") / f"{GRAPH_UID}.png"
    assert img.save(str(png))
    return {"original_image": png.read_bytes(), "graph_json": graph,
            "graph_validated": graph, "graph_canvas": graph}


@pytest.fixture(scope="module")
def contours_auto(graph_blobs):
    """`contours_auto.json` под корпусную схему: полигоны SAM2 у узлов с `ann_idx`.

    Нужен, чтобы кнопка «Применить все» вкладки контуров дала НАСТОЯЩУЮ
    команду в `undo_mgr` — то есть чтобы вкладка стала грязной так же, как
    от жеста оператора, а не подкруткой флага.
    """
    graph = json.loads(graph_blobs["graph_json"].decode("utf-8"))
    nodes = [n for n in graph["nodes"]
             if n.get("ann_idx") is not None and n.get("bbox")]
    assert nodes, "в корпусной схеме нет узлов с ann_idx — стенд не собрать"
    n = nodes[0]
    x0, y0, x1, y1 = n["bbox"]
    return json.dumps({
        "nodes": [{
            "ann_id": n["ann_idx"],
            "polygon_auto": [x0, y0, x1, y0, x1, y1, x0, y1],
            "confidence": 0.95,
            "status": "auto",
        }],
        "stats": {"auto": 1, "manual_review": 0},
    }).encode()


@pytest.fixture
def open_graph_tab(qapp, monkeypatch, dialogs, graph_blobs):
    """Вкладка графа, собранная тем же слотом `_on_downloaded`, что в бою."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.base_graph_tab import BaseGraphTab, _graph_jobs

    opened = []

    def _open(cls, api):
        monkeypatch.setattr(
            BaseGraphTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = cls(GRAPH_UID, "проба 1-38", api)
        opened.append(tab)

        out = {}
        dl = ArtifactDownloader(api, GRAPH_UID, tab.temp_dir,
                                _graph_jobs(want_canvas=cls.USE_CANVAS))
        dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
        dl.error.connect(lambda m: out.__setitem__("error", m))
        dl.run()
        assert out.get("error") is None, f"загрузчик увёл вкладку в ошибку: {out}"
        tab._on_downloaded(out["artifacts"])
        assert tab._editor is not None, "редактор не собрался"
        del dialogs[:]          # предупреждения ОТКРЫТИЯ — не предмет пункта
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()
        tab.deleteLater()
    qapp.processEvents()


def _simple():
    from ui.tabs.simple_graph_tab import SimpleGraphTab
    return SimpleGraphTab


def _advanced():
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    return AdvancedGraphTab


def _dirty_graph(tab):
    """Честная правка оператора через стек команд.

    Жест у редакторов разный (в «Ручной правке» — протяжка узла, в простом —
    снос узла), общий у них результат: команда в `undo_mgr`, поднявшая
    `revision`, — то самое, что вкладка считает дёрти-флагом
    (`base_graph_tab.py:1170-1174`).
    """
    assert tab.has_unsaved_changes() is False, "вкладка грязная до правки"
    ed = tab._editor
    if hasattr(ed, "start_drag_node"):
        ed.start_drag_node(NID)
        ed.drag_node_to(*DRAG_TO)
        ed.end_drag_node()
    else:
        assert ed.delete_node(NID), f"узел {NID} не снесён — фикстура сдвинулась"
    assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"


GRAPH_TABS = pytest.mark.parametrize(
    "factory", [_simple, _advanced], ids=["simple", "advanced"])


def _unread_graph_api(graph_blobs, cls):
    """Сервер, у которого сохранённый граф НЕ отдался (не 404)."""
    artifact = "graph_canvas" if cls.USE_CANVAS else "graph_validated"
    api = GraphAPI(graph_blobs,
                   failures={artifact: APIError("gateway timeout", 504)})
    return api, artifact


# ── дверь 1: вопрос о слепой перезаписи ──────────────────────────────────

@GRAPH_TABS
def test_graph_tick_never_asks_about_blind_overwrite(
        factory, open_graph_tab, graph_blobs, dialogs):
    """Тик не спрашивает, не пишет вслепую и НЕ СНИМАЕТ запрет.

    Три утверждения вместе, потому что порознь каждое лечится неправильно:
    «не спрашивает» — разрешением записи, «не пишет» — молчаливым отказом
    без следа, «запрет цел» — отсутствием самой записи.
    """
    cls = factory()
    api, artifact = _unread_graph_api(graph_blobs, cls)
    tab = open_graph_tab(cls, api)
    assert artifact in tab._unreadable_on_server, "запрет не взведён — стенд пуст"
    _dirty_graph(tab)

    _tick(tab)

    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert api.uploads == [], f"слепая запись прошла по таймеру: {api.uploads}"
    assert artifact in tab._unreadable_on_server, (
        "автосохранение сняло запрет 1-33 без оператора")
    assert "вручную" in _line(tab), (
        f"строка не объясняет отказ и не зовёт сохранить вручную: {_line(tab)!r}")


@GRAPH_TABS
def test_graph_manual_save_still_asks_after_a_silent_tick(
        factory, open_graph_tab, graph_blobs, dialogs):
    """Ручное сохранение ПОСЛЕ молчаливого тика по-прежнему спрашивает.

    ⛔ Прогон идёт ПОСЛЕ чужого действия (`PROTOCOL §3`): у вкладки есть
    предыстория — один отказавшийся тик. Ответ «Да» обязан пройти
    и запрет снять: режим отнимает вопрос у таймера, а не у оператора.
    """
    cls = factory()
    api, artifact = _unread_graph_api(graph_blobs, cls)
    tab = open_graph_tab(cls, api)
    _dirty_graph(tab)
    _tick(tab)
    del dialogs[:]

    assert tab._save_graph() is True, "ручное сохранение не прошло"

    assert _titles(dialogs) == ["Сохранение затрёт серверную копию"], (
        f"ручное сохранение потеряло вопрос: {dialogs}")
    assert api.uploads == [artifact], f"запись не прошла: {api.uploads}"
    assert artifact not in tab._unreadable_on_server, (
        "«Да» оператора не сняло запрет")


# ── дверь 2: предупреждение об отказе записи ─────────────────────────────

@GRAPH_TABS
def test_graph_tick_reports_a_refused_upload_by_a_line(
        factory, open_graph_tab, graph_blobs, dialogs):
    """Отказ сервера на тике: модалки нет, причина — строкой."""
    cls = factory()
    api = GraphAPI(graph_blobs)
    tab = open_graph_tab(cls, api)
    _dirty_graph(tab)
    api.refuse_upload = APIError("сервер отказал", 500)

    _tick(tab)

    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert "сервер отказал" in _line(tab), (
        f"строка не называет причину отказа: {_line(tab)!r}")


@GRAPH_TABS
def test_graph_manual_save_still_opens_a_modal_on_refusal(
        factory, open_graph_tab, graph_blobs, dialogs):
    """Обратная граница: у оператора модалка на отказе остаётся."""
    cls = factory()
    api = GraphAPI(graph_blobs)
    tab = open_graph_tab(cls, api)
    _dirty_graph(tab)
    api.refuse_upload = APIError("сервер отказал", 500)

    assert tab._save_graph() is False

    assert _titles(dialogs) == ["Ошибка"], (
        f"ручное сохранение промолчало об отказе: {dialogs}")


# ── дверь 3: контуры `ContourTab` ────────────────────────────────────────

@pytest.fixture
def contour_tab(open_graph_tab, graph_blobs, contours_auto):
    """Вкладка контуров с настоящими контурами и применённым контуром."""
    from ui.tabs.contour_tab import ContourTab

    api = GraphAPI(graph_blobs, contours=contours_auto)
    tab = open_graph_tab(ContourTab, api)
    assert tab.has_unsaved_changes() is False, "вкладка грязная до правки"
    tab._apply_all_contours()            # жест оператора: «Применить все»
    assert tab.has_unsaved_changes() is True, (
        "применение контуров не подняло дёрти-флаг — стенд не собрался")
    return tab, api


@pytest.fixture
def contours_refused(monkeypatch):
    """Заливка `contours_validated` отказывает — граф при этом уходит."""
    from ui.tabs.contour_tab import ContourTab

    def refuse(self, *a, **kw):
        raise APIError("контуры не записаны", 500)

    monkeypatch.setattr(ContourTab, "_save_contours_validated", refuse)


def test_contour_tick_never_opens_the_contours_modal(
        contour_tab, contours_refused, dialogs):
    """Граф записан, контуры — нет: на тике об этом говорит строка, не модалка."""
    tab, api = contour_tab

    _tick(tab)

    assert "graph_validated" in api.uploads, "граф не записан — сверять нечего"
    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert "онтур" in _line(tab), (
        f"потеря контуров по таймеру не названа строкой: {_line(tab)!r}")


def test_contour_manual_save_still_opens_the_contours_modal(
        contour_tab, contours_refused, dialogs):
    """Обратная граница той же двери.

    ⛔ Возврат стал `False` (пункт 1-41): половина записи — не сохранение,
    и дёрти-флаг обязан остаться поднятым, иначе вкладка закроется без
    вопроса и подтверждения контуров исчезнут. Заголовок модалки при этом
    называет ПОТЕРЯННУЮ половину, а не «Ошибка» вообще.
    """
    tab, _api = contour_tab

    assert tab._save_graph() is False
    assert tab.has_unsaved_changes() is True, (
        "дёрти-флаг погашен графовой половиной — уход будет молчаливым")

    assert _titles(dialogs) == ["Контуры не сохранены"], (
        f"ручное сохранение промолчало о потере контуров: {dialogs}")


# ══════════════════════════════════════════════════════════════════════════
# ВКЛАДКИ МАСОК: перекрёстки и трубы
# ══════════════════════════════════════════════════════════════════════════

class MaskAPI:
    """Подставной сервер вкладок масок: что скачали и что залили."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.saves = []
        self.refuse = {}

    def download_artifact(self, uid, artifact_type, dest_path):
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def upload_validated_mask(self, uid, mask_type, file_path):
        exc = self.refuse.get(mask_type)
        if exc is not None:
            raise exc
        self.blobs[mask_type] = Path(file_path).read_bytes()
        self.saves.append(mask_type)
        return {}

    def upload_updated_nodes(self, uid, path):
        self.saves.append("coco_validated")
        return {}


@pytest.fixture(scope="module")
def mask_blobs(qapp, tmp_path_factory):
    root = tmp_path_factory.mktemp("mask_rasters")

    def _png(img, name):
        path = root / name
        assert img.save(str(path))
        return path.read_bytes()

    original = QImage(W, H, QImage.Format.Format_RGB32)
    original.fill(QColor("white"))

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
        p.setPen(QPen(QColor("white"), 3, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(40, 200, 360, 200)
        p.end()
        return img

    return {
        "original_image": _png(original, "original.png"),
        "junction_mask": _png(squares([(120, 200)]), "junction.png"),
        "bridge_mask": _png(squares(BRIDGE_SQUARES), "bridge.png"),
        "skeleton": _png(lines(), "skeleton.png"),
        "pipe_mask": _png(lines(), "pipe.png"),
        "points": json.dumps(POINTS_SAVED).encode(),
        "coco": json.dumps({"annotations": []}).encode(),
    }


def _download_for(kind, api, tmp_dir):
    """Прогнать НАСТОЯЩИЙ список заданий вкладки синхронно: (artifacts, error)."""
    from ui.services.artifact_downloader import ArtifactDownloader

    if kind == "junction":
        from ui.tabs.junction_tab import _ARTIFACTS as jobs
    else:
        from ui.tabs.pipe_tab import _ARTIFACTS as jobs

    dl = ArtifactDownloader(api, MASK_UID, tmp_dir, jobs)
    out = {}
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    return out.get("artifacts"), out.get("error")


@pytest.fixture
def junction_tab(qapp, monkeypatch, dialogs, mask_blobs):
    """Вкладка перекрёстков с сохранённой работой оператора на сервере."""
    from ui.tabs.junction_tab import JunctionTab

    api = MaskAPI({
        "original_image": mask_blobs["original_image"],
        "junction_mask_validated": mask_blobs["junction_mask"],
        "bridge_mask_validated": mask_blobs["bridge_mask"],
        "skeleton_final": mask_blobs["skeleton"],
        "coco_validated": mask_blobs["coco"],
        "junction_points_validated": mask_blobs["points"],
    })
    monkeypatch.setattr(
        JunctionTab, "_download_artifacts",
        lambda self: setattr(self, "_download_thread", QThread(self)))
    tab = JunctionTab(MASK_UID, "проба 1-38", api)
    artifacts, error = _download_for("junction", api, tab.temp_dir)
    assert error is None, f"загрузчик увёл вкладку в ошибку: {error}"
    tab._on_downloaded(artifacts)
    del dialogs[:]

    assert tab.has_unsaved_changes() is False, "вкладка грязная до правки"
    tab._editor.add_square(260, 200)      # жест оператора: новый квадрат
    assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"

    yield tab, api
    tab.deleteLater()
    qapp.processEvents()


@pytest.fixture
def pipe_tab(qapp, monkeypatch, dialogs, mask_blobs):
    """Вкладка труб с сохранённой маской оператора на сервере."""
    from ui.tabs.pipe_tab import PipeTab

    api = MaskAPI({
        "original_image": mask_blobs["original_image"],
        "pipe_mask_validated": mask_blobs["skeleton"],
        "coco_validated": mask_blobs["coco"],
        "pipe_mask": mask_blobs["pipe_mask"],
    })
    monkeypatch.setattr(
        PipeTab, "_download_artifacts",
        lambda self: setattr(self, "_download_thread", QThread(self)))
    tab = PipeTab(MASK_UID, "проба 1-38", api)
    artifacts, error = _download_for("pipe", api, tab.temp_dir)
    assert error is None, f"загрузчик увёл вкладку в ошибку: {error}"
    tab._on_downloaded(artifacts)
    del dialogs[:]

    assert tab.has_unsaved_changes() is False, "вкладка грязная до правки"
    tab._editor._erase_at(QPointF(200, 200))   # жест оператора: ластик
    assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"

    yield tab, api
    tab.deleteLater()
    qapp.processEvents()


# ── дверь 4: отказ записи масок перекрёстков ─────────────────────────────

def test_junction_tick_reports_a_refused_mask_by_a_line(junction_tab, dialogs):
    tab, api = junction_tab
    api.refuse["junction_mask_validated"] = APIError("сервер отказал", 500)

    _tick(tab)

    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert "сервер отказал" in _line(tab), (
        f"строка не называет причину отказа: {_line(tab)!r}")


def test_junction_manual_save_still_opens_a_modal_on_refusal(junction_tab,
                                                             dialogs):
    tab, api = junction_tab
    api.refuse["junction_mask_validated"] = APIError("сервер отказал", 500)

    assert tab._save_masks() is False

    assert _titles(dialogs) == ["Ошибка"], (
        f"ручное сохранение промолчало об отказе: {dialogs}")


# ── дверь 5: отказ записи центров перекрёстков (1-39) ────────────────────

def test_junction_tick_reports_lost_points_by_a_line(junction_tab, dialogs):
    """Маски записаны, центры — нет: строкой, а не модалкой посреди работы."""
    tab, api = junction_tab
    api.refuse["junction_points_validated"] = APIError("сервер отказал", 500)

    _tick(tab)

    assert "junction_mask_validated" in api.saves, "маски не записаны"
    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert "ентр" in _line(tab), (
        f"потеря центров по таймеру не названа строкой: {_line(tab)!r}")


def test_junction_manual_save_still_opens_the_points_modal(junction_tab,
                                                           dialogs):
    """Обратная граница: у оператора отдельная модалка про центры остаётся."""
    tab, api = junction_tab
    api.refuse["junction_points_validated"] = APIError("сервер отказал", 500)

    assert tab._save_masks() is False

    assert _titles(dialogs) == ["Центры не сохранены"], (
        f"ручное сохранение промолчало о центрах: {dialogs}")


# ── дверь 6: отказ записи маски труб ─────────────────────────────────────

def test_pipe_tick_reports_a_refused_mask_by_a_line(pipe_tab, dialogs):
    tab, api = pipe_tab
    api.refuse["pipe_mask_validated"] = APIError("сервер отказал", 500)

    _tick(tab)

    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert "сервер отказал" in _line(tab), (
        f"строка не называет причину отказа: {_line(tab)!r}")


def test_pipe_manual_save_still_opens_a_modal_on_refusal(pipe_tab, dialogs):
    tab, api = pipe_tab
    api.refuse["pipe_mask_validated"] = APIError("сервер отказал", 500)

    assert tab._save_mask() is False

    assert _titles(dialogs) == ["Ошибка"], (
        f"ручное сохранение промолчало об отказе: {dialogs}")


# ══════════════════════════════════════════════════════════════════════════
# ВКЛАДКА ПРИВЯЗОК OCR
# ══════════════════════════════════════════════════════════════════════════

def _bind_graph(n_nodes: int) -> dict:
    nodes = []
    for i in range(n_nodes):
        x = 60.0 + 100 * i
        nodes.append({"id": f"n{i}", "type": "equipment",
                      "class_name": "zadvizhka", "centroid": [80.0, x],
                      "bbox": [x - 12, 68, x + 12, 92]})
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [H, W]},
            "nodes": nodes, "links": [], "text_blocks": [], "bindings": []}


_BIND_BLOCKS = [
    {"bbox": [48.0, 30.0, 112.0, 52.0], "text": "10LBA10", "confidence": 0.98,
     "source": "ocr", "origin": "ocr"},
    {"bbox": [148.0, 30.0, 212.0, 52.0], "text": "DN100", "confidence": 0.95,
     "source": "ocr", "origin": "ocr"},
]
BINDING_SAVED = json.dumps({
    "version": 2,
    "bindings": [
        {"ocr_block_idx": i, "text": b["text"], "bbox": b["bbox"],
         "node_id": f"n{i}"}
        for i, b in enumerate(_BIND_BLOCKS)
    ],
    "edited_blocks": _BIND_BLOCKS,
}).encode()


class BindAPI:
    """Подставной сервер вкладки привязок: пишет туда же, откуда читает."""

    def __init__(self, blobs, failures=None):
        self.blobs = dict(blobs)
        self.failures = dict(failures or {})
        self.saves = []
        self.refuse_save = None

    def _put(self, name, dest_path):
        exc = self.failures.get(name)
        if exc is not None:
            raise exc
        data = self.blobs.get(name)
        if data is None:
            raise APIError(f"artifact {name} not found", 404)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return dest_path

    def download_artifact(self, uid, artifact_type, dest_path):
        return self._put(artifact_type, dest_path)

    def download_ocr_result(self, uid, dest_path):
        return self._put("ocr_result", dest_path)

    def download_ocr_binding(self, uid, dest_path):
        return self._put("ocr_binding", dest_path)

    def download_ocr_validation(self, uid, dest_path):
        return self._put("ocr_validation", dest_path)

    def _store(self, name, path):
        if self.refuse_save is not None:
            raise self.refuse_save
        self.blobs[name] = Path(path).read_bytes()
        self.saves.append(name)
        return {}

    def save_ocr_binding(self, uid, path):
        return self._store("ocr_binding", path)

    def save_ocr_validation(self, uid, path):
        return self._store("ocr_validation", path)

    def upload_validated_graph(self, uid, path):
        return self._store("graph_validated", path)


@pytest.fixture(scope="module")
def bind_raster(qapp, tmp_path_factory):
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("bind_raster") / "original.png"
    assert img.save(str(png))
    return png.read_bytes()


@pytest.fixture
def open_bind_tab(qapp, monkeypatch, dialogs, bind_raster):
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.ocr_binding_tab import OcrBindingTab, _ARTIFACTS

    opened = []

    def _open(failures=None):
        api = BindAPI({
            "original_image": bind_raster,
            "ocr_result": json.dumps({"target": [], "secondary": []}).encode(),
            "graph_validated": json.dumps(_bind_graph(N_SAVED)).encode(),
            "graph_json": json.dumps(_bind_graph(2)).encode(),
            "ocr_binding": BINDING_SAVED,
        }, failures)
        monkeypatch.setattr(
            OcrBindingTab, "_start_download",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = OcrBindingTab(BIND_UID, "проба 1-38", api)
        opened.append(tab)

        dl = ArtifactDownloader(api, BIND_UID, tab.temp_dir, _ARTIFACTS)
        out = {}
        dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
        dl.error.connect(lambda m: out.__setitem__("error", m))
        dl.run()
        assert out.get("error") is None, f"загрузчик увёл вкладку в ошибку: {out}"
        tab._on_download_finished(out["artifacts"])
        del dialogs[:]

        assert tab.has_unsaved_changes() is False, "вкладка грязная до правки"
        # Жест оператора: правка привязки в редакторе — вкладка узнаёт о ней
        # тем же слотом, которым её приносит сигнал редактора.
        tab._on_binding_changed()
        assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"
        return tab, api

    yield _open
    for tab in opened:
        tab.cleanup()
        tab.deleteLater()
    qapp.processEvents()


def _bindings_on_server(api) -> int:
    return len(json.loads(api.blobs["ocr_binding"].decode())["bindings"])


# ── дверь 7: вопрос о слепой перезаписи у вкладки привязок ───────────────

def test_binding_tick_never_asks_about_blind_overwrite(open_bind_tab, dialogs):
    """Тик не спрашивает, не пишет вслепую и НЕ СНИМАЕТ запрет 1-39."""
    tab, api = open_bind_tab(
        {"ocr_binding": APIError("gateway timeout", 504)})
    assert "ocr_binding" in tab._unreadable_on_server, "запрет не взведён"
    assert _bindings_on_server(api) == N_BINDINGS, "стенд начал не с работы оператора"

    _tick(tab)

    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert api.saves == [], f"слепая запись прошла по таймеру: {api.saves}"
    assert _bindings_on_server(api) == N_BINDINGS, (
        "работа оператора на сервере затёрта автосохранением")
    assert "ocr_binding" in tab._unreadable_on_server, (
        "автосохранение сняло запрет 1-39 без оператора")
    assert "вручную" in _line(tab), (
        f"строка не объясняет отказ и не зовёт сохранить вручную: {_line(tab)!r}")


def test_binding_manual_save_still_asks_after_a_silent_tick(open_bind_tab,
                                                            dialogs):
    """Ручное сохранение после молчаливого тика спрашивает и проходит."""
    tab, api = open_bind_tab(
        {"ocr_binding": APIError("gateway timeout", 504)})
    _tick(tab)
    del dialogs[:]

    assert tab._save_binding() is True, "ручное сохранение не прошло"

    assert _titles(dialogs) == ["Сохранение затрёт серверную копию"], (
        f"ручное сохранение потеряло вопрос: {dialogs}")
    assert "ocr_binding" in api.saves, "запись не прошла"
    assert "ocr_binding" not in tab._unreadable_on_server, (
        "«Да» оператора не сняло запрет")


# ── дверь 8: отказ записи привязок ───────────────────────────────────────

def test_binding_tick_reports_a_refused_save_by_a_line(open_bind_tab, dialogs):
    tab, api = open_bind_tab()
    api.refuse_save = APIError("сервер отказал", 500)

    _tick(tab)

    assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
    assert "сервер отказал" in _line(tab), (
        f"строка не называет причину отказа: {_line(tab)!r}")


def test_binding_manual_save_still_opens_a_modal_on_refusal(open_bind_tab,
                                                            dialogs):
    tab, api = open_bind_tab()
    api.refuse_save = APIError("сервер отказал", 500)

    assert tab._save_binding() is False

    assert _titles(dialogs) == ["Ошибка"], (
        f"ручное сохранение промолчало об отказе: {dialogs}")
