# -*- coding: utf-8 -*-
"""Пункт 1-41 дороги — половина записи на вкладке контуров считалась сохранением.

`ContourTab._save_graph` (`ui/tabs/contour_tab.py:407`) пишет ДВА артефакта:
граф (через `BaseGraphTab._save_graph`) и `contours_validated.json`. При отказе
второй половины он показывает модалку «Граф сохранён, но контуры не сохранены»
и **возвращает `True`** (`:428`).

Цена не в модалке, а в том, что происходит ПОСЛЕ неё. Графовая половина уже
погасила дёрти-флаг: `BaseGraphTab._save_graph` при успехе ставит
`_saved_revision = undo_mgr.revision` (`:1300`), а `has_unsaved_changes()`
(`:1189-1193`) считает ровно это. Значит `confirm_discard_active_tab`
(`diagram_workspace.py:1444`) вопроса не задаст — вкладка закроется молча,
и подтверждения контуров, которых нет на сервере, исчезнут без следа в UI.

Политика противоположная и уже принята на соседней вкладке: `JunctionTab`
при непрошедшей записи центров `_saved` НЕ взводит и возвращает `False`
(пункт 1.x14, `junction_tab.py:404-411`) — «маски сохранены, центры — НЕТ».
Половина записи — это не сохранение.

Код древний (Phase 2C), связкой не тронут и сценарием не воспроизводился —
поэтому работа начинается здесь, с воспроизведения.

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом:
модальный диалог в пути инъекции подвешивает набор вместо падения
(`PROTOCOL §5`).
⚠ Поток загрузки не поднимается, и это не слепота вида 1-43: проверяемое
поведение — что вкладка сделала с дёрти-флагом при отказе записи; ни одна
его стадия от хода загрузочного потока не зависит.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import QThread                               # noqa: E402
from PySide6.QtGui import QColor, QImage                         # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

from ui.services.api_client import APIError                      # noqa: E402

UID = "1c41cc01"
W, H = 400, 300

#: узлов в графе; у каждого свой `ann_idx` — по нему контур находит узел
N_NODES = 2

#: контур-квадрат вокруг узла, плоский список [x1,y1,x2,y2,...]
SIDE = 40


def _graph() -> dict:
    nodes = []
    for i in range(N_NODES):
        x = 100.0 + 120 * i
        nodes.append({"id": f"n{i}", "type": "equipment", "ann_idx": i,
                      "class_name": "zadvizhka", "centroid": [150.0, x],
                      "bbox": [x - 12, 138, x + 12, 162],
                      "segmentation": None})
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [H, W]},
            "nodes": nodes, "links": []}


def _square(cx: float, cy: float) -> list:
    h = SIDE / 2
    return [cx - h, cy - h, cx + h, cy - h, cx + h, cy + h, cx - h, cy + h]


def _contours_auto() -> dict:
    return {
        "nodes": [
            {"ann_id": i, "confidence": 0.95,
             "polygon_auto": _square(100.0 + 120 * i, 150.0),
             "status": "auto"}
            for i in range(N_NODES)
        ],
        "stats": {"auto": N_NODES, "manual_review": 0},
    }


GRAPH = json.dumps(_graph()).encode()
CONTOURS_AUTO = json.dumps(_contours_auto()).encode()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def raster(qapp, tmp_path_factory) -> bytes:
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("raster") / "original.png"
    assert img.save(str(png))
    return png.read_bytes()


# ── подставной сервер ────────────────────────────────────────────────────

class FakeAPI:
    """Кладёт записанное туда же, откуда читает: видно, что сделало сохранение."""

    def __init__(self, blobs, failures=None):
        self.blobs = dict(blobs)
        self.failures = dict(failures or {})
        self.saves = []

    def _get(self, name, dest_path):
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

    def _store(self, name, path):
        exc = self.failures.get("upload:" + name)
        if exc is not None:
            raise exc
        self.blobs[name] = Path(path).read_bytes()
        self.saves.append(name)
        return {}

    def download_artifact(self, uid, artifact_type, dest_path):
        return self._get(artifact_type, dest_path)

    def download_contours_auto(self, uid, dest_path):
        return self._get("contours_auto", dest_path)

    def upload_validated_graph(self, uid, path):
        return self._store("graph_validated", path)

    def upload_canvas_graph(self, uid, path):
        return self._store("graph_canvas", path)

    def upload_contours_validated(self, uid, path):
        return self._store("contours_validated", path)

    def upload_contours_training(self, uid, path):
        return self._store("contours_training", path)


def _server(raster, **failures):
    return FakeAPI({
        "original_image": raster,
        "graph_validated": GRAPH,
        "graph_json": GRAPH,
        "contours_auto": CONTOURS_AUTO,
    }, failures)


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки вкладки → список (title, text); ответ настраивается."""

    class _Seen(list):
        answer = QMessageBox.StandardButton.Cancel

    seen = _Seen()

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return seen.answer

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


@pytest.fixture
def open_contours(qapp, monkeypatch):
    """Открыть вкладку контуров тем словарём, который отдал НАСТОЯЩИЙ загрузчик."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.base_graph_tab import BaseGraphTab, _graph_jobs
    from ui.tabs.contour_tab import ContourTab

    opened = []

    def _open(api):
        monkeypatch.setattr(
            BaseGraphTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = ContourTab(UID, "проба 1-41", api)
        opened.append(tab)

        dl = ArtifactDownloader(api, UID, tab.temp_dir,
                                _graph_jobs(want_canvas=ContourTab.USE_CANVAS))
        out = {}
        dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
        dl.error.connect(lambda m: out.__setitem__("error", m))
        dl.run()
        assert "error" not in out, f"загрузчик увёл вкладку в ошибку: {out}"
        tab._on_downloaded(out["artifacts"])
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()


def _approved_on_server(api) -> int:
    """Сколько контуров оператор подтвердил — по этому числу видно ЧЬЯ работа."""
    data = json.loads(api.blobs["contours_validated"].decode())
    return sum(1 for cn in data.get("nodes", [])
               if cn.get("status") == "approved")


def _operator_applies_contours(tab) -> int:
    """Кнопка «Apply All»: подтверждение контуров — работа оператора."""
    before = tab._editor.undo_mgr.revision
    tab._apply_all_contours()
    assert tab._editor.undo_mgr.revision != before, (
        "жест оператора не дошёл до стека отмены — стенд ничего не проверяет")
    return len(tab._editor._applied_nodes)


# =========================================================================
# 1. Контроль честности стенда
# =========================================================================

def test_apply_and_save_reaches_the_server_and_the_reader_sees_it(
        raster, open_contours, dialogs):
    """Экзамен сдаёт РАЗНИЦА: было 0 подтверждённых, стало N (`PROTOCOL §3`)."""
    api = _server(raster)
    tab = open_contours(api)
    assert "contours_validated" not in api.blobs

    applied = _operator_applies_contours(tab)
    assert applied == N_NODES

    assert tab._save_graph() is True
    assert "contours_validated" in api.saves, "запись контуров до сервера не дошла"
    assert _approved_on_server(api) == N_NODES, (
        "запись не видна читателю — на таком стенде «контуры целы» ничего не значит")
    assert tab.has_unsaved_changes() is False, (
        "успешное сохранение обязано гасить дёрти-флаг")


# =========================================================================
# 2. Дефект: половина записи объявлялась сохранением
# =========================================================================

def test_failed_contour_upload_leaves_the_tab_unsaved(
        raster, open_contours, dialogs):
    """Контуры не записались → вкладка НЕ сохранена, и оператора спросят.

    До правки `_save_graph` возвращал `True`, а `_saved_revision` оставался
    поднятым графовой половиной — `has_unsaved_changes()` отвечал `False`,
    и «← Назад» уносил подтверждения контуров молча.
    """
    api = _server(raster, **{"upload:contours_validated": APIError("boom", 500)})
    tab = open_contours(api)
    _operator_applies_contours(tab)

    assert tab._save_graph() is False, (
        "половина записи объявлена сохранением — вкладка закроется без вопроса")
    assert tab.has_unsaved_changes() is True, (
        "дёрти-флаг погашен графовой половиной: дверь вопроса промолчит "
        "(diagram_workspace.py:1444)")
    assert "contours_validated" not in api.saves
    assert "graph_validated" in api.saves, (
        "графовая половина обязана остаться записанной — откатывать её нечем")


def test_operator_is_told_which_half_failed(raster, open_contours, dialogs):
    """Модалка называет ровно то, что не доехало, — как у соседа 1.x14."""
    api = _server(raster, **{"upload:contours_validated": APIError("boom", 500)})
    tab = open_contours(api)
    _operator_applies_contours(tab)
    tab._save_graph()

    texts = [text for _, text in dialogs]
    assert any("контуры" in t.lower() for t in texts), (
        f"оператор не узнал, какая половина не записалась: {dialogs}")


def test_failed_contour_upload_after_a_successful_save_leaves_the_tab_unsaved(
        raster, open_contours, dialogs):
    """Тот же отказ, но ПОСЛЕ предыстории — успешного сохранения (`PROTOCOL §3`).

    Свежая вкладка предусловие лекарства выполняет заведомо; здесь `_saved_revision`
    уже поднят прошлым успешным сохранением, то есть проверяется взаимодействие
    с историей сессии, а не поведение чистого листа.
    """
    api = _server(raster)
    tab = open_contours(api)
    _operator_applies_contours(tab)
    assert tab._save_graph() is True
    assert tab.has_unsaved_changes() is False

    # ... связь с сервером портится, оператор снимает один контур и сохраняет
    api.failures["upload:contours_validated"] = APIError("gateway timeout", 504)
    tab._remove_all_contours()

    assert tab._save_graph() is False
    assert tab.has_unsaved_changes() is True, (
        "после успешного сохранения дёрти-флаг больше не восстанавливается")
    assert _approved_on_server(api) == N_NODES, (
        "на сервере остались подтверждения ПРОШЛОГО сохранения — их и потерял бы "
        "оператор, уйдя без вопроса")


# =========================================================================
# 3. Границы: отказ ГРАФОВОЙ половины пункт не трогает
# =========================================================================

def test_failed_graph_upload_is_still_a_refusal(raster, open_contours, dialogs):
    """Первая половина не записалась → `False`, как и было; контуры не пишутся."""
    api = _server(raster, **{"upload:graph_validated": APIError("boom", 500)})
    tab = open_contours(api)
    _operator_applies_contours(tab)

    assert tab._save_graph() is False
    assert api.saves == [], f"после отказа графа что-то записано: {api.saves}"
    assert tab.has_unsaved_changes() is True
