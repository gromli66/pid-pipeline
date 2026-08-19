# -*- coding: utf-8 -*-
"""Пункт 1.x8 дороги, носитель 4 — отказ загрузки графа во «Привязке подписей».

Тот же дефект, что закрыл 1.23 в графовой вкладке (`test_download_failure_is_visible.py`),
второй его носитель: цепочка `graph_validated` → `graph_json` в
`ui/tabs/ocr_binding_tab.py:65` стоит БЕЗ `failure_key`, то есть не различает
«сохранённого графа нет» (404, первый заход — фолбэк законен) и «сохранённый
граф не отдался» (5xx, сеть, диск).

Цена ровно та же и здесь она ЗАМЕРЕНА, а не выведена: `_save_binding`
(`:1634-1639`) кладёт `self._graph_data` обратно в `graph_validated`
`api_client.upload_validated_graph`. Значит после тихого фолбэка первое же
«Сохранить» затирает сохранённую работу оператора ИСХОДНЫМ графом сборщика.
Путь достижим и автосохранением: `ui/services/autosave.py:_SAVE_METHODS`
зовёт у этой вкладки `_save_binding` напрямую.

Замер §73г до правки: 504 и 404 неразличимы — оба молча приводят исходный граф
(2 узла вместо 3) и НЕ кладут в артефакты ни одного флага отказа.

Инвариант — тот же, что у 1.23: **404 = сохранённого законно нет, фолбэк молчит;
любой другой отказ = оператор ВИДИТ предупреждение, что открыл не свою работу
и что сохранение её затрёт.**

⛔ Поле `swallow` не трогается ни у одного задания: различение 0.5
(не-`APIError` уводит вкладку в ошибку) — несущее поведение, здесь оно
сторожится отдельным тестом.

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом:
модальный диалог в пути инъекции подвешивает набор вместо падения
(`PROTOCOL §5`, замеры 1.5, 1.3 и 1.4).
"""
import json
import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import QThread                               # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox          # noqa: E402
from PySide6.QtGui import QImage, QColor                         # noqa: E402

from ui.services.api_client import APIError                      # noqa: E402

UID = "1c23aa01"
W, H = 400, 300                 # растр-подложка

N_SAVED = 3                     # узлов в сохранённом графе оператора
N_RAW = 2                       # узлов в исходном графе сборщика

#: заголовок предупреждения — то, по чему оператор отличает отказ от «его нет»
TITLE_GRAPH = "Сохранённый граф не загружен"


# ── данные ───────────────────────────────────────────────────────────────

def _graph(n_nodes: int) -> dict:
    """Граф из n_nodes узлов: число узлов и есть метка происхождения."""
    nodes = []
    for i in range(n_nodes):
        x = 60.0 + 100 * i
        nodes.append({"id": f"n{i}", "type": "equipment",
                      "class_name": "zadvizhka", "centroid": [80.0, x],
                      "bbox": [x - 12, 68, x + 12, 92]})
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [H, W]},
            "nodes": nodes, "links": [], "text_blocks": [], "bindings": []}


GRAPH_SAVED = json.dumps(_graph(N_SAVED)).encode()
GRAPH_RAW = json.dumps(_graph(N_RAW)).encode()
OCR_RESULT = json.dumps({"target": [], "secondary": []}).encode()


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
    """Отдаёт заданные байты; чего нет — `APIError` 404; заданное — своей ошибкой.

    OCR-артефакты у этой вкладки идут своими методами `APIClient`, а не общим
    `download_artifact` (см. `_ARTIFACTS`), поэтому их здесь тоже три штуки.
    """

    def __init__(self, blobs, failures=None):
        self.blobs = dict(blobs)
        self.failures = dict(failures or {})
        self.calls = []

    def _put(self, name, dest_path):
        self.calls.append(name)
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


def _server(raster, *, saved=True, raw=True, failures=None):
    """Сервер по описанию: что на нём лежит и что из этого отказывает."""
    blobs = {"original_image": raster, "ocr_result": OCR_RESULT}
    if saved:
        blobs["graph_validated"] = GRAPH_SAVED
    if raw:
        blobs["graph_json"] = GRAPH_RAW
    return FakeAPI(blobs, failures)


# ── харнесс ──────────────────────────────────────────────────────────────

def _download(api, tmp_dir):
    """Прогнать НАСТОЯЩИЙ загрузчик вкладки синхронно: (artifacts, error)."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.ocr_binding_tab import _ARTIFACTS

    dl = ArtifactDownloader(api, UID, tmp_dir, _ARTIFACTS)
    out = {}
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    return out.get("artifacts"), out.get("error")


@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки вкладки → список (title, text). Возврата ждать некому."""
    seen = []

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return QMessageBox.StandardButton.Ok

    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_rec))
    return seen


@pytest.fixture
def open_tab(qapp, monkeypatch):
    """Открыть вкладку тем словарём, который отдал НАСТОЯЩИЙ загрузчик.

    Поток снят: загрузчик крутится синхронно в этом же процессе, а его
    результат уходит во вкладку тем же слотом `_on_download_finished`, которым
    его доставляет бой. Шов «загрузчик → вкладка» не подменён — через него
    и идёт проверка.
    """
    from ui.tabs.ocr_binding_tab import OcrBindingTab

    opened = []

    def _open(api):
        monkeypatch.setattr(
            OcrBindingTab, "_start_download",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = OcrBindingTab(UID, "проба 1.x8", api)
        opened.append(tab)
        artifacts, error = _download(api, tab.temp_dir)
        assert error is None, f"загрузчик увёл вкладку в ошибку: {error}"
        tab._on_download_finished(artifacts)
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()
        # ⛔ Снос детерминированный (замер §73е): брошенная вкладка оставляет
        # в очереди Qt отложенные удаления, а падает на них ЧУЖОЙ набор — тот,
        # что первым крутит `processEvents`. Очередь выпивается здесь, своим.
        tab.deleteLater()
    qapp.processEvents()


def _nodes_of(tab) -> int:
    """Сколько узлов в графе, который вкладка положила себе — по нему видно, ЧЕЙ он."""
    return len(tab._graph_data.get("nodes", []))


def _titles(dialogs):
    return [title for title, _ in dialogs]


# ── половина 1: что оператор получил в редакторе ─────────────────────────

def test_saved_graph_arrives_when_server_is_healthy(raster, open_tab, dialogs):
    """Здоровый сервер: у оператора его сохранённая работа, ни одной модалки."""
    tab = open_tab(_server(raster))

    assert _nodes_of(tab) == N_SAVED
    assert _nodes_of(tab) != N_RAW
    assert dialogs == []


def test_missing_saved_graph_falls_back_silently(raster, open_tab, dialogs):
    """404 = сохранённого законно нет (первый заход) — фолбэк молчит."""
    tab = open_tab(_server(raster, saved=False))

    assert _nodes_of(tab) == N_RAW
    assert dialogs == [], "404 — законный первый заход, пугать оператора нечем"


def test_failed_saved_graph_warns_the_operator(raster, open_tab, dialogs):
    """5xx: открыт исходный граф — и оператор ВИДИТ, что открыл не свою работу."""
    tab = open_tab(_server(
        raster, failures={"graph_validated": APIError("gateway timeout", 504)}))

    assert _nodes_of(tab) == N_RAW, "фолбэк обязан состояться — вкладка работает"
    assert TITLE_GRAPH in _titles(dialogs), \
        "оператор молча получил чужой граф, а «Сохранить» затрёт им его собственный"


def test_warning_names_the_cost_of_saving(raster, open_tab, dialogs):
    """Текст модалки обязан назвать ПОСЛЕДСТВИЕ, иначе предупреждение декоративно."""
    open_tab(_server(
        raster, failures={"graph_validated": APIError("boom", 500)}))

    text = next(t for title, t in dialogs if title == TITLE_GRAPH)
    assert "затрёт" in text, "модалка не говорит, чем грозит сохранение"


# ── половина 2: флаг от загрузчика и Д2-след ─────────────────────────────

def test_downloader_flags_only_non_404_failures(raster, tmp_path):
    """Флаг ставится ровно на «состояние неизвестно», а не на любой отказ."""
    key = "saved_graph_download_failed"

    healthy, _ = _download(_server(raster), tmp_path / "a")
    missing, _ = _download(_server(raster, saved=False), tmp_path / "b")
    broken, _ = _download(
        _server(raster, failures={"graph_validated": APIError("boom", 503)}),
        tmp_path / "c")

    assert healthy.get(key) is None
    assert missing.get(key) is None, "404 — не отказ, а законное отсутствие"
    assert broken.get(key) is True
    assert "graph" in broken, "флаг обязан приезжать ВМЕСТЕ с фолбэком, не вместо"


def test_failed_saved_graph_leaves_a_log_line(raster, open_tab, dialogs, caplog):
    """Д2: отказ виден не только оператору, но и в логе клиента."""
    with caplog.at_level(logging.WARNING, logger="ui.tabs.ocr_binding_tab"):
        open_tab(_server(
            raster, failures={"graph_validated": APIError("boom", 502)}))

    lines = [r.getMessage() for r in caplog.records
             if r.name == "ui.tabs.ocr_binding_tab" and r.levelno >= logging.WARNING]
    assert any("graph_validated" in m for m in lines), \
        f"в логе вкладки нет строки об отказе: {lines}"


# ── границы, которые пункт не двигает ────────────────────────────────────

def test_both_candidates_gone_still_drives_the_tab_to_error(raster, tmp_path):
    """`required=True` не тронут: не осталось ни одного кандидата — вкладка в ошибке."""
    artifacts, error = _download(
        _server(raster, saved=False, raw=False), tmp_path / "d")

    assert artifacts is None
    assert error is not None


def test_swallow_of_optional_jobs_is_untouched(raster, tmp_path):
    """Характеризация, НЕ приёмка: у этой вкладки `swallow` шире, чем у графовой.

    ⛔ Замерено, а не выведено (§73д). У необязательных заданий здесь стоит
    ДЕФОЛТНЫЙ `swallow=(Exception,)`, а не `(APIError,)`, как у графовой вкладки
    после 0.5. Значит `OSError` (диск, права) на `ocr_binding` глотается молча
    и без флага: привязки оператора не приезжают, а вкладка об этом не говорит.
    Это НЕ адрес пункта 1.x8 (пункт правит цепочку графа), поэтому поведение
    здесь ЗАПЕРТО КАК ФАКТ — чтобы правка 1.x8 не сдвинула его случайно, — и
    вынесено архитектору строкой в журнал доски. Тест обязан быть переписан
    тем пунктом, который возьмётся за различение 0.5 в этой вкладке.
    """
    api = _server(raster)
    api.failures["ocr_binding"] = OSError("диск отвалился")

    artifacts, error = _download(api, tmp_path / "e")

    assert error is None, "поведение изменилось — см. докстроку, это находка §73д"
    assert "binding" not in artifacts, "привязки не приехали"
    assert not [k for k, v in artifacts.items() if v is True], \
        "флага отказа нет — вкладка не отличает «привязок нет» от «не отдались»"
