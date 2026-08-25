# -*- coding: utf-8 -*-
"""Блок 4 линии «Ручная правка + FXML», пункты 4.1 и 4.4 — контракт кнопки
«Авто-выравнивание».

**4.1 (К-1 / «НД5»).** За одной кнопкой стояло ДВА движка: на холсте после
раскладки — Э4-сглаживание, на холсте без раскладки — прежний
`auto_fix_graph`, медианное выравнивание цепочек без единой проверки коллизий
и без отката хода (замер §1.1 EDITOR_AFTER_LAYOUT: регрессия на 5 листах
корпуса из 7, ±120 px дрейфа). Надежда «ветка недостижима» ОПРОВЕРГНУТА
редтимом 2026-08-25 двумя путями, и оба закрываются здесь:

* **(а) холст, СОХРАНЁННЫЙ на сервер с `layout_applied=False`.** Такой холст
  проходит ветку «актуален» (`base_graph_tab.canvas_verdict` → `CANVAS_READY`)
  и грузится как есть — то есть кнопка попадает в опасный движок не на
  свежесобранном фолбэке, а на обычном рабочем холсте. Именно поэтому
  сценарный тест ниже поднимает вкладку БОЕВЫМ путём (`ArtifactDownloader` +
  `_on_downloaded`), а не подкладывает редактор руками: тест на свежесобранном
  объекте этого пути не проверяет (PROTOCOL §5). **Этот путь жив и после
  блока 8** — потому 4.1 и остаётся обязательным.
* **(б) ~~except-хвост загрузки: любой сбой — и в редакторе граф вообще без
  метки холста~~** — ⛔ путь СНЯТ блоком 8 (mefx-8, 2026-08-26): вкладка в
  холстовом режиме больше не собирает аварийный холст и не грузит сырой граф
  «как есть» — сбой подготовки и отсутствие холста дают экран отказа, а
  редактор не создаётся вовсе. Тест ниже переснят под новый контракт.

**4.4 (С8).** Сглаживание блокирующее (по 20 холстам корпуса медиана 2.8 с,
худший 6.6 с — замер `MEASUREMENTS §MEFX4Bб`), в фоновый поток не выносится (движок мутирует живую
модель и зовёт Qt-колбэки) — значит оператору полагается курсор ожидания и
честная строка. Плюс показ причины отказа: движок с `mefx-4a` считает три
ведра (`edit_smooth.REFUSAL_KINDS`), а показать их было некому.

⚠ Утверждения — о РАЗНИЦЕ, а не о совпадении с состоянием «до»: курсор
сверяется ВНУТРИ прогона движка против состояния после него, строка
«Сглаживание…» — тоже внутри, а вёдра снимаются с ЖИВОГО `smooth_canvas`,
а не с подменённого словаря.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402
from unittest.mock import MagicMock                              # noqa: E402

from PySide6.QtCore import Qt, QThread                           # noqa: E402
from PySide6.QtGui import QColor, QImage                         # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

IMG_W, IMG_H = 1920, 1080


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _validated():
    """Схема-источник: косая труба, сглаживать есть что."""
    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [IMG_H, IMG_W]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [100.0, 100.0], "bbox": [80.0, 80.0, 120.0, 120.0]},
            {"id": "b", "type": "connector", "class_name": "connector",
             "centroid": [400.0, 300.0], "bbox": None},
        ],
        "links": [{"id": "e1", "source": "a", "target": "b",
                   "source_point": [100.0, 120.0],
                   "target_point": [400.0, 300.0], "waypoints": []}],
        "text_blocks": [], "bindings": [],
    }


def _canvas(*, layout_applied, extra_node=None):
    """Холст, помеченный ТЕМ ЖЕ каноном, что и клиент (`canvas_state.stamp`).

    `extra_node` — узел, которого в graph_validated нет: он и доказывает, что
    редактор открыл СЕРВЕРНЫЙ холст, а не пересобранный фолбэком из источника.
    """
    from modules.graph.core import canvas_state

    graph = json.loads(json.dumps(_validated()))
    if extra_node is not None:
        graph["nodes"].append(extra_node)
    canvas_state.stamp(graph, _validated(),
                       layout_applied=layout_applied, operator_saved=True)
    return graph


SERVER_ONLY_NODE = {"id": "server_only", "type": "equipment",
                    "class_name": "unknow", "centroid": [800.0, 800.0],
                    "bbox": [780.0, 780.0, 820.0, 820.0]}


# ── боевой путь: вкладка собирается загрузчиком и `_on_downloaded` ────────

class _API:
    """Подставной сервер: отдаёт только то, что положили; остальное — 404."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)

    def get_stages(self, uid):
        """Стадий нет: у этой схемы задача раскладки не заводилась.

        Экран отказа блока 8 спрашивает их, чтобы отличить «пересчитывается»
        от «задачи нет и не будет»; пустой список = вторая ветка (дверь).
        """
        return []

    def download_artifact(self, uid, artifact_type, dest_path):
        from ui.services.api_client import APIError
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest


def _blobs(tmp_path, canvas):
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path / "raster.png"
    assert img.save(str(png))
    blobs = {"original_image": png.read_bytes(),
             "graph_validated": json.dumps(_validated()).encode("utf-8")}
    if canvas is not None:
        blobs["graph_canvas"] = json.dumps(canvas).encode("utf-8")
    return blobs


@pytest.fixture
def open_tab(qapp, monkeypatch, tmp_path):
    """Вкладка «Ручная правка», собранная тем же слотом, что в бою."""
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.tabs.base_graph_tab import BaseGraphTab, _graph_jobs

    opened = []

    def _open(canvas, *, expect_editor=True):
        monkeypatch.setattr(
            BaseGraphTab, "_download_artifacts",
            lambda self: setattr(self, "_download_thread", QThread(self)))
        api = _API(_blobs(tmp_path, canvas))
        tab = AdvancedGraphTab("mefx4b-uid", "проба 4.1", api)
        opened.append(tab)

        out = {}
        dl = ArtifactDownloader(api, "mefx4b-uid", tab.temp_dir,
                                _graph_jobs(want_canvas=True))
        dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
        dl.error.connect(lambda m: out.__setitem__("error", m))
        dl.run()
        assert out.get("error") is None, f"загрузчик увёл вкладку в ошибку: {out}"
        tab._on_downloaded(out["artifacts"])
        if expect_editor:
            assert tab._editor is not None, "редактор не собрался"
        else:
            assert tab._editor is None, (
                "холста нет, а редактор собрался — блок 8 обещал отказ")
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()


@pytest.fixture
def hand_tab(qapp, monkeypatch, tmp_path):
    """Вкладка с ГОТОВЫМ холстом в редакторе, без загрузчика.

    Для 4.4 боевой путь загрузки роли не играет (там проверяется плетение
    вкладки: курсор, строка, показ вёдер), а корпусный холст с сервером
    не сверить — его `source_sha` от чужой схемы.
    """
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.tabs.base_graph_tab import BaseGraphTab

    opened = []

    def _open(canvas_path):
        monkeypatch.setattr(BaseGraphTab, "_download_artifacts",
                            lambda self: None)
        tab = AdvancedGraphTab("mefx4b-uid", "проба 4.4", MagicMock())
        opened.append(tab)
        img = QImage(IMG_W, IMG_H, QImage.Format.Format_RGB32)
        img.fill(QColor("white"))
        ip = tmp_path / "hand.png"
        assert img.save(str(ip))
        editor = tab._create_editor()
        assert editor.load_data(str(ip), str(canvas_path))
        tab._editor = editor
        editor.status_callback = tab.status_label.setText
        tab._on_editor_ready()
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()


def _engines(monkeypatch, tab):
    """Перехват ОБОИХ движков кнопки: кто из них позван."""
    called = []
    monkeypatch.setattr(tab._editor, "auto_fix",
                        lambda *a, **k: called.append("auto_fix"))
    monkeypatch.setattr(tab._editor, "smooth_canvas",
                        lambda *a, **k: called.append("smooth") or {})
    return called


# ── 4.1, путь (а): холст с сервера, layout_applied=False ─────────────────


def test_saved_canvas_without_layout_locks_the_button(open_tab, monkeypatch):
    """Холст СОХРАНЁН на сервере и актуален, но собран без раскладки.

    Ровно тот случай, который редтим назвал живым: ветка «актуален» его
    пропускает как есть, и до 4.1 кнопка вела оператора в прежний `auto_fix`.
    """
    tab = open_tab(_canvas(layout_applied=False, extra_node=SERVER_ONLY_NODE))

    assert "server_only" in tab._editor.nodes, \
        "редактор открыл не серверный холст — тест проверяет не тот путь"

    assert not tab.btn_auto_fix.isEnabled(), \
        "кнопка на холсте без раскладки обязана быть заперта"
    assert "раскладк" in tab.btn_auto_fix.toolTip().lower(), \
        f"подсказка не называет причину: {tab.btn_auto_fix.toolTip()!r}"

    called = _engines(monkeypatch, tab)
    tab.btn_auto_fix.click()
    assert called == [], f"заперта, а движок позван: {called}"
    tab._auto_fix()          # прямой путь — обещание держится и на нём
    assert called == [], f"прежний auto_fix запущен мимо кнопки: {called}"
    assert "раскладк" in tab.status_label.text().lower(), \
        f"оператору не сказано, почему отказ: {tab.status_label.text()!r}"


def test_saved_canvas_with_layout_keeps_the_button(open_tab, monkeypatch):
    """Обратная граница: тот же боевой путь, но холст РАЗЛОЖЕН.

    Без этой половины замок нельзя отличить от «кнопка выключена всегда»."""
    tab = open_tab(_canvas(layout_applied=True, extra_node=SERVER_ONLY_NODE))

    assert "server_only" in tab._editor.nodes
    assert tab.btn_auto_fix.isEnabled(), \
        "на разложенном холсте кнопка обязана работать"

    called = _engines(monkeypatch, tab)
    tab.btn_auto_fix.click()
    assert called == ["smooth"], f"ожидалось сглаживание: {called}"


# ── 4.1, путь (б): холста нет вовсе ──────────────────────────────────────


def test_canvas_absent_gives_no_editor_at_all(open_tab):
    """`graph_canvas` на сервере нет (404) — вкладка НЕ открывается.

    ⚠ Пересъём mefx-8. Прежняя редакция называлась «фолбэк-холст тоже без
    раскладки — кнопка заперта» и проверяла замок ПОВЕРХ аварийного холста,
    который вкладка собирала сама. Блок 8 эту сборку убрал: холста нет —
    редактора нет, и опасный движок недостижим не замком, а отсутствием
    объекта, на котором он работает. Замок 4.1 от этого не лишний: путь (а)
    (сохранённый холст с `layout_applied=False`) остался, и его половина
    набора зелёная.
    """
    tab = open_tab(None, expect_editor=False)

    # Движки перехватить не на чем — их носитель не создан; это и есть
    # утверждение. Вызов обязан быть тихим no-op, а не падением.
    tab._auto_fix()
    assert tab._editor is None
    assert not tab.btn_save.isEnabled() and not tab.btn_confirm.isEnabled(), (
        "на экране отказа кнопки записи живы — жест соврёт оператору")


# ── 4.4: курсор, строка и три ведра отказа ──────────────────────────────


def test_wait_cursor_and_status_live_only_during_the_engine(open_tab,
                                                            monkeypatch):
    """Курсор ожидания и «Сглаживание…» — ВО ВРЕМЯ прогона, а не после.

    Утверждается разница: снимок берётся ИЗНУТРИ движка и сверяется с
    состоянием после возврата. Тест, смотрящий только «после», зелен и на
    коде без курсора вовсе.
    """
    tab = open_tab(_canvas(layout_applied=True))
    seen = {}

    def _engine(*a, **k):
        cursor = QApplication.overrideCursor()
        seen["shape"] = cursor.shape() if cursor is not None else None
        seen["status"] = tab.status_label.text()
        return {}

    monkeypatch.setattr(tab._editor, "smooth_canvas", _engine)
    tab.btn_auto_fix.click()

    assert seen.get("shape") == Qt.CursorShape.WaitCursor, \
        f"во время сглаживания курсор не ждёт: {seen.get('shape')}"
    assert seen.get("status") == "Сглаживание…", \
        f"оператору не сказано, что идёт работа: {seen.get('status')!r}"
    assert QApplication.overrideCursor() is None, \
        "курсор ожидания остался висеть после возврата"


def test_wait_cursor_is_dropped_before_the_failure_report(open_tab,
                                                          monkeypatch):
    """Движок упал — отчёт открывается уже БЕЗ песочных часов.

    Модалка под курсором ожидания — то же «выглядит зависшей», от которого
    пункт и заводился; поэтому курсор снимается своим `finally`, вложенным.
    """
    import ui.tabs.advanced_graph_tab as mod

    tab = open_tab(_canvas(layout_applied=True))
    seen = {}

    def _boom(*a, **k):
        raise RuntimeError("движок упал посреди лестницы")

    monkeypatch.setattr(tab._editor, "smooth_canvas", _boom)
    monkeypatch.setattr(
        mod, "report_exception",
        lambda *a, **k: seen.__setitem__("cursor",
                                         QApplication.overrideCursor()))
    tab.btn_auto_fix.click()

    assert "cursor" in seen, "отказ не дошёл до отчёта — тест проверяет не то"
    assert seen["cursor"] is None, "отчёт открылся под курсором ожидания"
    assert QApplication.overrideCursor() is None
    assert tab.status_label.text() != "Сглаживание…",         "строка «идёт работа» пережила отказ и осталась висеть"


#: Корпусный холст `tools/bench/smooth_corpus/edited/` (в git, 20 файлов),
#: на котором ЖИВАЯ кнопка получает отказ. Взят не наугад: замер §MEFX4Bб
#: прогнал всю популяцию боевым `smooth_canvas` — 18 холстов из 20 дают
#: отказ; этот — из самых быстрых (0.47 с) при ненулевом ведре.
REFUSING_CANVAS = "tools/bench/smooth_corpus/edited/graph_edited_after.json"


def test_refusal_buckets_reach_the_status_line(hand_tab):
    """Вёдра отказа названы оператору — на ЖИВОМ движке, не на словаре.

    Фикстура заведомо ПО ТУ СТОРОНУ порога (PROTOCOL §5, замер 1-44): без
    хотя бы одного отказа ветка показа не исполняется вовсе и сторож
    декоративен, поэтому ненулевой отказ утверждается отдельно.

    Сверяется НЕ заранее вбитое имя ведра, а верность показа движку: в
    строке обязаны стоять ровно те вёдра и ровно те числа, что он вернул.
    Так утверждение переживает и правку роутера, и четвёртое ведро.
    """
    from modules.graph.core import edit_smooth

    tab = hand_tab(REFUSING_CANVAS)
    stats = tab._editor.smooth_canvas()
    assert stats["отклонено"] >= 1, f"фикстура не даёт отказа: {stats}"
    fired = {k: stats[k] for k in edit_smooth.REFUSAL_KINDS if stats[k]}
    assert fired, f"отказ есть, а ведра нет: {stats}"

    tab._auto_fix()
    text = tab.status_label.text()
    assert "не вышло: " in text, f"причина отказа не названа: {text!r}"
    assert "осталось" in text, \
        f"сводка ходов затёрта вместо дополнения: {text!r}"
    for kind in edit_smooth.REFUSAL_KINDS:
        if kind in fired:
            assert f"{kind} {fired[kind]}" in text, \
                f"ведро {kind!r} показано не тем числом: {text!r} vs {fired}"
        else:
            assert kind not in text, \
                f"пустое ведро {kind!r} засоряет строку: {text!r}"


def test_silent_run_adds_nothing_to_the_status(open_tab, monkeypatch):
    """Отказов не было — строка редактора остаётся нетронутой."""
    tab = open_tab(_canvas(layout_applied=True))

    def _clean(*a, **k):
        tab.status_label.setText("Сглаживание: колен 1; осталось 0")
        return {"отклонено": 0, "нет кандидата": 0,
                "сверх бюджета": 0, "лестница исчерпана": 0}

    monkeypatch.setattr(tab._editor, "smooth_canvas", _clean)
    tab.btn_auto_fix.click()
    assert tab.status_label.text() == "Сглаживание: колен 1; осталось 0"
