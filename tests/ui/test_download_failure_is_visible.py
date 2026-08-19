# -*- coding: utf-8 -*-
"""Пункт 1.23 дороги — отказ загрузки СОХРАНЁННОЙ работы оператор не видит.

Две половины одного дефекта, обе в пути открытия графовой вкладки.

**Половина 1 — граф.** `_graph_jobs` (`base_graph_tab.py:675`) тянет граф
цепочкой `graph_validated` → `graph_json`. Цепочка не различает «сохранённого
графа нет» (404, первый заход на валидацию — фолбэк законен) и «сохранённый
граф не отдался» (5xx, сеть, диск). Во втором случае вкладка молча открывает
ИСХОДНЫЙ граф без правок оператора, а `_save_graph` пишет его обратно
в `graph_validated` (`:1126`) — прежняя валидация затирается.

**Половина 2 — холст.** `graph_canvas` (`:684`) необязателен, глотает `APIError`
и не имеет `failure_key`. При 5xx вкладка получает пустоту, неотличимую от 404:
`saved` пусто (`:872`) → ветка `else` → а предупреждающая модалка стоит ВНУТРИ
`if saved:` (`:892`), то есть не показывается вовсе. Холст тихо пересобирается
с нуля, и первый же Ctrl+S затирает серверный холст с ручной раскладкой
(семья 1.5: на сервер уходит то, что считает дёрти-флаг).

Инвариант пункта: **404 = артефакта законно нет, фолбэк молчит; любой другой
отказ = оператор ВИДИТ предупреждение, что открыл не свою работу и что
сохранение её затрёт.**

Образец различения — соседний `contours_validated` (`:686-688`,
`artifact_downloader.py:128-134`): у него это уже введено пунктом 0.5 как
критичное, потому что спутать сбой с «артефакта нет» = выбросить правки.
⛔ Строгость 0.5 (`swallow=(APIError,)` у графовой вкладки: не-APIError уводит
вкладку в ошибку) — несущее поведение, здесь она сторожится отдельно.

Проверяется только наблюдаемое оператором: сколько диалогов и с каким текстом
он увидел и КАКОЙ граф оказался у него в редакторе. Числа абсолютные и заперты
с двух сторон (и «пришёл исходный из 2 узлов», и «не пришёл сохранённый из 3»).

⚠ `QMessageBox` подменён с утверждением о ФАКТЕ вызова, а не таймаутом: модальный
диалог в пути инъекции подвешивает набор вместо падения (PROTOCOL §5, замеры 1.5
и 1.3). Веток модалок тут две — новая и старая «Схема изменилась», — поэтому
подменяются все три метода, а тесты считают их вместе.
"""
import json
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
N_OTHER = 4                     # третий источник: холст, устаревший для ОБОИХ

#: заголовки предупреждений — то, по чему оператор отличает одно от другого
TITLE_GRAPH = "Сохранённый граф не загружен"
TITLE_CANVAS = "Холст не загружен"
TITLE_STALE = "Схема изменилась"        # старая модалка, пункт её не трогает


# ── данные ───────────────────────────────────────────────────────────────

def _graph(n_nodes: int, text: bool = False) -> dict:
    """Граф из n_nodes узлов в цепочку: число узлов и есть метка происхождения."""
    nodes, links = [], []
    for i in range(n_nodes):
        x = 60.0 + 100 * i
        nodes.append({"id": f"n{i}", "type": "equipment",
                      "class_name": "zadvizhka", "centroid": [80.0, x],
                      "bbox": [x - 12, 68, x + 12, 92]})
    for i in range(n_nodes - 1):
        links.append({"id": f"e{i}", "source": f"n{i}", "target": f"n{i + 1}",
                      "source_point": [60.0 + 100 * i, 80.0],
                      "target_point": [60.0 + 100 * (i + 1), 80.0],
                      "waypoints": []})
    blocks = [{"id": "t0", "text": "ЗД-1", "bbox": [66.0, 40.0, 106.0, 58.0],
               "confidence": 0.93}] if text else []
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [H, W]},
            "nodes": nodes, "links": links,
            "text_blocks": blocks, "bindings": []}


GRAPH_SAVED = json.dumps(_graph(N_SAVED)).encode()
GRAPH_RAW = json.dumps(_graph(N_RAW)).encode()
GRAPH_OTHER = json.dumps(_graph(N_OTHER)).encode()
#: источник холста С ПОДПИСЯМИ — ими видно, что импорт текста натворил
GRAPH_SAVED_TEXT = json.dumps(_graph(N_SAVED, text=True)).encode()


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


@pytest.fixture(scope="module")
def canvas_of(qapp, raster, tmp_path_factory):
    """Готовый холст, собранный из заданного графа-источника.

    Собран тем же кодом, что и в бою (`_pretransform_to_canvas`), поэтому его
    штамп источника настоящий: холст из GRAPH_SAVED свежий, из GRAPH_RAW —
    устаревший относительно GRAPH_SAVED.

    ⚠ Третий, `stale_both`, собран из GRAPH_OTHER и потому устарел
    относительно ОБОИХ графов. Без него параметр «устаревший холст» в паре
    с отказом graph_validated декоративен: холст из GRAPH_RAW фолбэку
    РОВЕСНИК (фолбэк — тот же GRAPH_RAW), устаревшим не считается и зелёный
    даёт даже нетронутый код.
    """
    from ui.tabs.base_graph_tab import _pretransform_to_canvas

    root = tmp_path_factory.mktemp("canvas")
    png = root / "original.png"
    png.write_bytes(raster)

    def build(source: bytes, name: str) -> bytes:
        src = root / f"{name}.json"
        src.write_bytes(source)
        out = root / f"{name}_canvas.json"
        assert _pretransform_to_canvas(src, png, out), "холст не собрался"
        return out.read_bytes()

    return {"fresh": build(GRAPH_SAVED, "fresh"),
            "stale": build(GRAPH_RAW, "stale"),
            "stale_both": build(GRAPH_OTHER, "stale_both"),
            "with_text": build(GRAPH_SAVED_TEXT, "with_text")}


# ── подставной сервер ────────────────────────────────────────────────────

class FakeAPI:
    """Отдаёт заданные байты; чего нет — `APIError` 404; заданное — своей ошибкой."""

    def __init__(self, blobs, failures=None):
        self.blobs = dict(blobs)
        self.failures = dict(failures or {})
        self.calls = []
        self.uploads = []          # что вкладка ЗАПИСАЛА обратно (пункт 1.x9)

    def download_artifact(self, uid, artifact_type, dest_path):
        self.calls.append(artifact_type)
        exc = self.failures.get(artifact_type)
        if exc is not None:
            raise exc
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return dest_path

    def upload_canvas_graph(self, uid, path):
        self.uploads.append(("graph_canvas", Path(path).name))

    def upload_validated_graph(self, uid, path):
        self.uploads.append(("graph_validated", Path(path).name))


def _server(raster, *, saved=True, raw=True, canvas=None, failures=None):
    """Сервер по описанию: что на нём лежит и что из этого отказывает."""
    blobs = {"original_image": raster}
    if saved:
        blobs["graph_validated"] = GRAPH_SAVED
    if raw:
        blobs["graph_json"] = GRAPH_RAW
    if canvas is not None:
        blobs["graph_canvas"] = canvas
    return FakeAPI(blobs, failures)


# ── харнесс ──────────────────────────────────────────────────────────────

def _nodes_in(graph_path) -> int:
    """Сколько узлов в приземлившемся графе — по нему видно, ЧЕЙ он."""
    return len(json.loads(Path(graph_path).read_text(encoding="utf-8"))["nodes"])


def _download(api, tmp_dir, want_canvas):
    """Прогнать НАСТОЯЩИЙ загрузчик вкладки синхронно: (artifacts, error)."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.base_graph_tab import _graph_jobs

    dl = ArtifactDownloader(api, UID, tmp_dir, _graph_jobs(want_canvas=want_canvas))
    out = {}
    dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
    dl.error.connect(lambda m: out.__setitem__("error", m))
    dl.run()
    return out.get("artifacts"), out.get("error")


@pytest.fixture
def dialogs(monkeypatch):
    """Все модалки вкладки → список (title, text). Возврата ждать некому.

    `dialogs.answer` — что вернёт СЛЕДУЮЩИЙ диалог. По умолчанию `Cancel`:
    у пункта 1.x9 появился вопрос, разрешающий перезапись, и тест, который
    о нём не знает, не должен нечаянно ответить «да» за оператора.
    """

    class _Seen(list):
        answer = QMessageBox.StandardButton.Cancel

    seen = _Seen()

    def _rec(parent, title, text, *a, **kw):
        seen.append((title, text))
        return seen.answer

    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_rec))
    return seen


@pytest.fixture
def open_tab(qapp, monkeypatch):
    """Открыть графовую вкладку тем словарём, который отдал настоящий загрузчик.

    Поток снят: загрузчик крутится синхронно в этом же процессе, а его результат
    уходит во вкладку тем же слотом `_on_downloaded`, которым его доставляет бой.
    Шов «загрузчик → вкладка» при этом не подменён — через него и идёт проверка.
    """
    from ui.tabs.base_graph_tab import BaseGraphTab

    opened = []

    def _open(cls, api):
        monkeypatch.setattr(
            BaseGraphTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = cls(UID, "проба 1.23", api)
        opened.append(tab)
        artifacts, error = _download(api, tab.temp_dir, cls.USE_CANVAS)
        assert error is None, f"загрузчик увёл вкладку в ошибку: {error}"
        tab._on_downloaded(artifacts)
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


# =========================================================================
# Слой 1 — загрузчик: 404 против всего остального
# =========================================================================

def test_404_on_saved_graph_is_a_silent_fallback(raster, tmp_path):
    """Сохранённого графа нет (первый заход) — фолбэк законен и молчит."""
    artifacts, error = _download(_server(raster, saved=False), tmp_path, False)
    assert error is None
    assert "saved_graph_download_failed" not in artifacts
    assert _nodes_in(artifacts["graph_json"]) == N_RAW


@pytest.mark.parametrize("exc", [
    APIError("bad gateway", 502),
    APIError("Connection error: сеть недоступна", 0),
    OSError("диск полон"),
])
def test_non_404_on_saved_graph_marks_the_fallback(exc, raster, tmp_path):
    """Сохранённый граф МОГ быть — фолбэк взят вслепую, состояние неизвестно."""
    api = _server(raster, failures={"graph_validated": exc})
    artifacts, error = _download(api, tmp_path, False)
    assert error is None
    assert artifacts["saved_graph_download_failed"] is True
    assert _nodes_in(artifacts["graph_json"]) == N_RAW


def test_404_on_canvas_leaves_no_flag(raster, tmp_path):
    """Холста нет (в «Ручной правке» ещё не были) — пересборка законна и молчит."""
    artifacts, error = _download(_server(raster), tmp_path, True)
    assert error is None
    assert "graph_canvas" not in artifacts
    assert "canvas_download_failed" not in artifacts


def test_5xx_on_canvas_marks_state_unknown(raster, tmp_path):
    """Отказ сервера на холсте ≠ «холста нет»: цена ошибки — ручная раскладка."""
    api = _server(raster, canvas=b"{}", failures={
        "graph_canvas": APIError("bad gateway", 502)})
    artifacts, error = _download(api, tmp_path, True)
    assert error is None
    assert "graph_canvas" not in artifacts
    assert artifacts["canvas_download_failed"] is True


def test_canvas_non_api_error_still_stops_the_tab(raster, tmp_path):
    """Сторож 0.5: не-APIError на холсте НЕ глотается — вкладка уходит в ошибку."""
    api = _server(raster, canvas=b"{}", failures={"graph_canvas": OSError("диск полон")})
    artifacts, error = _download(api, tmp_path, True)
    assert artifacts is None
    assert error


def test_saved_graph_and_canvas_both_5xx_are_two_separate_flags(raster, tmp_path):
    """Две половины дефекта независимы — и отмечаются независимо."""
    api = _server(raster, canvas=b"{}", failures={
        "graph_validated": APIError("bad gateway", 502),
        "graph_canvas": APIError("bad gateway", 502)})
    artifacts, error = _download(api, tmp_path, True)
    assert error is None
    assert artifacts["saved_graph_download_failed"] is True
    assert artifacts["canvas_download_failed"] is True


# =========================================================================
# Слой 2 — вкладка: что видит оператор
# =========================================================================

def _titles(dialogs):
    return [title for title, _ in dialogs]


def _nodes_in_editor(tab):
    return len(tab._editor.nodes)


def test_saved_graph_present_opens_silently(open_tab, dialogs, raster):
    """Всё на месте: оператор видит СВОЙ граф и ни одной модалки."""
    tab = open_tab(_simple(), _server(raster))
    assert _titles(dialogs) == []
    assert _nodes_in_editor(tab) == N_SAVED
    assert _nodes_in_editor(tab) != N_RAW


def test_saved_graph_404_opens_silently(open_tab, dialogs, raster):
    """Сохранённого графа нет — исходный граф без единой модалки, это норма."""
    tab = open_tab(_simple(), _server(raster, saved=False))
    assert _titles(dialogs) == []
    assert _nodes_in_editor(tab) == N_RAW
    assert _nodes_in_editor(tab) != N_SAVED


def test_saved_graph_5xx_warns_the_operator(open_tab, dialogs, raster):
    """Ядро половины 1: открыт ЧУЖОЙ граф, и оператор об этом предупреждён."""
    tab = open_tab(_simple(), _server(
        raster, failures={"graph_validated": APIError("bad gateway", 502)}))
    assert _nodes_in_editor(tab) == N_RAW          # в редакторе исходный граф…
    assert _nodes_in_editor(tab) != N_SAVED        # …а не сохранённый
    assert _titles(dialogs) == [TITLE_GRAPH]
    assert "затрёт" in dialogs[0][1], "не сказано главное — сохранение затрёт"


def test_saved_graph_5xx_keeps_the_fresh_canvas(open_tab, dialogs, raster,
                                               canvas_of):
    """Половина 1 живёт в базовом классе — «Ручная правка» предупреждает так же.

    ⭐ Здесь же заперта вторая находка 1.23 (замер §68.3), ИСПРАВЛЕННАЯ
    пунктом 1.x9. Было: штамп холста сверялся с ФОЛБЭКОМ, поэтому свежий холст
    объявлялся устаревшим и пересобирался — оператор терял ручную раскладку,
    а вторая модалка («Схема изменилась») называла причину, которой не было.
    1.23 запер это как ИЗМЕРЕННЫЙ ФАКТ (порядок двух модалок), потому что
    ветка `if saved:` не была его адресом. Теперь замок стоит на верном
    поведении: источник не прочитан — судить о холсте нечем, холст остаётся
    оператору, а модалка ровно одна и правдивая.
    """
    tab = open_tab(_advanced(), _server(
        raster, canvas=canvas_of["fresh"],
        failures={"graph_validated": APIError("bad gateway", 502)}))
    assert _titles(dialogs) == [TITLE_GRAPH]       # одна модалка, и она правдива
    assert TITLE_STALE not in _titles(dialogs)
    assert _nodes_in_editor(tab) == N_SAVED        # у оператора СВОЙ холст…
    assert _nodes_in_editor(tab) != N_RAW          # …а не пересобранный из фолбэка


@pytest.mark.parametrize("canvas_kind", ["fresh", "stale_both"])
def test_saved_graph_5xx_never_rebuilds_the_canvas(canvas_kind, open_tab,
                                                   dialogs, raster, canvas_of):
    """Источник не прочитан → холст не пересобирается НИ В ОДНОМ случае.

    Пересборка оставляет след — `graph_1920.json` во временном каталоге
    вкладки; его отсутствие и есть «холст оператора не тронут». УСТАРЕВШИЙ
    холст здесь тоже остаётся: судить о нём нечем, а цена ложного «устарел» —
    выброшенная ручная раскладка (та же логика, что у `contours_download_failed`,
    пункт 0.5).
    """
    tab = open_tab(_advanced(), _server(
        raster, canvas=canvas_of[canvas_kind],
        failures={"graph_validated": APIError("bad gateway", 502)}))
    assert not (tab.temp_dir / "graph_1920.json").exists()
    assert _titles(dialogs) == [TITLE_GRAPH]


def _canvas_text(tab) -> int:
    """Сколько подписей осталось на холсте вкладки (файл, а не сцена)."""
    canvas = json.loads(
        (tab.temp_dir / "graph_canvas.json").read_text(encoding="utf-8"))
    return len(canvas.get("text_blocks") or [])


def test_saved_graph_5xx_does_not_wipe_the_labels_on_the_canvas(
        open_tab, dialogs, raster, canvas_of):
    """Оставить холст мало — его нельзя ещё и опрашивать по фолбэку.

    `import_text` при источнике БЕЗ подписей чистит холст досуха
    (`modules/graph/core/text_import.py`: `text_blocks = []`, `bindings = []`),
    а фолбэк `graph_json` — граф ДО OCR, подписей в нём нет по определению.
    То есть «холст сохранён, но подписи с него стёрты фолбэком» — та же потеря
    работы оператора, только тише.
    """
    tab = open_tab(_advanced(), _server(
        raster, canvas=canvas_of["with_text"],
        failures={"graph_validated": APIError("bad gateway", 502)}))
    assert _canvas_text(tab) == 1


def test_read_source_still_imports_its_text_into_the_canvas(
        open_tab, dialogs, raster, canvas_of):
    """Порог с другой стороны: импорт НЕ выключен — при прочитанном источнике
    холст по-прежнему берёт текст из него (в GRAPH_SAVED подписей нет, и холст
    честно остаётся без них)."""
    tab = open_tab(_advanced(), _server(raster, canvas=canvas_of["with_text"]))
    assert _canvas_text(tab) == 0


def test_canvas_404_rebuilds_silently(open_tab, dialogs, raster):
    """Холста нет — пересборка законна, модалок нет."""
    open_tab(_advanced(), _server(raster))
    assert _titles(dialogs) == []


def test_canvas_5xx_warns_the_operator(open_tab, dialogs, raster, canvas_of):
    """Ядро половины 2: холст пересобран вслепую, и оператор об этом предупреждён."""
    open_tab(_advanced(), _server(
        raster, canvas=canvas_of["fresh"],
        failures={"graph_canvas": APIError("bad gateway", 502)}))
    assert _titles(dialogs) == [TITLE_CANVAS]
    assert "затрёт" in dialogs[0][1], "не сказано главное — сохранение затрёт"


def test_fresh_canvas_opens_silently(open_tab, dialogs, raster, canvas_of):
    """Холст на месте и свежий — ни старой модалки, ни новой."""
    open_tab(_advanced(), _server(raster, canvas=canvas_of["fresh"]))
    assert _titles(dialogs) == []


@pytest.mark.parametrize("cls_of,failing,said", [
    (_simple, "graph_validated", "открыт исходный граф"),
    (_advanced, "graph_canvas", "холст пересобран заново"),
])
def test_failure_leaves_a_warning_in_the_client_log(cls_of, failing, said,
                                                    open_tab, dialogs, raster,
                                                    canvas_of, caplog):
    """Д2: модалка живёт на экране, а разбираться потом будут по файлу лога.

    Требуется строка САМОЙ ВКЛАДКИ — про последствие, а не только про отказ
    скачивания: у холста рядом стоит строка загрузчика с тем же именем
    артефакта, и без второго условия она сдала бы экзамен за вкладку
    (PROTOCOL §5 про соседа, который делает работу проверяемого).
    """
    import logging

    server = _server(raster, canvas=canvas_of["fresh"],
                     failures={failing: APIError("bad gateway", 502)})
    with caplog.at_level(logging.WARNING):
        open_tab(cls_of(), server)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert [m for m in warnings if failing in m and said in m], warnings


def test_stale_canvas_still_shows_its_own_modal(open_tab, dialogs, raster,
                                                canvas_of):
    """Сторож старой ветки: «Схема изменилась» осталась ровно одна и та же."""
    open_tab(_advanced(), _server(raster, canvas=canvas_of["stale"]))
    assert _titles(dialogs) == [TITLE_STALE]
    assert TITLE_CANVAS not in _titles(dialogs)


# =========================================================================
# Слой 3 — запись: предупреждение обязано МЕШАТЬ (пункт 1.x9)
# =========================================================================
#
# 1.23 закрыл ВИДИМОСТЬ отказа и границу назвал прямо: запрет записи в букву
# его гейта не входил. Замер остатка: после слепой пересборки первая же запись
# уходила на сервер и затирала холст, который прочитать не удалось.
#
# ⚠ Путь записи — `BaseGraphTab._save_graph`, а НЕ Ctrl+S: горячая клавиша
# редактора зовёт локальный `editor.save_graph` (`base_graph_editor.py:1592`,
# это пункт 1.19). Через `_save_graph` идут все три боевых входа на сервер:
# кнопка 💾 (`base_graph_tab.py:806`), «Подтвердить» (`_on_confirm`)
# и автосохранение раз в 120 с (`ui/services/autosave.py`, включено по умолчанию).


def test_clean_open_saves_without_a_question(open_tab, dialogs, raster,
                                             canvas_of):
    """Контроль с другой стороны: всё скачалось — запись идёт молча."""
    api = _server(raster, canvas=canvas_of["fresh"])
    tab = open_tab(_advanced(), api)
    assert tab._save_graph() is True
    assert api.uploads == [("graph_canvas", "graph_canvas.json")]
    assert _titles(dialogs) == []


def test_canvas_404_saves_without_a_question(open_tab, dialogs, raster):
    """404 = холста законно нет (первый заход): затирать нечего, вопроса нет."""
    api = _server(raster)
    tab = open_tab(_advanced(), api)
    assert tab._save_graph() is True
    assert api.uploads == [("graph_canvas", "graph_canvas.json")]
    assert _titles(dialogs) == []


def test_canvas_5xx_does_not_let_the_first_save_through(open_tab, dialogs,
                                                        raster, canvas_of):
    """Ядро 1.x9: холст собран вслепую — первая запись на сервер НЕ уходит."""
    api = _server(raster, canvas=canvas_of["fresh"],
                  failures={"graph_canvas": APIError("bad gateway", 502)})
    tab = open_tab(_advanced(), api)
    assert _titles(dialogs) == [TITLE_CANVAS]      # предупреждение при открытии
    dialogs.clear()

    assert tab._save_graph() is False
    assert api.uploads == [], "серверный холст затёрт при отказе оператора"
    assert len(dialogs) == 1, "запись отменена молча — оператор не понял, почему"
    assert "затрёт" in dialogs[0][1]


def test_canvas_5xx_save_goes_through_after_an_explicit_yes(open_tab, dialogs,
                                                            raster, canvas_of):
    """Запрет снимается только явным «да» — и спрашивается ровно один раз."""
    api = _server(raster, canvas=canvas_of["fresh"],
                  failures={"graph_canvas": APIError("bad gateway", 502)})
    tab = open_tab(_advanced(), api)
    dialogs.clear()
    dialogs.answer = QMessageBox.StandardButton.Yes

    assert tab._save_graph() is True
    assert api.uploads == [("graph_canvas", "graph_canvas.json")]
    assert len(dialogs) == 1

    dialogs.clear()
    assert tab._save_graph() is True               # второй save — уже без вопроса
    assert len(api.uploads) == 2
    assert _titles(dialogs) == []


def test_saved_graph_5xx_does_not_let_the_first_save_through(open_tab, dialogs,
                                                             raster):
    """Та же половина у графа: открыт исходный — запись ждёт «да» оператора.

    Модалка 1.23 обещала «сохранение затрёт сохранённый граф на сервере»
    и не мешала ему это сделать. «Проверка схемы» пишет в `graph_validated`
    (`_canvas_mode` False), поэтому под угрозой именно он.
    """
    api = _server(raster,
                  failures={"graph_validated": APIError("bad gateway", 502)})
    tab = open_tab(_simple(), api)
    assert _titles(dialogs) == [TITLE_GRAPH]
    dialogs.clear()

    assert tab._save_graph() is False
    assert api.uploads == [], "серверный graph_validated затёрт исходным графом"
    assert len(dialogs) == 1


def test_the_lock_is_addressed_to_its_own_artifact(open_tab, dialogs, raster,
                                                   canvas_of):
    """Запрет адресный: отказ ГРАФА не запирает запись в ПРОЧИТАННЫЙ холст.

    «Ручная правка» пишет в `graph_canvas`, а он скачался — затирать нечужое
    незачем. Без этой границы любой сбой запирал бы вкладку целиком.
    """
    api = _server(raster, canvas=canvas_of["fresh"],
                  failures={"graph_validated": APIError("bad gateway", 502)})
    tab = open_tab(_advanced(), api)
    dialogs.clear()

    assert tab._save_graph() is True
    assert api.uploads == [("graph_canvas", "graph_canvas.json")]
    assert _titles(dialogs) == []
