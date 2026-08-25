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

── Пункт 1.x10 (нижняя половина файла) ──────────────────────────────────
Второй слой той же вкладки: политика `swallow` у НЕОБЯЗАТЕЛЬНЫХ заданий.
1.x8 запер её здесь характеризационным тестом КАК ФАКТ («у этой вкладки
`swallow` шире, чем у графовой») и передал архитектору строкой. 1.x10 этот
замок снял и заменил приёмкой: у необязательных заданий стоит `(APIError,)`,
как решил 0.5, — отказ ЛОКАЛЬНОГО диска обязан уводить вкладку в ошибку,
а отказ сервера по-прежнему глотается молча.
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

# ── сохранённая работа оператора в `ocr_binding` (пункт 1.x10) ────────────
# Формат v2: подписи + отредактированные оператором блоки. Блоки приезжают
# ИМЕННО отсюда (`_on_download_finished`: `edited_blocks` перекрывает
# `ocr_result`), поэтому потеря этого артефакта = потеря обеих половин работы.
N_BINDINGS = 2                  # привязок в сохранённой работе оператора

_BLOCKS = [
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
        for i, b in enumerate(_BLOCKS)
    ],
    "edited_blocks": _BLOCKS,
}).encode()


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
        self.saves = []

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

    # -- запись (пункт 1.x10) ---------------------------------------------
    # Боевой сервер кладёт сохранённое ТУДА ЖЕ, откуда вкладка потом читает,
    # поэтому и подставной пишет в те же блобы: только так видно, что первое
    # сохранение сделало с работой оператора.

    def _store(self, name, path):
        self.blobs[name] = Path(path).read_bytes()
        self.saves.append(name)
        return {}

    def save_ocr_binding(self, uid, path):
        return self._store("ocr_binding", path)

    def save_ocr_validation(self, uid, path):
        return self._store("ocr_validation", path)

    def upload_validated_graph(self, uid, path):
        return self._store("graph_validated", path)


def _server(raster, *, saved=True, raw=True, binding=None, failures=None):
    """Сервер по описанию: что на нём лежит и что из этого отказывает."""
    blobs = {"original_image": raster, "ocr_result": OCR_RESULT}
    if saved:
        blobs["graph_validated"] = GRAPH_SAVED
    if raw:
        blobs["graph_json"] = GRAPH_RAW
    if binding is not None:
        blobs["ocr_binding"] = binding
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
    # Вопрос «да / отмена» с пункта 5.3 собирается своими кнопками (русскими),
    # мимо статической двери `QMessageBox.question`, — подменяется отдельно.
    from ui.tabs.blind_overwrite import BlindOverwriteGuard
    monkeypatch.setattr(
        BlindOverwriteGuard, "_ask_yes_cancel",
        lambda self, title, text:
            _rec(self, title, text) == QMessageBox.StandardButton.Yes)
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

    def _open(api, *, allow_error=False):
        """`allow_error=True` → вернуть `(вкладка_или_None, error)`.

        Пункт 1.x10: отказ загрузки — законный исход, и проверять надо ровно
        то, что вкладка при нём НЕ открывается; поэтому сессия обязана уметь
        пережить `error`, а не падать на нём своим `assert`.
        """
        monkeypatch.setattr(
            OcrBindingTab, "_start_download",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = OcrBindingTab(UID, "проба 1.x8", api)
        opened.append(tab)
        artifacts, error = _download(api, tab.temp_dir)
        if allow_error and error is not None:
            return None, error
        assert error is None, f"загрузчик увёл вкладку в ошибку: {error}"
        tab._on_download_finished(artifacts)
        return (tab, None) if allow_error else tab

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


# =========================================================================
# Пункт 1.x10 — политика `swallow` у необязательных заданий этой вкладки
# =========================================================================
# ⛔ Здесь стоял характеризационный `test_swallow_of_optional_jobs_is_untouched`
# (1.x8): он запирал КАК ФАКТ то, что у необязательных заданий этой вкладки
# `swallow` дефолтный `(Exception,)`, и сам требовал переписать себя тем
# пунктом, который возьмётся за различение 0.5. Это он и есть.
#
# Инвариант 0.5, к которому приводится вкладка: отказ СЕРВЕРА у необязательного
# артефакта терпим (его может законно не быть), отказ ЛОКАЛЬНОГО ДИСКА — нет.
# Граница проходит ровно по `APIError`: `APIClient._request_raw` заворачивает
# в него и HTTP-коды, и сетевые сбои (`api_client.py:199-204`), а `write_bytes`
# в `dest` лежит ЗА этой обёрткой (`:387-389`, `:673-676`, `:697-700`) — то есть
# не-`APIError` здесь означает «локальная запись не удалась».


def _bindings_on_server(api) -> int:
    """Сколько привязок сейчас лежит на сервере — по ним видно, чья это работа."""
    stored = json.loads(api.blobs["ocr_binding"].decode())
    return len(stored["bindings"])


# ── половина 1: цена отказа (сценарий, уровень дефекта) ──────────────────

def test_a_save_reaches_the_server_and_the_reader_sees_it(
        raster, open_tab, dialogs):
    """Контроль честности следующего теста: запись ДОХОДИТ и ВИДНА.

    ⛔ Первая редакция проверяла «после сохранения на сервере 2 привязки» —
    и была ДЕКОРАТИВНОЙ: столько же лежало там до сохранения, поэтому зонд
    «обезвредить запись в подставном сервере» её не покрасил (родня §76.7).
    Экзамен сдаёт только РАЗНИЦА: оператор снимает одну привязку, и она
    обязана исчезнуть у читателя.
    """
    api = _server(raster, binding=BINDING_SAVED)
    tab = open_tab(api)

    assert len(tab._bindings) == N_BINDINGS, "вкладка не подняла работу оператора"
    tab.editor.set_bindings(tab._bindings[:1])        # оператор снял одну
    assert tab._save_binding() is True
    assert "ocr_binding" in api.saves, "сохранение до сервера не дошло"
    assert _bindings_on_server(api) == 1, \
        "запись не видна читателю — на таком стенде «работа цела» ничего не значит"


def test_disk_failure_never_costs_the_operator_the_saved_binding(
        raster, open_tab, dialogs):
    """Сценарий целиком: отказ диска → работа оператора на сервере цела.

    До правки: `OSError` глотался, вкладка открывалась с ПУСТЫМИ привязками,
    и первое же сохранение писало эту пустоту поверх работы оператора
    (`_save_binding` зовёт `save_ocr_binding` БЕЗУСЛОВНО, `:1573`). Путь
    достижим и без оператора: `ui/services/autosave.py:_SAVE_METHODS` зовёт
    у этой вкладки тот же `_save_binding` по таймеру (120 с, включено
    по умолчанию).
    """
    api = _server(raster, binding=BINDING_SAVED)
    api.failures["ocr_binding"] = OSError("диск отвалился")

    tab, error = open_tab(api, allow_error=True)
    if tab is not None:                       # вкладка всё же открылась —
        tab._save_binding()                   # ... вот чем кончается её работа

    assert _bindings_on_server(api) == N_BINDINGS, (
        "сохранённые привязки оператора затёрты из-за отказа ЛОКАЛЬНОГО диска")
    assert error is not None, "вкладка обязана уйти в отказ, а не работать вслепую"


# ── половина 2: политика на каждом необязательном задании ────────────────

def test_disk_failure_on_binding_drives_the_tab_to_error(raster, tmp_path):
    """`ocr_binding`: не-`APIError` не глотается — вкладка уходит в ошибку."""
    api = _server(raster, binding=BINDING_SAVED)
    api.failures["ocr_binding"] = OSError("диск отвалился")

    artifacts, error = _download(api, tmp_path / "e")

    assert artifacts is None
    assert error is not None and "диск отвалился" in error


def test_disk_failure_on_validation_drives_the_tab_to_error(raster, tmp_path):
    """`ocr_validation`: та же политика — задание необязательное, диск нет."""
    api = _server(raster)
    api.blobs["ocr_validation"] = json.dumps({"classifications": []}).encode()
    api.failures["ocr_validation"] = OSError("нет места на диске")

    artifacts, error = _download(api, tmp_path / "f")

    assert artifacts is None
    assert error is not None and "нет места на диске" in error


def test_disk_failure_on_the_whole_coco_chain_drives_the_tab_to_error(
        raster, tmp_path):
    """Цепочка coco: политику судит ПОСЛЕДНИЙ кандидат — валить обоих.

    ⚠ Свойство общего `_run_job` (0.5), не этой вкладки: `swallow` сверяется
    с исключением ПОСЛЕДНЕГО кандидата цепочки, поэтому отказ диска у
    предпочтённого, прикрытый 404 у фолбэка, остаётся глотаемым. Здесь это
    зафиксировано как известная граница, а не как требование.
    """
    api = _server(raster)
    api.blobs["coco_validated"] = b"{}"
    api.blobs["coco_predicted"] = b"{}"
    api.failures["coco_validated"] = OSError("диск отвалился")
    api.failures["coco_predicted"] = OSError("диск отвалился")

    artifacts, error = _download(api, tmp_path / "g")

    assert artifacts is None
    assert error is not None


def test_every_optional_job_carries_the_policy(raster):
    """Политика не теряется при копировании задания — это и есть её замок.

    Абсолютное требование ко ВСЕМ необязательным заданиям вкладки, а не к трём
    известным сегодня: `swallow` копируется вместе с заданием, и потерять его
    в четвёртом — ровно тот дефект, который чинит этот пункт.
    """
    from ui.tabs.ocr_binding_tab import _ARTIFACTS

    optional = [j for j in _ARTIFACTS if not j.required]
    assert len(optional) == 3, "изменился состав заданий — политику пересмотреть"
    assert all(j.swallow == (APIError,) for j in optional), \
        [j.fetches[0].source for j in optional if j.swallow != (APIError,)]


# =========================================================================
# Пункт 1.x12 — отказ СЕРВЕРА у привязок оператор тоже видит (механизм 1.23)
# =========================================================================
# ⛔ Здесь стоял `test_server_failure_on_binding_is_still_swallowed_silently`
# (1.x10): он запирал КАК ФАКТ то, что 5xx у `ocr_binding` неотличим от 404, —
# и сам назвал лекарство: не `swallow` (граница по `APIError` его не берёт,
# 5xx в него ЗАВЁРНУТ), а `failure_key`, механизм 1.23. Это он и есть.
#
# Различение то же, что у графа в §5.1: 404 = сохранённых привязок законно нет
# (первый заход) — молчим; любой другой отказ сервера = они МОГЛИ лежать,
# а `_save_binding` пишет привязки обратно безусловно, то есть вкладка,
# открытая без них, затрёт работу оператора первым же сохранением или тиком
# автосохранения.

#: заголовок предупреждения о непрочитанных привязках
TITLE_BINDING = "Сохранённые привязки не загружены"


def test_absent_binding_is_still_swallowed_silently(raster, open_tab, dialogs):
    """404 = привязок законно нет (первый заход) — ни ошибки, ни модалки."""
    tab = open_tab(_server(raster))

    assert tab._bindings == []
    assert dialogs == [], "404 — законный первый заход, пугать оператора нечем"


def test_server_failure_on_binding_warns_the_operator(
        raster, open_tab, dialogs):
    """5xx: вкладка открылась без привязок — и оператор ВИДИТ, чего лишился."""
    api = _server(raster, binding=BINDING_SAVED,
                  failures={"ocr_binding": APIError("gateway timeout", 504)})
    tab = open_tab(api)

    assert tab._bindings == [], "фолбэка у привязок нет — вкладка работает пустой"
    assert TITLE_BINDING in _titles(dialogs), (
        "оператор молча получил пустые привязки, а «Сохранить» затрёт ими "
        f"его собственные {N_BINDINGS}")


def test_binding_warning_names_the_cost_of_saving(raster, open_tab, dialogs):
    """Текст модалки обязан назвать ПОСЛЕДСТВИЕ, иначе предупреждение декоративно."""
    open_tab(_server(raster, binding=BINDING_SAVED,
                     failures={"ocr_binding": APIError("boom", 500)}))

    text = next(t for title, t in dialogs if title == TITLE_BINDING)
    assert "затрёт" in text, "модалка не говорит, чем грозит сохранение"


def test_binding_flag_marks_only_non_404_failures(raster, tmp_path):
    """Флаг ставится ровно на «состояние неизвестно», а не на любой отказ."""
    key = "binding_download_failed"

    healthy, _ = _download(_server(raster, binding=BINDING_SAVED), tmp_path / "h")
    missing, _ = _download(_server(raster), tmp_path / "i")
    broken, _ = _download(
        _server(raster, binding=BINDING_SAVED,
                failures={"ocr_binding": APIError("boom", 503)}),
        tmp_path / "j")

    assert healthy.get(key) is None
    assert "binding" in healthy, "здоровый сервер обязан отдать привязки"
    assert missing.get(key) is None, "404 — не отказ, а законное отсутствие"
    assert broken.get(key) is True


def test_failed_binding_leaves_a_log_line(raster, open_tab, dialogs, caplog):
    """Д2: отказ виден не только оператору, но и в логе клиента."""
    with caplog.at_level(logging.WARNING, logger="ui.tabs.ocr_binding_tab"):
        open_tab(_server(raster, binding=BINDING_SAVED,
                         failures={"ocr_binding": APIError("boom", 502)}))

    lines = [r.getMessage() for r in caplog.records
             if r.name == "ui.tabs.ocr_binding_tab" and r.levelno >= logging.WARNING]
    assert any("binding" in m for m in lines), \
        f"в логе вкладки нет строки об отказе привязок: {lines}"


def test_healthy_server_shows_no_binding_warning(raster, open_tab, dialogs):
    """Порог с другой стороны: привязки доехали — ни одной модалки."""
    tab = open_tab(_server(raster, binding=BINDING_SAVED))

    assert len(tab._bindings) == N_BINDINGS
    assert dialogs == []


# =========================================================================
# Пункт 1.x14 — запрет слепой перезаписи доехал и до вкладки привязок
# =========================================================================
# 1.x12 записал остаток прямо: «предупреждение НЕ МЕШАЕТ записи», потому что
# запрет 1.x9 живёт в `BaseGraphTab._save_graph`, а эта вкладка от него не
# наследуется (`OcrBindingTab(AppearanceMixin, QWidget)`). То есть вкладка
# привязок повторяла ровно тот дефект, который 1.x9 закрыл у графовых:
# оператор ВИДИТ, что открыл не свою работу, и первый же save её затирает.
#
# ⚠ Артефакта здесь ДВА, а не один (замер §89а): `_save_binding` пишет и
# привязки (`save_ocr_binding`), и граф (`upload_validated_graph`). Дверь,
# закрытая только со стороны привязок, оставила бы вторую половину настежь.


@pytest.fixture
def answer(monkeypatch):
    """Ответ оператора на вопрос о слепой перезаписи + журнал вопросов.

    Возвращает список заголовков заданных вопросов; ответ настраивается
    полем `.reply`. Утверждение о ФАКТЕ вызова, а не таймаут (`PROTOCOL §5`).
    """
    class _Answer(list):
        #: ответ по умолчанию
        reply = QMessageBox.StandardButton.Cancel
        def __init__(self):
            super().__init__()
            self._queue = iter(())

        def answer_in_order(self, *replies):
            self._queue = iter(replies)

        def next_reply(self):
            return next(self._queue, self.reply)

    asked = _Answer()

    def _question(parent, title, text, *a, **kw):
        asked.append((title, text))
        return asked.next_reply()

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_question))
    return asked


def _unread_binding_server(raster):
    """Сервер, у которого привязки НЕ отдались (не 404): состояние неизвестно."""
    return _server(raster, binding=BINDING_SAVED,
                   failures={"ocr_binding": APIError("gateway timeout", 504)})


def _unread_graph_server(raster):
    """То же для графа: сохранённый не отдался, открыт исходный."""
    return _server(raster, binding=BINDING_SAVED,
                   failures={"graph_validated": APIError("boom", 500)})


def test_refused_blind_write_leaves_the_server_untouched(
        raster, open_tab, dialogs, answer):
    """Оператор сказал «нет» — на сервере остаётся ЕГО работа, а не пустота.

    ⛔ Утверждается РАЗНИЦА (`PROTOCOL §3`): до правки `_save_binding`
    возвращал True и клал на сервер 0 привязок вместо сохранённых там 2.
    """
    api = _unread_binding_server(raster)
    tab = open_tab(api)
    assert _bindings_on_server(api) == N_BINDINGS, "стенд начал не с работы оператора"

    assert tab._save_binding() is False
    assert answer, "запрета нет вовсе — вопрос не задан"
    assert _bindings_on_server(api) == N_BINDINGS, (
        "слепая запись прошла: работа оператора на сервере затёрта пустотой")
    assert "ocr_binding" not in api.saves


def test_refused_blind_write_blocks_the_graph_half_too(
        raster, open_tab, dialogs, answer):
    """Вторая половина двери: непрочитанный ГРАФ запирает ту же запись.

    `_save_binding` пишет `graph_validated` шагом 5, и до правки этот путь
    был открыт даже при закрытой двери привязок.
    """
    api = _unread_graph_server(raster)
    tab = open_tab(api)

    assert tab._save_binding() is False
    assert any("граф" in text for _, text in answer), (
        f"вопрос про граф не задан: {[t for _, t in answer]}")
    assert "graph_validated" not in api.saves, (
        "исходный граф записан поверх непрочитанного сохранённого")


def test_confirmed_blind_write_goes_through(raster, open_tab, dialogs, answer):
    """Обратная граница: «да» — решение оператора, запись обязана пройти."""
    api = _unread_binding_server(raster)
    tab = open_tab(api)
    answer.reply = QMessageBox.StandardButton.Yes

    assert tab._save_binding() is True
    assert "ocr_binding" in api.saves
    assert _bindings_on_server(api) == 0, (
        "оператор разрешил перезапись, а она не прошла")


def test_healthy_download_never_asks(raster, open_tab, dialogs, answer):
    """Порог с другой стороны: привязки доехали — вопроса нет вовсе."""
    api = _server(raster, binding=BINDING_SAVED)
    tab = open_tab(api)

    assert tab._save_binding() is True
    assert answer == [], f"вопрос задан на здоровом сервере: {[t for t, _ in answer]}"


def test_absent_binding_never_asks(raster, open_tab, dialogs, answer):
    """404 — привязок законно нет (первый заход), запирать нечего."""
    tab = open_tab(_server(raster))

    assert tab._save_binding() is True
    assert answer == [], "404 запер запись — первый заход стал невозможен"


def test_refusal_is_repeated_on_the_next_attempt(raster, open_tab, dialogs,
                                                 answer):
    """«Нет» запрет НЕ снимает: следующая попытка спрашивает снова.

    ⛔ Прогон ПОСЛЕ чужого действия оператора, а не с чистого листа
    (`PROTOCOL §3`): вторая попытка идёт по вкладке, у которой уже есть
    предыстория — один отказ и одно отменённое сохранение.
    """
    api = _unread_binding_server(raster)
    tab = open_tab(api)

    assert tab._save_binding() is False
    asked_once = len(answer)
    assert tab._save_binding() is False, "второй заход прошёл мимо запрета"
    assert len(answer) > asked_once, "вопрос больше не задают — запрет испарился"
    assert _bindings_on_server(api) == N_BINDINGS


def test_permission_given_once_is_not_asked_again(raster, open_tab, dialogs,
                                                  answer):
    """«Да» снимает запрет насовсем — иначе автосейв спросит через 120 с.

    Второе сохранение идёт ПОСЛЕ состоявшейся записи: проверяется путь,
    на котором `_unreadable_on_server` уже не в начальном состоянии.
    """
    api = _unread_binding_server(raster)
    tab = open_tab(api)
    answer.reply = QMessageBox.StandardButton.Yes

    assert tab._save_binding() is True
    asked_once = len(answer)
    assert tab._save_binding() is True
    assert len(answer) == asked_once, (
        "разрешение оператора не запомнено — вопрос вернулся")


def test_refusal_after_permission_for_the_other_artifact_grants_nothing(
        raster, open_tab, dialogs, answer):
    """Оба артефакта непрочитаны, «да» первому и «нет» второму → не записано НИЧЕГО.

    И разрешение первого не сохраняется: ничего не записано, значит и
    разрешения оператор не давал — следующая попытка спросит снова про оба.
    """
    api = _server(raster, binding=BINDING_SAVED,
                  failures={"ocr_binding": APIError("gateway timeout", 504),
                            "graph_validated": APIError("boom", 500)})
    tab = open_tab(api)
    answer.answer_in_order(QMessageBox.StandardButton.Yes,
                           QMessageBox.StandardButton.Cancel)

    assert tab._save_binding() is False
    assert len(answer) == 2, f"спросили не про оба артефакта: {len(answer)}"
    assert api.saves == [], f"при отказе что-то всё же записано: {api.saves}"
    assert tab._unreadable_on_server == {"ocr_binding", "graph_validated"}, (
        "разрешение засчитано при отменённом сохранении")


# =========================================================================
# Пункт 1.x12 — «⏳ OCR в процессе» на отказе, который повторами не лечится
# =========================================================================
# `_on_download_error` повторяет загрузку 10 раз по 5 с, показывая всё это время
# «⏳ OCR в процессе...». Для «OCR ещё не готов» это правда: артефакта пока нет,
# сервер отвечает 404, и следующая попытка его застанет. Для отказа ЛОКАЛЬНОГО
# диска это ложь о работе на ~50 секунд: ждать нечего, повторять нечего.
# Граница та же, что у `swallow` (1.x10): `APIError` = отказ сервера, может
# пройти; не-`APIError` = локальная запись, не пройдёт никогда.
#
# ── Пункт 1.x14 ──────────────────────────────────────────────────────────
# 1.x12 закрыл только не-`APIError` и записал остаток прямо: «отказ сервера
# может пройти со следующей попытки, и текст про OCR при нём остаётся прежним».
# Остаток и оказался дефектом: 5xx (и обрыв связи, и отказ прав) — это НЕ «OCR
# в процессе», а оператор видит именно эту фразу все ~50 секунд ретраев.
# Правда про ожидание есть ровно у ОДНОГО кода: 404 = артефакта пока нет,
# следующая попытка его застанет — так и написано в комментарии самого
# `_on_download_error`. Поэтому пункт трогает ТЕКСТ, а не повторы: повтор при
# шлюзовом отказе полезен и оставлен, лжёт не он.


@pytest.fixture
def retries(monkeypatch):
    """План повторов: миллисекунды, с которыми вкладка звала `QTimer.singleShot`.

    ⚠ Утверждение о ФАКТЕ вызова, а не таймаут (`PROTOCOL §5`): настоящий
    таймер в наборе либо висит, либо стреляет в чужой тест.
    """
    from ui.tabs import ocr_binding_tab as mod

    seen = []
    monkeypatch.setattr(mod.QTimer, "singleShot",
                        staticmethod(lambda ms, fn: seen.append(ms)))
    return seen


@pytest.fixture
def open_tab_failing(qapp, monkeypatch):
    """Довести вкладку до `_on_download_error` НАСТОЯЩИМ загрузчиком.

    Снят только поток: загрузчик тот же, сигнал тот же, слот тот же — то есть
    проверяется боевой шов «загрузчик → вкладка», а не подделка.
    """
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.ocr_binding_tab import OcrBindingTab, _ARTIFACTS

    opened = []

    def _open(api):
        monkeypatch.setattr(
            OcrBindingTab, "_start_download",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        tab = OcrBindingTab(UID, "проба 1.x12", api)
        opened.append(tab)

        # ровно то, что делает боевой `_start_download`, без переноса в поток
        tab._downloader = ArtifactDownloader(api, UID, tab.temp_dir, _ARTIFACTS)
        tab._downloader.finished.connect(tab._on_download_finished)
        tab._downloader.error.connect(tab._on_download_error)
        tab._downloader.run()
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()
        tab.deleteLater()
    qapp.processEvents()


def test_disk_failure_is_reported_at_once(raster, open_tab_failing, retries,
                                          dialogs):
    """Отказ диска: оператор видит ошибку СРАЗУ, без «⏳ OCR в процессе»."""
    api = _server(raster, binding=BINDING_SAVED,
                  failures={"ocr_binding": OSError("диск отвалился")})
    tab = open_tab_failing(api)

    assert retries == [], "повтор запланирован на отказе, который им не лечится"
    text = tab.loading_label.text()
    assert "OCR в процессе" not in text, f"ложь о работе OCR: {text!r}"
    assert "диск отвалился" in text, f"оператор не видит причину: {text!r}"


def test_unfinished_ocr_still_waits_and_says_so(raster, open_tab_failing,
                                                retries, dialogs):
    """Порог с другой стороны: OCR ещё не готов (404) — ждём и говорим правду."""
    api = _server(raster)
    del api.blobs["ocr_result"]                    # этап ещё не отработал
    tab = open_tab_failing(api)

    assert retries == [5000], "повтор обязан остаться: 404 застанет следующая попытка"
    assert "OCR в процессе" in tab.loading_label.text()


@pytest.mark.parametrize("exc,tail", [
    pytest.param(APIError("Internal Server Error", 500), "Internal Server Error",
                 id="server-500"),
    pytest.param(APIError("Bad Gateway", 502), "Bad Gateway", id="gateway-502"),
    pytest.param(APIError("Connection failed after 4 attempts", 0),
                 "Connection failed", id="network"),
    pytest.param(APIError("Not authenticated", 401), "Not authenticated",
                 id="auth-401"),
])
def test_server_failure_does_not_claim_ocr_is_running(
        raster, open_tab_failing, retries, dialogs, exc, tail):
    """Пункт 1.x14: отказ сервера — не «OCR в процессе», а названная причина.

    Перебор, а не пример: любой код, кроме 404, означает «сервер ответил
    и отказал», и ни при одном из них OCR не «в процессе». До правки все
    четыре клетки были неразличимы между собой И с честным ожиданием 404.
    """
    api = _server(raster, failures={"ocr_result": exc})
    tab = open_tab_failing(api)

    text = tab.loading_label.text()
    assert "OCR в процессе" not in text, f"ложь о работе OCR: {text!r}"
    assert tail in text, f"оператор не видит причину отказа: {text!r}"


@pytest.mark.parametrize("exc", [
    APIError("Internal Server Error", 500),
    APIError("Connection failed after 4 attempts", 0),
])
def test_server_failure_still_retries(raster, open_tab_failing, retries,
                                      dialogs, exc):
    """Обратная граница: правка трогает ТЕКСТ, а не повторы.

    Шлюзовой отказ и обрыв связи проходят со следующей попытки, поэтому
    повтор оставлен ровно таким, каким был, — и это проверяется, чтобы
    «починка» не съела заодно полезное поведение.
    """
    api = _server(raster, failures={"ocr_result": exc})
    open_tab_failing(api)

    assert retries == [5000], "повтор при отказе сервера пропал вместе с текстом"


def test_server_failure_leaves_a_log_line(raster, open_tab_failing, retries,
                                          dialogs, caplog):
    """Д2: причина отказа видна не только на экране, но и в логе клиента."""
    with caplog.at_level(logging.WARNING, logger="ui.tabs.ocr_binding_tab"):
        open_tab_failing(_server(
            raster, failures={"ocr_result": APIError("Bad Gateway", 502)}))

    lines = [r.getMessage() for r in caplog.records
             if r.name == "ui.tabs.ocr_binding_tab" and r.levelno >= logging.WARNING]
    assert any("Bad Gateway" in m for m in lines), \
        f"в логе вкладки нет строки об отказе загрузки: {lines}"


def test_retry_counter_is_visible_on_both_kinds_of_wait(
        raster, open_tab_failing, retries, dialogs):
    """Счётчик попыток остаётся на обеих ветках — оператор видит, сколько ждать."""
    api = _server(raster)
    del api.blobs["ocr_result"]                    # 404, честное ожидание
    tab = open_tab_failing(api)
    assert "1/10" in tab.loading_label.text()

    api2 = _server(raster, failures={"ocr_result": APIError("boom", 503)})
    tab2 = open_tab_failing(api2)
    assert "1/10" in tab2.loading_label.text()


# =========================================================================
# Пункт 1-41 — ТРЕТИЙ записываемый артефакт вкладки: `ocr_validation`
# =========================================================================
# Популяция «предупреждение есть, запрета нет» закрывалась 1.x9 и 1.x14 по
# ДВУМ артефактам (`ocr_binding`, `graph_validated`), а множество ЗАПИСЫВАЕМЫХ
# этой вкладкой шире: `_save_binding` шагом 6 (`:1848`) кладёт на сервер ещё
# и `ocr_validation`. Его задание (`_ARTIFACTS`, `:95`) `failure_key` не несёт,
# то есть 5xx и 404 неразличимы — и подтверждения оператора, сделанные в
# прошлый заход, уходят с сервера при первой же записи из этого.
#
# Цена условна и названа прямо: запись идёт только `if self._classifications`,
# то есть теряет работу тот оператор, который в этот заход поработал в режиме
# валидации хотя бы над одним блоком. Ровно этот сценарий здесь и проигран.
#
# ⛔ Второй класс отказа того же артефакта — «файл лёг, но не читается»:
# разбор `ocr_validation` стоит в СВОЁМ `try` (`:823-824`) и уходил строкой
# в лог, мимо общего обработчика загрузки, который поднимает модалку.

N_CLASSIFIED = 2                # подтверждений в сохранённой работе оператора

TITLE_VALIDATION = "Сохранённые подтверждения не загружены"


def _validation_blob(n: int) -> bytes:
    return json.dumps({
        "version": 1,
        "classifications": [
            {"block_idx": i, "block_type": "kks", "match_quality": "exact",
             "color": "green", "confirm_status": "confirmed",
             "original_text": f"10LBA1{i}", "corrected_text": f"10LBA1{i}"}
            for i in range(n)
        ],
    }).encode()


VALIDATION_SAVED = _validation_blob(N_CLASSIFIED)


def _classifications_on_server(api) -> int:
    stored = json.loads(api.blobs["ocr_validation"].decode())
    return len(stored["classifications"])


def _validation_server(raster, **failures):
    """Сервер, на котором лежат подтверждения оператора прошлого захода."""
    api = _server(raster, binding=BINDING_SAVED, failures=failures or None)
    api.blobs["ocr_validation"] = VALIDATION_SAVED
    return api


def _operator_classifies_one_block(tab):
    """Оператор поработал в режиме валидации: одно подтверждение.

    Идёт через `set_validation_results` — публичный вход, которым результаты
    валидации попадают в редактор; `_save_binding` шагом 6 забирает их именно
    оттуда (`if self.editor._validation_results`).
    """
    from modules.ocr_validation.result import (
        BlockClassification, BlockType, ConfirmStatus, MatchQuality,
        ValidationColor,
    )
    cl = BlockClassification(
        block_idx=0, block_type=BlockType.KKS,
        match_quality=MatchQuality.EXACT, color=ValidationColor.GREEN,
        confirm_status=ConfirmStatus.CONFIRMED,
    )
    cl.original_text = "10LBA10"
    tab.editor.set_validation_results([cl])


def test_saved_validation_arrives_when_server_is_healthy(raster, open_tab,
                                                         dialogs):
    """Контроль честности: подтверждения прошлого захода доезжают до вкладки."""
    tab = open_tab(_validation_server(raster))

    assert len(tab._classifications) == N_CLASSIFIED
    assert dialogs == []


def test_validation_save_reaches_the_server_and_the_reader_sees_it(
        raster, open_tab, dialogs, answer):
    """Контроль честности записи: экзамен сдаёт РАЗНИЦА, а не совпадение.

    ⛔ «После сохранения на сервере столько же» зелено и при обезвреженной
    записи (`PROTOCOL §3`, замеры §76.7 и §82.14). Поэтому оператор оставляет
    ОДНО подтверждение, и у читателя обязано остаться одно, а не два.
    """
    api = _validation_server(raster)
    tab = open_tab(api)
    assert _classifications_on_server(api) == N_CLASSIFIED

    _operator_classifies_one_block(tab)
    assert tab._save_binding() is True

    assert "ocr_validation" in api.saves, "запись подтверждений до сервера не дошла"
    assert _classifications_on_server(api) == 1, (
        "запись не видна читателю — на таком стенде «подтверждения целы» "
        "ничего не значит")


def test_missing_validation_is_silent(raster, open_tab, dialogs):
    """404 = подтверждений законно нет (первый заход) — молча."""
    tab = open_tab(_server(raster, binding=BINDING_SAVED))

    assert tab._classifications == []
    assert dialogs == [], "404 — законный первый заход, пугать оператора нечем"


def test_failed_validation_download_warns_and_locks_the_write(
        raster, open_tab, dialogs, answer):
    """5xx: оператор видит отказ, «нет» спасает подтверждения прошлого захода."""
    api = _validation_server(
        raster, ocr_validation=APIError("gateway timeout", 504))
    tab = open_tab(api)

    assert tab._classifications == [], "стенд не воспроизвёл отказ чтения"
    assert TITLE_VALIDATION in _titles(dialogs), (
        f"отказ 504 у подтверждений прошёл молча: {_titles(dialogs)}")

    _operator_classifies_one_block(tab)
    assert tab._save_binding() is False, "запись не заперта — вопроса не было"
    assert "ocr_validation" not in api.saves
    assert _classifications_on_server(api) == N_CLASSIFIED, (
        "подтверждения оператора затёрты из-за отказа СЕРВЕРА при чтении")


def test_unreadable_validation_file_warns_and_locks_the_write(
        raster, open_tab, dialogs, answer):
    """«Файл лёг, но не читается» — тот же запрет, другой класс отказа."""
    api = _validation_server(raster)
    api.blobs["ocr_validation"] = b'{"version": 1, "classifications": [{'

    tab = open_tab(api)

    assert tab._classifications == []
    assert TITLE_VALIDATION in _titles(dialogs), (
        f"нечитаемый ocr_validation.json прошёл молча: {_titles(dialogs)}")

    _operator_classifies_one_block(tab)
    assert tab._save_binding() is False
    assert "ocr_validation" not in api.saves


def test_operator_may_allow_the_validation_overwrite(raster, open_tab,
                                                     dialogs, answer):
    """Порог с другой стороны: «да» — решение оператора, запись проходит."""
    api = _validation_server(
        raster, ocr_validation=APIError("gateway timeout", 504))
    tab = open_tab(api)
    answer.reply = QMessageBox.StandardButton.Yes

    _operator_classifies_one_block(tab)
    assert tab._save_binding() is True
    assert _classifications_on_server(api) == 1, (
        "оператор разрешил перезапись, а она не состоялась")
