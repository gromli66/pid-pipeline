# -*- coding: utf-8 -*-
"""Пункт 1.x3 дороги — вкладка труб просит у сервера НЕСУЩЕСТВУЮЩИЙ тип артефакта.

До правки `ui/tabs/pipe_tab.py` тянул `segmentation_mask`, а такого значения нет
в `app/models/artifact.ArtifactType`. Эндпоинт `GET /{uid}/download/{type}`
(`app/api/diagrams.py:369-376`) валидирует тип ДО поиска артефакта и на
неизвестном отдаёт **400**, а не 404. Задание необязательное с политикой
`swallow=(Exception,)` (её ввёл 0.5 и трогать её нельзя) — 400 глотался,
ключ во вкладку не приезжал.

Следствие, ради которого пункт заведён: `_estimate_median_thickness` не
вызывался никогда, `median_thickness` всегда `None`, и стартовая ширина
кисти всегда равнялась дефолту слайдера (4 px) — при том что тултип «Ширина»
обещает «По умолчанию — медианная толщина труб на схеме».

⛔ Почему характеризационный набор 0.5 этого не увидел: его `FakeAPI`
(`tests/ui/test_artifact_downloader.py`) держит `segmentation_mask` в списке
блобов, то есть подставной сервер ЩЕДРЕЕ боевого и отдаёт тип, который боевой
отвергает. Поэтому первый слой здесь сверяет запросы вкладок не с подставным
сервером, а с самим `ArtifactType`, а второй слой ведёт подставной сервер ровно
как боевой эндпоинт: неизвестный тип → 400, известный, но отсутствующий → 404.

Числа абсолютные. Ожидаемая ширина 10 px снята один раз с синтетической маски
пером 9 px тем же `_estimate_median_thickness` и вписана константой — тест её
не вычисляет. Порог заперт с двух сторон: маска сегментации есть → 10, её нет
на сервере → дефолт 4.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import Qt, QThread                           # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen         # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402

from app.models.artifact import ArtifactType                     # noqa: E402
from ui.services.api_client import APIError                      # noqa: E402

UID = "1cx3aa01"
W, H = 400, 300

#: Перо синтетической маски сегментации и ширина, которую с неё снимает
#: `polyline_mask_editor._estimate_median_thickness`. Замерено 2026-08-19
#: (MEASUREMENTS §72.4): перо 5 → 6, 7 → 8, 8 → 8, 9 → 10, 11 → 12.
SEG_PEN = 9
EXPECT_WIDTH = 10

#: Дефолт слайдера «Ширина» (`pipe_tab._setup_ui`) — то, что оператор получает,
#: когда медианную толщину посчитать не из чего.
DEFAULT_WIDTH = 4

#: Типы артефактов, которые боевой эндпоинт вообще готов обслуживать.
VALID_TYPES = {t.value for t in ArtifactType}

#: Кандидаты на роль маски сегментации: пункт про существование типа,
#: а не про выбор между сырой маской и уточнённой.
SEG_TYPES = ("pipe_mask", "pipe_mask_refined")


# ── данные ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _png(img: QImage, path: Path) -> bytes:
    assert img.save(str(path))
    return path.read_bytes()


@pytest.fixture(scope="module")
def rasters(qapp, tmp_path_factory) -> dict:
    """Оригинал, редактируемая маска и маска сегментации пером SEG_PEN."""
    root = tmp_path_factory.mktemp("pipe_rasters")

    original = QImage(W, H, QImage.Format.Format_RGB32)
    original.fill(QColor("white"))

    def lines(pen_width: int) -> QImage:
        img = QImage(W, H, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        p.setPen(QPen(QColor("white"), pen_width, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(40, 90, 360, 90)
        p.drawLine(40, 200, 360, 200)
        p.drawLine(120, 90, 120, 200)
        p.end()
        return img

    return {
        "original_image": _png(original, root / "original.png"),
        # редактируемая маска нарочно тоньше: снять ширину с НЕЁ нельзя
        "pipe_mask_validated": _png(lines(3), root / "mask.png"),
        "segmentation": _png(lines(SEG_PEN), root / "seg.png"),
    }


# ── подставной сервер: ведёт себя как боевой эндпоинт ────────────────────

class ServerLikeAPI:
    """`GET /{uid}/download/{type}`: неизвестный тип → 400, отсутствующий → 404.

    Валидация типа стоит ДО поиска артефакта (`app/api/diagrams.py:369-376`),
    поэтому разница видна и тогда, когда артефакта на сервере нет вовсе.
    """

    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.calls = []

    def download_artifact(self, uid, artifact_type, dest_path):
        self.calls.append(artifact_type)
        if artifact_type not in VALID_TYPES:
            raise APIError(
                f"Invalid artifact type. Valid: {sorted(VALID_TYPES)}", 400)
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(
                f"Artifact '{artifact_type}' not found for diagram {uid}", 404)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return dest_path


def _server(rasters, *, segmentation=True):
    blobs = {"original_image": rasters["original_image"],
             "pipe_mask_validated": rasters["pipe_mask_validated"]}
    if segmentation:
        for art_type in SEG_TYPES:
            blobs[art_type] = rasters["segmentation"]
    return ServerLikeAPI(blobs)


def _download(api, tmp_dir):
    """Прогнать НАСТОЯЩИЙ список заданий вкладки синхронно."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.pipe_tab import _ARTIFACTS

    dl = ArtifactDownloader(api, UID, tmp_dir, _ARTIFACTS)
    out = {}
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    return out.get("artifacts"), out.get("error")


@pytest.fixture
def dialogs(monkeypatch):
    """Модалки вкладки → список (title, text): пустой = ничего не упало."""
    seen = []

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Ok

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
def open_pipe_tab(qapp, monkeypatch, dialogs):
    """Открыть вкладку тем словарём, который отдал настоящий загрузчик.

    Поток снят, шов «загрузчик → вкладка» не подменён: результат уходит
    во вкладку тем же слотом `_on_downloaded`, которым его доставляет бой.
    """
    from ui.tabs.pipe_tab import PipeTab

    opened = []

    def _open(api):
        monkeypatch.setattr(
            PipeTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = PipeTab(UID, "проба 1.x3", api)
        opened.append(tab)
        artifacts, error = _download(api, tab.temp_dir)
        assert error is None, f"загрузчик увёл вкладку в ошибку: {error}"
        tab._on_downloaded(artifacts)
        assert not dialogs, f"вкладка ругнулась при открытии: {dialogs}"
        return tab

    yield _open
    for tab in opened:
        tab.deleteLater()


def _jobs_of_every_tab():
    """Списки заданий всех вкладок клиента: (имя, задания)."""
    from ui.tabs.base_graph_tab import _graph_jobs
    from ui.tabs.junction_tab import _ARTIFACTS as junction
    from ui.tabs.ocr_binding_tab import _ARTIFACTS as ocr
    from ui.tabs.pipe_tab import _ARTIFACTS as pipe

    return [("graph", _graph_jobs(want_canvas=False)),
            ("graph_canvas", _graph_jobs(want_canvas=True)),
            ("junction", junction),
            ("pipe", pipe),
            ("ocr", ocr)]


# =========================================================================
# Слой 1 — контракт: вкладка не имеет права просить тип, которого нет
# =========================================================================

@pytest.mark.parametrize("kind", [k for k, _ in _jobs_of_every_tab()])
def test_every_requested_artifact_type_exists_on_the_server(kind):
    """Тип, которого нет в `ArtifactType`, эндпоинт отвергает с 400 навсегда.

    Проверка идёт против самого перечисления, а не против подставного сервера:
    подставной сервер можно сделать щедрее боевого, и ровно так дефект прожил
    мимо характеризационного набора 0.5.
    """
    jobs = dict(_jobs_of_every_tab())[kind]
    unknown = sorted({f.art_type for job in jobs for f in job.fetches
                      if f.art_type and f.art_type not in VALID_TYPES})
    assert unknown == [], (
        f"вкладка {kind} просит типы, которых нет в ArtifactType: {unknown}")


def test_pipe_tab_asks_for_a_segmentation_mask_at_all():
    """У вкладки труб есть задание на маску сегментации — и оно одно."""
    from ui.tabs.pipe_tab import _ARTIFACTS

    seg = [f.art_type for job in _ARTIFACTS for f in job.fetches
           if f.art_type in SEG_TYPES]
    assert len(seg) == 1, f"заданий на маску сегментации: {seg}"


# =========================================================================
# Слой 2 — загрузчик против сервера, ведущего себя как боевой
# =========================================================================

def test_segmentation_mask_reaches_the_tab(rasters, tmp_path):
    """Маска сегментации доезжает до вкладки, а не глотается 400-м."""
    api = _server(rasters)
    artifacts, error = _download(api, tmp_path)
    assert error is None
    landed = {k: v for k, v in artifacts.items() if k in SEG_TYPES}
    assert len(landed) == 1, f"маска сегментации не доехала: {sorted(artifacts)}"
    assert Path(next(iter(landed.values()))).read_bytes() == rasters["segmentation"]


def test_no_request_is_answered_with_400(rasters, tmp_path):
    """Ни один запрос вкладки труб сервер не отвергает как неизвестный тип."""
    api = _server(rasters)
    _download(api, tmp_path)
    rejected = sorted({t for t in api.calls if t not in VALID_TYPES})
    assert rejected == [], f"сервер отверг с 400: {rejected}"


def test_missing_segmentation_mask_is_still_swallowed(rasters, tmp_path):
    """404 (маски сегментации на сервере нет) вкладку по-прежнему не роняет.

    Политику `swallow` ввёл 0.5 осознанно, и здесь она не меняется.
    """
    api = _server(rasters, segmentation=False)
    artifacts, error = _download(api, tmp_path)
    assert error is None
    for art_type in SEG_TYPES:
        assert art_type not in artifacts
    assert Path(artifacts["pipe_mask_validated"]).exists()


# =========================================================================
# Слой 3 — что видит оператор: стартовая ширина кисти
# =========================================================================

def test_start_width_is_the_median_pipe_thickness(rasters, open_pipe_tab):
    """Тултип «Ширина» обещает медианную толщину труб — она и стоит."""
    tab = open_pipe_tab(_server(rasters))
    assert tab._editor is not None
    assert tab._editor.median_thickness == EXPECT_WIDTH
    assert tab.width_slider.value() == EXPECT_WIDTH
    assert tab.width_label.text() == f"{EXPECT_WIDTH}px"
    assert tab._editor.line_width == EXPECT_WIDTH


def test_start_width_falls_back_to_default_without_the_mask(
        rasters, open_pipe_tab):
    """Порог с другой стороны: маски сегментации нет → дефолт слайдера."""
    tab = open_pipe_tab(_server(rasters, segmentation=False))
    assert tab._editor is not None
    assert tab._editor.median_thickness is None
    assert tab.width_slider.value() == DEFAULT_WIDTH
    assert tab.width_label.text() == f"{DEFAULT_WIDTH}px"
