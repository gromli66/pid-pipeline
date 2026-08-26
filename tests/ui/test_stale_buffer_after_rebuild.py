# -*- coding: utf-8 -*-
"""Блок 5 «точечных болей» (2026-08-25) — вкладка, пережившая пересборку графа.

Зачем. Пока граф пересобирается, сервер отбивает записи фазы B гейтом
(`app/api/build_gate.py`). Но клиенту от одного 400 толку мало: канала «узнать
о смене статуса» у открытой вкладки НЕТ — поллинг на время вкладки заглушён
(`diagram_workspace.py`), — а автосохранение молча ретраит каждые 120 с. Сборка
идёт ~344 с (замер 1-23), то есть хотя бы один тик попадёт в гейт; а как только
статус минует гейт, СЛЕДУЮЩИЙ тик зальёт СТАРОЕ поколение графа поверх нового.
Это окно «после BUILT», и стоит оно оператору всей работы новой сборки.

Полное закрытие окна — штамп поколения в самом графе — кандидат в дорогу.
Здесь дешёвый заслон, и он же требование блока: отбитый гейтом сейв показывает
ЧЕСТНУЮ ошибку и помечает буфер несвежим — автосохранение останавливается,
оператору висит баннер «граф пересобран, перезагрузите вкладку».

⛔ Признак отказа МАШИННЫЙ (`REBUILD_REFUSAL` в `detail`), а не «сервер ответил
400»: у соседних отказов — «OCR result not available yet», слепая перезапись,
битый JSON — правки оператора живы и лечатся повтором. Морозить буфер там
значило бы отнимать работу вместо того, чтобы её спасать. Обе полярности
проверяются здесь, иначе заслон лечился бы «морозить на любом 400».

⛔ Отказ берётся у НАСТОЯЩИХ корутин сервера, а не пишется строкой: разъедься
признак между сторонами, стенд с рукописной строкой остался бы зелёным
(образец — `tests/ui/test_phase_b_free_entry.py`).

⚠ Три записи, куда клиент фазы B приходит ПЕРВЫМИ, — `/validation/graph/save`,
`/validation/graph/canvas/save`, `/ocr/binding/save`. Гейты `contours`/`ocr`
из перечня блока лежат ЗА ними, и без признака на этих трёх заслон висел бы
на пути, которого клиент не потребляет (`PROTOCOL`: «вопрос снят» проверяется
на каждом ПОТРЕБИТЕЛЕ инварианта).

⚠ Тик подаётся ДЕТЕРМИНИРОВАННО — сигналом таймера, а не ожиданием по стенным
часам (замер 1-39). ⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова,
а не таймаутом (`PROTOCOL §5`). ⚠ Виджеты сносятся детерминированно (1-32).
"""
import asyncio
import json
import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                     # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                          # noqa: E402

from PySide6.QtCore import QThread                                # noqa: E402
from PySide6.QtGui import QColor, QImage                          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox           # noqa: E402
from fastapi import HTTPException                                 # noqa: E402

import app.services.storage as storage_mod                        # noqa: E402
from app.api.ocr import save_ocr_binding                          # noqa: E402
from app.api.validation import (                                  # noqa: E402
    save_canvas_graph,
    save_validated_graph,
)
from app.models import Artifact, ArtifactType, Diagram            # noqa: E402
from app.models import DiagramStatus as SrvStatus                 # noqa: E402
from tools import corpus                                          # noqa: E402
from ui.services.api_client import APIError                       # noqa: E402
from ui.services.autosave import AutoSaveService                  # noqa: E402
from ui.services.ui_settings import UISettings                    # noqa: E402
from ui.tabs.save_mode import REBUILD_REFUSAL                     # noqa: E402

GRAPH_UID = "d74eb9f1"        # корпус-фикстура в git (пункт 0.8)
BIND_UID = "1c38bb02"
NID = "node_11"               # узел корпусной схемы для честной правки
DRAG_TO = (500.0, 400.0)
W, H = 400, 300

#: статус, в котором граф пересобирается
REBUILDING = SrvStatus.BUILDING_GRAPH


# ── настоящий отказ сервера ──────────────────────────────────────────────

class _Res:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _SrvDB:
    """Поверхность `AsyncSession` трёх записей фазы B."""

    def __init__(self, diagram, artifacts):
        self.diagram = diagram
        self.artifacts = artifacts

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _Res(self.diagram)
        params = stmt.compile().params
        return _Res(self.artifacts.get(params.get("artifact_type_1")))

    async def commit(self):
        pass

    async def flush(self):
        pass

    def add(self, obj):
        pass

    async def delete(self, obj):
        pass


class _Upload:
    async def read(self):
        return b'{"nodes": [], "edges": []}'


def _art(path):
    a = Artifact()
    a.file_path = path
    a.file_size = 1
    a.mime_type = "application/json"
    return a


def _srv_error(coro_factory, status) -> APIError:
    """Отказ НАСТОЯЩЕЙ корутины сервера, переведённый в клиентский `APIError`.

    Ровно так его видит вкладка: `ui/services/api_client.py` кладёт `detail`
    в `message`, а код — в `status_code`.
    """
    uid = uuid.UUID("d74eb9f1-1111-2222-3333-444455556666")
    diagram = Diagram()
    diagram.uid = uid
    diagram.status = status
    diagram.error_stage = None
    diagram.error_message = None
    diagram.project_code = "thermohydraulics"
    db = _SrvDB(diagram, {
        ArtifactType.GRAPH_VALIDATED: _art("graph/graph_validated.json"),
        ArtifactType.OCR_RESULT: _art("ocr/ocr_result.json"),
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro_factory(uid, db))
    assert exc.value.status_code == 400
    return APIError(str(exc.value.detail), 400)


_COROS = {
    "graph_validated": lambda uid, db: save_validated_graph(
        uid, file=_Upload(), db=db),
    "graph_canvas": lambda uid, db: save_canvas_graph(
        uid, file=_Upload(), db=db),
    "ocr_binding": lambda uid, db: save_ocr_binding(
        uid, file=_Upload(), db=db),
}


@pytest.fixture(scope="module")
def refusal(tmp_path_factory):
    """Отказ гейта пересборки по каждой из трёх записей — с признаком."""
    storage_mod.settings.STORAGE_PATH = str(tmp_path_factory.mktemp("srv"))
    out = {k: _srv_error(f, REBUILDING) for k, f in _COROS.items()}
    for key, exc in out.items():
        assert REBUILD_REFUSAL in exc.message, (
            f"{key}: гейт пересборки не пометил отказ признаком")
    return out


@pytest.fixture(scope="module")
def plain_refusal(tmp_path_factory):
    """Соседний 400 БЕЗ признака: статус вне списка, но пересборки нет.

    `extracting_contours` — законный статус фазы B, `/graph/save` его просто
    не знает (пункт 3.1в списка не расширял). Правки оператора при таком
    отказе живы, и буфер морозить нельзя.
    """
    storage_mod.settings.STORAGE_PATH = str(tmp_path_factory.mktemp("srv2"))
    exc = _srv_error(_COROS["graph_validated"], SrvStatus.EXTRACTING_CONTOURS)
    assert REBUILD_REFUSAL not in exc.message, "соседний 400 несёт чужой признак"
    return exc


# ── обстановка клиента ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки → список (title, text). Пустой список = вкладка молчала."""
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


class GraphAPI:
    """Подставной сервер графовой вкладки: что скачали и что залили."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.uploads = []
        self.refuse_upload = None

    def download_artifact(self, uid, artifact_type, dest_path):
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

    def upload_contours_training(self, uid, path):
        return self._upload("contours_training", path)

    def upload_contours_validated(self, uid, path):
        return self._upload("contours_validated", path)

    def download_contours_auto(self, uid, dest):
        raise APIError(f"contours_auto not found for {uid}", 404)


def _canvas_blob(graph: bytes, png, tmp_path_factory) -> bytes:
    """Холст «Ручной правки» — тем же кодом, каким его собирал сам клиент.

    ⚠ mefx-8: вкладка в холстовом режиме больше не пересобирает холст сама
    (`canvas_verdict` → экран «холст не готов»), поэтому набору нужен готовый
    и СВЕЖИЙ холст: метка источника считается от того же графа. Содержимое
    редактора при этом ровно то же, что видели прежние редакции набора, —
    прежде его собирал фолбэк, теперь фикстура.
    """
    from ui.tabs.base_graph_tab import _pretransform_to_canvas

    root = tmp_path_factory.mktemp("graph_canvas")
    src = root / "graph.json"
    src.write_bytes(graph)
    out = root / "graph_canvas.json"
    assert _pretransform_to_canvas(src, png, out), "холст не собрался"
    return out.read_bytes()


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
            "graph_validated": graph,
            "graph_canvas": _canvas_blob(graph, png, tmp_path_factory)}


@pytest.fixture
def open_graph_tab(qapp, monkeypatch, dialogs, graph_blobs):
    """Вкладка графа, собранная тем же слотом `_on_downloaded`, что в бою."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.base_graph_tab import BaseGraphTab, _graph_jobs

    opened = []

    def _open(cls, dirty=True):
        api = GraphAPI(graph_blobs)
        monkeypatch.setattr(
            BaseGraphTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = cls(GRAPH_UID, "проба блока 5", api)
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

        if not dirty:
            return tab, api

        # Честная правка оператора через стек команд.
        ed = tab._editor
        if hasattr(ed, "start_drag_node"):
            ed.start_drag_node(NID)
            ed.drag_node_to(*DRAG_TO)
            ed.end_drag_node()
        else:
            assert ed.delete_node(NID), f"узел {NID} не снесён — фикстура уехала"
        assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"
        return tab, api

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


GRAPH_TABS = pytest.mark.parametrize(
    "factory", [_simple, _advanced], ids=["simple", "advanced"])


def _artifact_of(cls) -> str:
    return "graph_canvas" if cls.USE_CANVAS else "graph_validated"


def _tick(tab):
    """Тик автосохранения по живой вкладке — тем же кодом, что в бою.

    Сервис ВОЗВРАЩАЕТСЯ: остановка таймера — наблюдаемое следствие правки,
    и судить её надо на том же объекте, который тикал.
    """
    service = AutoSaveService()
    service.start(tab)
    assert service._timer.isActive(), "start() не завёл таймер автосохранения"
    service._timer.timeout.emit()
    return service


def _line(tab) -> str:
    return tab.status_label.text()


# ══════════════════════════════════════════════════════════════════════════
# ГРАФОВЫЕ ВКЛАДКИ: «Проверка схемы» и «Ручная правка»
# ══════════════════════════════════════════════════════════════════════════

@GRAPH_TABS
def test_manual_save_reports_the_rebuild_and_freezes_the_buffer(
        factory, open_graph_tab, refusal, dialogs):
    """Ручное сохранение: честная модалка + буфер помечен несвежим.

    Три утверждения вместе, потому что порознь каждое лечится неправильно:
    «сказал» — любой модалкой, «пометил» — без слов оператору, «не записал» —
    отказом сервера, который и так был.
    """
    cls = factory()
    tab, api = open_graph_tab(cls)
    api.refuse_upload = refusal[_artifact_of(cls)]

    assert tab._save_graph() is False

    assert [t for t, _ in dialogs] == ["Схема пересобрана"], (
        f"оператору не сказали, что схему пересобрали: {dialogs}")
    assert tab.save_blocked_reason, "буфер не помечен несвежим"
    assert "перезагруз" in _line(tab).lower() or "закройте" in _line(tab).lower(), (
        f"баннер не зовёт перезагрузить вкладку: {_line(tab)!r}")
    assert api.uploads == [], f"запись всё-таки прошла: {api.uploads}"


@GRAPH_TABS
def test_a_frozen_buffer_does_not_reach_the_server_again(
        factory, open_graph_tab, refusal, dialogs):
    """Второй сейв после заморозки до сервера НЕ доходит.

    ⛔ Прогон идёт ПОСЛЕ чужого действия (`PROTOCOL §3`): у вкладки есть
    предыстория — один отбитый сейв. Утверждается РАЗНИЦА: гейт снят, сервер
    принял бы запись, и без метки она бы прошла — а не проходит.
    """
    cls = factory()
    tab, api = open_graph_tab(cls)
    api.refuse_upload = refusal[_artifact_of(cls)]
    tab._save_graph()
    del dialogs[:]

    api.refuse_upload = None          # сборка кончилась, гейт открылся
    assert tab._save_graph() is False

    assert api.uploads == [], (
        "мёртвый буфер залил старое поколение графа поверх нового")
    assert dialogs == [], f"второй отказ поднял модалку: {dialogs}"
    assert tab.save_blocked_reason in _line(tab)


@GRAPH_TABS
def test_a_neighbour_400_leaves_the_buffer_alive(
        factory, open_graph_tab, plain_refusal, dialogs):
    """Обратная полярность: 400 БЕЗ признака буфер не морозит.

    Без этой клетки заслон лечился бы «морозить на любом 400» — и вкладка
    умирала бы от отказа, который лечится повтором.
    """
    cls = factory()
    tab, api = open_graph_tab(cls)
    api.refuse_upload = plain_refusal

    assert tab._save_graph() is False
    assert tab.save_blocked_reason == "", "чужой 400 заморозил буфер"

    api.refuse_upload = None
    assert tab._save_graph() is True, "буфер не ожил после устранимого отказа"
    assert api.uploads == [_artifact_of(cls)]


@GRAPH_TABS
def test_autosave_stops_after_the_rebuild_refusal(
        factory, open_graph_tab, refusal, dialogs):
    """Автосохранение: молча по таймеру, строкой оператору — и ОСТАНАВЛИВАЕТСЯ.

    Ретрай тика и есть окно «после BUILT»: сборка идёт минутами, тик — раз
    в 120 с, и следующий за открытием гейта тик заливает старое поколение.
    """
    cls = factory()
    tab, api = open_graph_tab(cls)
    api.refuse_upload = refusal[_artifact_of(cls)]

    service = _tick(tab)
    try:
        assert dialogs == [], f"тик автосохранения поднял модалку: {dialogs}"
        assert tab.save_blocked_reason, "тик не пометил буфер несвежим"
        assert not service._timer.isActive(), (
            "автосохранение продолжает тикать по мёртвому буферу")
        assert tab.save_blocked_reason in _line(tab), (
            f"баннера нет в строке вкладки: {_line(tab)!r}")

        # Гейт открылся — а тикать уже нечему: запись не повторится.
        api.refuse_upload = None
        service._timer.timeout.emit()
        assert api.uploads == [], (
            "остановленное автосохранение всё-таки залило старый граф")
    finally:
        service.stop()
        service.deleteLater()


@GRAPH_TABS
def test_autosave_survives_a_neighbour_400(
        factory, open_graph_tab, plain_refusal, dialogs):
    """Порог заперт с другой стороны: устранимый отказ автосейв не глушит."""
    cls = factory()
    tab, api = open_graph_tab(cls)
    api.refuse_upload = plain_refusal

    service = _tick(tab)
    try:
        assert service._timer.isActive(), "автосохранение сдалось на чужом 400"
        api.refuse_upload = None
        service._timer.timeout.emit()
        assert api.uploads == [_artifact_of(cls)], (
            "следующий тик не дописал работу оператора")
    finally:
        service.stop()
        service.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# ВКЛАДКА ПРИВЯЗКИ: у неё ТРИ записи, и первая — `/ocr/binding/save`
# ══════════════════════════════════════════════════════════════════════════

BINDING_GRAPH = {
    "graph": {"image_size": [H, W]},
    "nodes": [{"id": "node_1", "class_name": "valve", "centroid": [100, 100],
               "bbox": [90, 90, 110, 110]}],
    "links": [],
}


class BindAPI:
    """Подставной сервер вкладки привязки: три записи и что до них дошло."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.saves = []
        self.refuse_save = None

    def _get(self, name, dest_path):
        data = self.blobs.get(name)
        if data is None:
            raise APIError(f"artifact {name} not found", 404)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return dest_path

    def download_artifact(self, uid, artifact_type, dest_path):
        return self._get(artifact_type, dest_path)

    def download_ocr_result(self, uid, dest_path):
        return self._get("ocr_result", dest_path)

    def download_ocr_binding(self, uid, dest_path):
        return self._get("ocr_binding", dest_path)

    def download_ocr_validation(self, uid, dest_path):
        return self._get("ocr_validation", dest_path)

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


@pytest.fixture
def open_bind_tab(qapp, monkeypatch, dialogs):
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.ocr_binding_tab import OcrBindingTab, _ARTIFACTS

    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    opened = []

    def _open(tmp_path):
        png = tmp_path / "original.png"
        assert img.save(str(png))
        api = BindAPI({
            "original_image": png.read_bytes(),
            "ocr_result": json.dumps({"target": [], "secondary": []}).encode(),
            "graph_validated": json.dumps(BINDING_GRAPH).encode(),
            "graph_json": json.dumps(BINDING_GRAPH).encode(),
            "ocr_binding": json.dumps({"version": 2, "bindings": [],
                                       "edited_blocks": []}).encode(),
        })
        monkeypatch.setattr(
            OcrBindingTab, "_start_download",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = OcrBindingTab(BIND_UID, "проба блока 5", api)
        opened.append(tab)

        dl = ArtifactDownloader(api, BIND_UID, tab.temp_dir, _ARTIFACTS)
        out = {}
        dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
        dl.error.connect(lambda m: out.__setitem__("error", m))
        dl.run()
        assert out.get("error") is None, f"загрузчик увёл вкладку в ошибку: {out}"
        tab._on_download_finished(out["artifacts"])
        del dialogs[:]

        tab._on_binding_changed()
        assert tab.has_unsaved_changes() is True, "правка не подняла дёрти-флаг"
        return tab, api

    yield _open
    for tab in opened:
        tab.cleanup()
        tab.deleteLater()
    qapp.processEvents()


def test_binding_tab_freezes_on_the_rebuild_refusal(
        open_bind_tab, refusal, dialogs, tmp_path):
    """Привязка: отказ ПЕРВОЙ из трёх записей морозит буфер вкладки.

    ⚠ Вкладка льёт граф не только привязками: её `_save_binding` отправляет
    ВЕСЬ `graph_validated` через `/graph/save`. Признак стоит на обеих дверях,
    поэтому какая бы из трёх ни отбилась первой — буфер замрёт.
    """
    tab, api = open_bind_tab(tmp_path)
    api.refuse_save = refusal["ocr_binding"]

    assert tab._save_binding() is False

    assert [t for t, _ in dialogs] == ["Схема пересобрана"], (
        f"оператору не сказали про пересборку: {dialogs}")
    assert tab.save_blocked_reason, "буфер не помечен несвежим"
    assert api.saves == [], f"что-то всё-таки записалось: {api.saves}"


def test_binding_tab_autosave_stops_and_never_writes_again(
        open_bind_tab, refusal, dialogs, tmp_path):
    """Тот же берег по таймеру: молчит, останавливается и больше не пишет."""
    tab, api = open_bind_tab(tmp_path)
    api.refuse_save = refusal["ocr_binding"]

    service = _tick(tab)
    try:
        assert dialogs == [], f"тик поднял модалку: {dialogs}"
        assert not service._timer.isActive(), "автосохранение продолжает тикать"
        api.refuse_save = None
        service._timer.timeout.emit()
        assert api.saves == [], "остановленный автосейв всё-таки записал"
    finally:
        service.stop()
        service.deleteLater()


# ══════════════════════════════════════════════════════════════════════════
# ВКЛАДКА КОНТУРОВ: у неё ВТОРАЯ половина записи своя
# ══════════════════════════════════════════════════════════════════════════

def test_contour_tab_freezes_when_only_the_contours_half_is_refused(
        open_graph_tab, monkeypatch, refusal, dialogs):
    """Гонка узкая, но настоящая: сборка стартовала МЕЖДУ двумя записями.

    `ContourTab._save_graph` пишет дважды — граф через `/graph/save`, контуры
    через PUT `/contours/validated`. Обычно гейт отбивает первую, и до второй
    дело не доходит; но статус мог смениться ровно между ними, и тогда отказ
    приходит своим `except` вкладки, а не базовым. Без метки там вкладка звала
    бы «сохраните ещё раз, когда причина устранена» — совет, который стоил бы
    оператору контуров.
    """
    from ui.tabs.contour_tab import ContourTab

    def refuse(self, *a, **kw):
        raise refusal["graph_validated"]

    monkeypatch.setattr(ContourTab, "_save_contours_validated", refuse)
    tab, api = open_graph_tab(ContourTab, dirty=False)

    assert tab._save_graph() is False

    assert [t for t, _ in dialogs] == ["Схема пересобрана"], (
        f"вкладка контуров позвала сохранить ещё раз: {dialogs}")
    assert tab.save_blocked_reason, "буфер контуров не помечен несвежим"
    assert tab.save_blocked_reason in _line(tab)
