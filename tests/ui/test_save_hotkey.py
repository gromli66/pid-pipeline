# -*- coding: utf-8 -*-
"""Пункт 1.19 дороги — Ctrl+S делает ровно то, что кнопка 💾.

Дефект был двойной, и обе половины — одна семья «хоткей ушёл своей дорогой»:

* **вкладка труб.** Ctrl+S звал `PolylineMaskEditor.save_mask()` без пути,
  а тот по умолчанию писал в ОТНОСИТЕЛЬНЫЙ `pipe_mask_validated.png`
  (`polyline_mask_editor.py:1020`) — то есть в CWD процесса, мимо сервера.
  Оператор видел «Сохранено: pipe_mask_validated.png» и уходил с вкладки;
  на сервере не оставалось ничего.
* **графовые вкладки.** Подсказка кнопки 💾 обещает «Сохранить граф
  на сервер (Ctrl+S)» (`base_graph_tab.py:819`), а Ctrl+S открывал
  ЛОКАЛЬНЫЙ диалог `QFileDialog` (`base_graph_editor.py:1592`).
  Кнопка и хоткей делали разные вещи.

Что выбрано и почему. Хоткей приведён к кнопке, а не подпись к хоткею:
надпись, которую оператор читает, обещает сервер в 3 подсказках из 3
(`base_graph_tab:819`, `pipe_tab:205`, `junction_tab:172`), локальную запись
не обещает ни одна; таблица горячих клавиш `UI_GUIDE §12.1` Ctrl+S не знала
вовсе; а собственная карта сохранений клиента (`autosave._SAVE_METHODS`)
для КАЖДОЙ вкладки называет серверный метод. Вкладка перекрёстков к этому
виду приведена раньше (`square_mask_editor.py:673` → `save_requested_callback`),
здесь тот же идиом доводится до двух оставшихся редакторов.

⛔ Хоткей жмёт САМУ КНОПКУ, а не зовёт метод сохранения: состояние кнопки
и есть право на сохранение. `AdvancedGraphTab` гасит её на время
распознавания (`advanced_graph_tab.py:467`) — гонка с фоновым потоком,
иначе на сервер уйдёт граф без распознанного текста; `ContourTab` прячет
её вовсе (`contour_tab.py:111`). Прямой вызов `_save_graph` обошёл бы обе
защиты — это и заперто ниже, полным перебором вкладок, а не одной.

Событие подаётся `QApplication.sendEvent`, то есть тем же диспетчером,
которым его приносит Qt: колбэк, не подключённый на боевом пути
`_on_downloaded`, так не позеленеет. Вкладки собираются настоящим
`_on_downloaded` из настоящего `ArtifactDownloader` — шов не подменён.

Числа абсолютные: сколько заливок ушло на сервер и сколько диалогов
открылось. Порог заперт с двух сторон — там, где сохранение разрешено,
проверяется ровно одна заливка, где запрещено — ровно ноль.
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

#: Файл, который редактор труб ронял в CWD: `save_mask()` без пути.
CWD_LITTER = "pipe_mask_validated.png"


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
    # Вопрос «да / отмена» с пункта 5.3 собирается своими кнопками (русскими),
    # мимо статической двери `QMessageBox.question`, — подменяется отдельно.
    from ui.tabs.blind_overwrite import BlindOverwriteGuard
    monkeypatch.setattr(
        BlindOverwriteGuard, "_ask_yes_cancel",
        lambda self, title, text:
            _rec(self, title, text) == QMessageBox.StandardButton.Yes)
    return seen


@pytest.fixture(autouse=True)
def _guard_cwd(monkeypatch, tmp_path):
    """CWD — всегда пустой временный каталог: мусор в нём виден и не мешает."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _no_litter(cwd):
    """Ни один путь сохранения не насорил в CWD процесса."""
    return sorted(p.name for p in Path(cwd).iterdir())


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
        self.uploads.append(("canvas", Path(path).name))
        return True

    def upload_validated_graph(self, uid, path):
        self.uploads.append(("validated", Path(path).name))
        return True

    def download_contours_auto(self, uid, dest):
        """Контуры ещё не распознаны — законный первый заход `ContourTab`.

        Отдаётся именно `APIError`: вкладка разбирает его сама
        (`contour_tab.py:224`) и остаётся живой. Без метода вовсе вылетал бы
        `AttributeError`, вкладка ловила бы его внешним `except` и собиралась
        наполовину — обстановка теста разошлась бы с боевой (PROTOCOL §5).
        """
        from ui.services.api_client import APIError
        raise APIError(f"contours_auto not found for {uid}", 404)


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
        assert not dialogs, f"вкладка ругнулась при открытии: {dialogs}"
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


@pytest.fixture(autouse=True)
def file_dialog(monkeypatch):
    """QFileDialog редактора → список открытий; сам диалог никогда не всплывает.

    ⛔ Подменяется ВСЕГДА, а не только там, где ждут диалог. Настоящий
    `getSaveFileName` модальный: зонд «вернуть старую ветку Ctrl+S» без этой
    подмены не покраснел и не позеленел, а ПОВИС — набор встал насмерть
    вместо падения (PROTOCOL §5, четвёртый исход зонда). Утверждение
    ставится о ФАКТЕ вызова, не по таймауту.
    """
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
def test_graph_hotkey_uploads_like_the_button(open_graph_tab, factory,
                                              file_dialog, _guard_cwd):
    """Ctrl+S уходит на сервер — ровно туда, куда обещает подсказка кнопки."""
    tab = open_graph_tab(factory())

    _ctrl_s(tab._editor)

    assert file_dialog == [], "открылся локальный диалог сохранения"
    assert len(tab.api_client.uploads) == 1, \
        f"на сервер ушло {tab.api_client.uploads} вместо одной заливки"
    assert _no_litter(_guard_cwd) == [], "хоткей насорил в CWD"


@pytest.mark.parametrize("factory", [_simple, _advanced],
                         ids=["simple", "advanced"])
def test_graph_hotkey_and_button_send_the_same_thing(open_graph_tab, factory,
                                                     file_dialog):
    """Кнопка и хоткей отдают серверу один и тот же артефакт одним путём."""
    tab = open_graph_tab(factory())

    tab.btn_save.click()
    by_button = list(tab.api_client.uploads)
    _ctrl_s(tab._editor)
    by_hotkey = tab.api_client.uploads[len(by_button):]

    assert len(by_button) == 1, f"кнопка отдала {by_button}"
    assert by_hotkey == by_button, \
        f"хоткей отдал {by_hotkey}, кнопка — {by_button}"
    assert file_dialog == [], "открылся локальный диалог сохранения"


def test_graph_hotkey_respects_disabled_button(open_graph_tab, file_dialog):
    """Кнопка выключена (гонка с распознаванием) — хоткей тоже молчит.

    `AdvancedGraphTab._on_recognize` гасит 💾 и «Подтвердить», пока фоновый
    поток не вернул текст: сохранение до его прихода зафиксировало бы граф
    без распознанного. Прямой вызов `_save_graph` из хоткея обошёл бы защиту.
    """
    tab = open_graph_tab(_advanced())
    tab.btn_save.setEnabled(False)

    _ctrl_s(tab._editor)

    assert tab.api_client.uploads == [], "хоткей сохранил в обход выключенной кнопки"
    assert file_dialog == [], "открылся локальный диалог сохранения"


def test_contour_hotkey_saves_nothing_without_a_button(open_graph_tab, file_dialog):
    """У вкладки контуров кнопки 💾 нет — значит, и хоткею сохранять нечем."""
    tab = open_graph_tab(_contour())
    assert tab.btn_save.isHidden(), "кнопка 💾 на вкладке контуров должна быть скрыта"

    _ctrl_s(tab._editor)

    assert tab.api_client.uploads == [], "хоткей сохранил там, где кнопки нет"
    assert file_dialog == [], "открылся локальный диалог сохранения"
    assert "Подтвердить" in tab.status_label.text(), \
        f"оператору не сказано, чем сохранять: {tab.status_label.text()!r}"


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


def test_pipe_hotkey_uploads_instead_of_littering_cwd(pipe_tab, _guard_cwd):
    """Ctrl+S отдаёт маску серверу и не оставляет файла в CWD процесса."""
    _ctrl_s(pipe_tab._editor)

    kinds = [k for k, _ in pipe_tab.api_client.uploads]
    assert kinds == ["pipe_mask_validated"], f"на сервер ушло {kinds}"
    assert _no_litter(_guard_cwd) == [], \
        f"в CWD осталось {_no_litter(_guard_cwd)}"


def test_pipe_hotkey_and_button_send_the_same_bytes(pipe_tab):
    """Кнопка и хоткей отдают серверу один и тот же артефакт байт в байт."""
    pipe_tab.btn_save.click()
    by_button = list(pipe_tab.api_client.uploads)
    _ctrl_s(pipe_tab._editor)
    by_hotkey = pipe_tab.api_client.uploads[len(by_button):]

    assert len(by_button) == 1, f"кнопка отдала {[k for k, _ in by_button]}"
    assert by_hotkey == by_button, "хоткей отдал не то, что кнопка"


def test_pipe_editor_alone_saves_nothing(qapp, _guard_cwd):
    """Редактор без вкладки: Ctrl+S молчит, а не пишет в CWD.

    Колбэк ставит вкладка. Не подключён — сохранять нечем и некуда: адрес
    сервера знает только она. Раньше этот же путь ронял PNG в CWD.
    """
    from ui.editors.polyline_mask_editor import PolylineMaskEditor

    editor = PolylineMaskEditor()
    said = []
    editor.status_callback = said.append
    try:
        _ctrl_s(editor)
    finally:
        editor.setParent(None)
        editor.deleteLater()

    assert _no_litter(_guard_cwd) == [], \
        f"в CWD осталось {_no_litter(_guard_cwd)}"
    assert said and "кнопкой" in said[-1], f"оператору сказано {said!r}"
