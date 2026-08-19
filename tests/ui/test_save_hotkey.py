# -*- coding: utf-8 -*-
"""Пункт 1.19 дороги — Ctrl+S делает то же, что кнопка 💾.

ХАРАКТЕРИЗАЦИЯ (первый заход): здесь заперто ТЕКУЩЕЕ поведение, «как есть
сегодня», а не «как правильно». Файл переписывается правкой пункта.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                     # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                          # noqa: E402

from PySide6.QtCore import QEvent, Qt, QThread                    # noqa: E402
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox           # noqa: E402

from tools import corpus                                          # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier

UID = "d74eb9f1"          # корпус-фикстура в git (пункт 0.8)
W, H = 400, 300


def _ctrl_s(widget):
    """Ctrl+S так, как его приносит Qt: через диспетчер событий виджета."""
    QApplication.sendEvent(
        widget, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_S, CTRL, "s"))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    """Модалки → список (title, text): пустой = вкладка ни на что не ругалась."""
    seen = []

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Yes

    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_rec))
    return seen


# ── графовая вкладка ─────────────────────────────────────────────────────

class GraphAPI:
    """Подставной сервер графовой вкладки: что скачали и что залили."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.uploads = []

    def download_artifact(self, uid, artifact_type, dest_path):
        from ui.services.api_client import APIError
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def upload_canvas_graph(self, uid, path):
        self.uploads.append(("canvas", uid))
        return True

    def upload_validated_graph(self, uid, path):
        self.uploads.append(("validated", uid))
        return True


@pytest.fixture(scope="module")
def graph_blobs(qapp, tmp_path_factory):
    """Растр размером с корпусную схему + сам корпусный граф."""
    path = corpus.graph_path(UID)
    assert path is not None, f"корпус-фикстура {UID} не найдена (tools/corpus.py)"
    graph = path.read_bytes()
    h, w = json.loads(graph.decode("utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("graph_raster") / f"{UID}.png"
    assert img.save(str(png))
    return {"original_image": png.read_bytes(), "graph_json": graph}


@pytest.fixture
def open_graph_tab(qapp, monkeypatch, dialogs, graph_blobs):
    """Вкладка графа, собранная тем же слотом `_on_downloaded`, что в бою."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.base_graph_tab import BaseGraphTab, _graph_jobs

    opened = []

    def _open(cls):
        monkeypatch.setattr(
            BaseGraphTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        api = GraphAPI(graph_blobs)
        tab = cls(UID, "проба 1.19", api)
        opened.append(tab)

        out = {}
        dl = ArtifactDownloader(api, UID, tab.temp_dir,
                                _graph_jobs(want_canvas=cls.USE_CANVAS))
        dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
        dl.error.connect(lambda m: out.__setitem__("error", m))
        dl.run()
        assert out.get("error") is None, f"загрузчик увёл вкладку в ошибку: {out}"
        tab._on_downloaded(out["artifacts"])
        assert tab._editor is not None, "редактор не собрался"
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()


def _simple():
    from ui.tabs.simple_graph_tab import SimpleGraphTab
    return SimpleGraphTab


def _advanced():
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    return AdvancedGraphTab


def _contour():
    from ui.tabs.contour_tab import ContourTab
    return ContourTab


def _patch_file_dialog(monkeypatch):
    """QFileDialog редактора → счётчик открытий (запись никогда не идёт)."""
    from ui.editors import base_graph_editor as mod

    seen = []

    class _FakeDialog:
        @staticmethod
        def getSaveFileName(parent, caption, directory="", filt="", *a, **kw):
            seen.append(directory)
            return "", filt

    monkeypatch.setattr(mod, "QFileDialog", _FakeDialog)
    return seen


@pytest.mark.parametrize("factory", [_simple, _advanced],
                         ids=["simple", "advanced"])
def test_graph_hotkey_opens_local_dialog(open_graph_tab, monkeypatch, factory):
    """КАК ЕСТЬ: Ctrl+S открывает ЛОКАЛЬНЫЙ диалог, на сервер не уходит ничего."""
    tab = open_graph_tab(factory())
    seen = _patch_file_dialog(monkeypatch)

    _ctrl_s(tab._editor)

    assert len(seen) == 1, "диалог сохранения не открылся"
    assert tab.api_client.uploads == [], "на сервер что-то ушло"


def test_graph_button_uploads_to_server(open_graph_tab):
    """КАК ЕСТЬ: кнопка 💾 идёт другим путём — заливкой на сервер."""
    tab = open_graph_tab(_advanced())

    tab.btn_save.click()

    assert len(tab.api_client.uploads) == 1, "кнопка на сервер не сходила"


def test_graph_hotkey_ignores_disabled_button(open_graph_tab, monkeypatch):
    """КАК ЕСТЬ: кнопка выключена (гонка с распознаванием), а Ctrl+S работает."""
    tab = open_graph_tab(_advanced())
    tab.btn_save.setEnabled(False)
    seen = _patch_file_dialog(monkeypatch)

    _ctrl_s(tab._editor)

    assert len(seen) == 1, "диалог сохранения не открылся"


def test_contour_hotkey_works_without_any_button(open_graph_tab, monkeypatch):
    """КАК ЕСТЬ: у вкладки контуров кнопки 💾 нет, а Ctrl+S всё равно отвечает."""
    tab = open_graph_tab(_contour())
    assert tab.btn_save.isHidden(), "кнопка 💾 на вкладке контуров должна быть скрыта"
    seen = _patch_file_dialog(monkeypatch)

    _ctrl_s(tab._editor)

    assert len(seen) == 1, "диалог сохранения не открылся"


# ── вкладка труб ─────────────────────────────────────────────────────────

class PipeAPI:
    """Подставной сервер вкладки труб."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.uploads = []

    def download_artifact(self, uid, artifact_type, dest_path):
        from ui.services.api_client import APIError
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def upload_validated_mask(self, uid, artifact_type, path):
        self.uploads.append((artifact_type, Path(path).read_bytes()))
        return True

    def upload_updated_nodes(self, uid, path):
        self.uploads.append(("coco_validated", Path(path).read_bytes()))
        return True


@pytest.fixture(scope="module")
def pipe_blobs(qapp, tmp_path_factory):
    root = tmp_path_factory.mktemp("pipe_raster")

    original = QImage(W, H, QImage.Format.Format_RGB32)
    original.fill(QColor("white"))
    orig_path = root / "original.png"
    assert original.save(str(orig_path))

    mask = QImage(W, H, QImage.Format.Format_RGB32)
    mask.fill(QColor("black"))
    p = QPainter(mask)
    p.setPen(QPen(QColor("white"), 3, Qt.SolidLine, Qt.FlatCap))
    p.drawLine(40, 90, 360, 90)
    p.end()
    mask_path = root / "mask.png"
    assert mask.save(str(mask_path))

    return {"original_image": orig_path.read_bytes(),
            "pipe_mask_validated": mask_path.read_bytes()}


@pytest.fixture
def pipe_tab(qapp, monkeypatch, dialogs, pipe_blobs):
    """Вкладка труб, собранная тем же слотом `_on_downloaded`, что в бою."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.pipe_tab import PipeTab, _ARTIFACTS

    monkeypatch.setattr(
        PipeTab, "_download_artifacts",
        lambda self: setattr(self, "_download_thread", QThread(self)))
    api = PipeAPI(pipe_blobs)
    tab = PipeTab(UID, "проба 1.19", api)

    out = {}
    dl = ArtifactDownloader(api, UID, tab.temp_dir, _ARTIFACTS)
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    assert out.get("error") is None, f"загрузчик увёл вкладку в ошибку: {out}"
    tab._on_downloaded(out["artifacts"])
    assert tab._editor is not None, "редактор труб не собрался"
    assert not dialogs, f"вкладка ругнулась при открытии: {dialogs}"
    yield tab
    tab.deleteLater()


def test_pipe_hotkey_writes_into_cwd(pipe_tab, monkeypatch, tmp_path):
    """КАК ЕСТЬ: Ctrl+S роняет PNG в CWD процесса, на сервер не уходит ничего."""
    monkeypatch.chdir(tmp_path)

    _ctrl_s(pipe_tab._editor)

    assert (tmp_path / "pipe_mask_validated.png").exists(), \
        "маска в CWD не появилась"
    assert pipe_tab.api_client.uploads == [], "на сервер что-то ушло"


def test_pipe_button_uploads_to_server(pipe_tab, monkeypatch, tmp_path):
    """КАК ЕСТЬ: кнопка 💾 идёт другим путём — заливкой на сервер."""
    monkeypatch.chdir(tmp_path)

    pipe_tab.btn_save.click()

    kinds = [k for k, _ in pipe_tab.api_client.uploads]
    assert kinds == ["pipe_mask_validated"], f"на сервер ушло {kinds}"
    assert not (tmp_path / "pipe_mask_validated.png").exists(), \
        "кнопка тоже насорила в CWD"
