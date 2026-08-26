# -*- coding: utf-8 -*-
"""Блок 8 линии «Ручная правка + FXML» (mefx-8): холст — есть или нет.

**Что было.** Вкладка в холстовом режиме молча собирала АВАРИЙНЫЙ холст
(`_pretransform_to_canvas`, метка `layout_applied=False`) в четырёх ветках
сразу: холста нет · холст устарел · холст не скачался · любой сбой подготовки
(except-хвост). Оператор попадал внутрь и правил недосчитанное, а настоящая
раскладка, поставленная закрытием «Контуров», ложилась рядом и его правки
теряла.

**Что стало (решения Максима №8 и №9).**

* холста нет / устарел / сбой подготовки → честный ОТКАЗ, редактор не
  создаётся вовсе;
* задача раскладки РЕАЛЬНО поставлена или бежит (стадия `layout` из
  `/stages`) → экран ожидания, и он отпирается САМ по готовности;
* задачи нет и не будет → экран-ДВЕРЬ «вернитесь в „Контуры“ и нажмите
  „Подтвердить“» (`complete_contour_validation` диспатчит раскладку всегда);
* холст не скачался (5xx/сеть) → отдельный исход с ретраем: это НЕ «холста
  нет», на сервере лежит ручная раскладка (её половина заперта в наборе
  1.23, `tests/ui/test_download_failure_is_visible.py`).

⛔ **Ключевое различение набора — «задача бежит» против «задачи нет».**
Показать «пересчитывается» там, где никто ничего не считает, значит соврать
НАВЕЧНО: пути без диспатча существуют и они частые (сохранение контуров без
подтверждения, окно `ALREADY_RUNNING` диспетчера, поднятие
`LAYOUT_CODE_VERSION` — оно протухает ВСЕ холсты установки разом). Поэтому
каждый экран здесь проверяется парой: с задачей и без неё.

⛔ **Перезагрузка идёт по ПЕРЕХОДУ «задача была → задачи нет», а не по
«стадия completed»:** холст умеет родиться протухшим при завершённой задаче
(угол `ALREADY_RUNNING`), и перечитывание по одному `completed` вернуло бы тот
же вердикт — экран закольцевался бы на бесконечной загрузке. Счётчик заходов
на сервер заперт абсолютным числом.

Проверяется наблюдаемое: ЧТО оператор читает на месте редактора, собрался ли
редактор, появился ли на диске аварийный холст (`graph_1920.json`) и сколько
раз вкладка сходила на сервер.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                                    # noqa: E402

pytest.importorskip("PySide6")

from pathlib import Path                                         # noqa: E402

from PySide6.QtCore import QThread                               # noqa: E402
from PySide6.QtGui import QColor, QImage                         # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from datetime import datetime, timedelta                         # noqa: E402

from ui.services.api_client import APIError                      # noqa: E402
from ui.services.layout_gate import WAIT_LIMIT_S                 # noqa: E402

UID = "mefx8-0001"
OTHER_UID = "mefx8-9999"
IMG_W, IMG_H = 1920, 1080

#: Узел, которого нет в источнике: по нему видно, ЧЕЙ холст открыт —
#: серверный (пересчитанный) или собранный вкладкой из graph_validated.
LAYOUT_ONLY_NODE = {"id": "laid_out", "type": "equipment",
                    "class_name": "unknow", "centroid": [700.0, 700.0],
                    "bbox": [680.0, 680.0, 720.0, 720.0]}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _validated(extra=None):
    g = {
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
    if extra:
        g["nodes"].append(json.loads(json.dumps(extra)))
    return g


def _canvas(source, *, extra=None, layout_applied=True):
    """Холст, помеченный ТЕМ ЖЕ каноном, что и раскладка (`canvas_state.stamp`).

    Метка считается от `source`: холст, помеченный от ЧУЖОГО графа, вкладка
    честно объявит устаревшим — так стенд получает «устарел» без подделки
    внутренних полей.
    """
    from modules.graph.core import canvas_state

    graph = _validated(extra)
    canvas_state.stamp(graph, source, layout_applied=layout_applied)
    return graph


class FakeServer:
    """Артефакты + стадии. Отсутствующее — 404, как боевой сервер."""

    def __init__(self, blobs, stages=None):
        self.blobs = dict(blobs)
        self.stages = list(stages or [])
        self.downloads = 0          # заходов за холстом — счётчик перезагрузок

    # ── поверхность APIClient, которой пользуется вкладка ──
    def download_artifact(self, uid, artifact_type, dest_path):
        if artifact_type == "graph_canvas":
            self.downloads += 1
        data = self.blobs.get(artifact_type)
        if data is None:
            raise APIError(f"artifact {artifact_type} not found", 404)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def get_stages(self, uid):
        return list(self.stages)

    # ── то, что делает сервер между заходами оператора ──
    def start_layout(self, stage_id=1):
        self.stages = [{"id": stage_id, "stage_type": "layout",
                        "status": "running",
                        "started_at": "2026-08-26T10:00:00"}]

    def finish_layout(self, canvas, stage_id=1):
        self.blobs["graph_canvas"] = _blob(canvas)
        self.stages = [{"id": stage_id, "stage_type": "layout",
                        "status": "completed",
                        "started_at": "2026-08-26T10:00:00",
                        "completed_at": "2026-08-26T10:02:00"}]


def _blob(graph: dict) -> bytes:
    return json.dumps(graph, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def raster(qapp, tmp_path_factory) -> bytes:
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("raster") / "original.png"
    assert img.save(str(png))
    return png.read_bytes()


@pytest.fixture
def open_tab(qapp, monkeypatch, raster):
    """«Ручная правка» на поддельном сервере — БОЕВЫМ швом загрузки.

    `_download_artifacts` подменён синхронным прогоном НАСТОЯЩЕГО
    `ArtifactDownloader` с доставкой в `_on_downloaded`: так работает и первый
    заход, и повтор (кнопка «Повторить», автоперечитывание после пересчёта) —
    иначе повтор проверить нечем, он ходит на сервер сам.
    """
    from ui.services.artifact_downloader import ArtifactDownloader
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.tabs.base_graph_tab import BaseGraphTab, _graph_jobs

    opened = []

    def _open(server, uid=UID):
        loads = []

        def _sync(self):
            # ⛔ ОГРАНИЧИТЕЛЬ ПЕТЛИ, а не украшение (`PROTOCOL §5`: зонд может
            # не покраснеть и не позеленеть, а ПОВИСНУТЬ). В бою загрузка
            # асинхронная, и «перечитывать по кругу» выглядит бесконечной
            # загрузкой; здесь она синхронная, поэтому тот же дефект даёт
            # РЕКУРСИЮ `_on_downloaded → экран → apply_stages → повтор` и
            # вешает набор вместо падения — замерено инъекцией зонда 3
            # (стек `py-spy`, §MEFX8). Порог рвёт петлю, число заходов
            # остаётся видимым, и утверждение о нём краснеет как положено.
            loads.append(1)
            if len(loads) > MAX_LOADS:
                return
            self._download_thread = QThread(self)
            out = {}
            dl = ArtifactDownloader(server, self.uid, self.temp_dir,
                                    _graph_jobs(want_canvas=self.USE_CANVAS))
            dl.finished.connect(lambda a: out.__setitem__("artifacts", a))
            dl.error.connect(lambda m: out.__setitem__("error", m))
            dl.run()
            # Оба исхода доставляются ТЕМИ ЖЕ слотами, что в бою: отказ
            # обязательного артефакта идёт в `_on_download_error`, а не в
            # `assert` стенда — иначе неудачный повтор в наборе недостижим.
            if out.get("error") is not None:
                self._on_download_error(out["error"])
            else:
                self._on_downloaded(out["artifacts"])

        monkeypatch.setattr(BaseGraphTab, "_download_artifacts", _sync)
        tab = AdvancedGraphTab(uid, "проба блока 8", server)
        opened.append(tab)
        return tab

    yield _open
    for tab in opened:
        tab.cleanup()
        tab.deleteLater()
    qapp.processEvents()


def _server(raster, *, canvas=None, stages=None, canvas_5xx=False):
    blobs = {"original_image": raster,
             "graph_validated": _blob(_validated())}
    if canvas is not None:
        blobs["graph_canvas"] = _blob(canvas)
    server = FakeServer(blobs, stages)
    if canvas_5xx:
        real = server.download_artifact

        def _fail(uid, artifact_type, dest_path):
            if artifact_type == "graph_canvas":
                server.downloads += 1
                raise APIError("bad gateway", 502)
            return real(uid, artifact_type, dest_path)

        server.download_artifact = _fail
    return server


def _screen(tab) -> str:
    """Текст, который оператор читает на месте редактора."""
    assert tab._canvas_gate is not None, "экрана нет — вкладка открылась?"
    return tab._canvas_gate_label.text()


def _fallback_built(tab) -> bool:
    """Аварийный холст оставляет след — файл `graph_1920.json` во временном."""
    return (tab.temp_dir / "graph_1920.json").exists()


#: Потолок заходов вкладки на сервер в одном тесте: рвёт петлю
#: «перечитал → тот же вердикт → перечитал» (см. `_sync`). Любой честный
#: сценарий укладывается в два захода — первый и один повтор.
MAX_LOADS = 4

RUNNING = [{"id": 1, "stage_type": "layout", "status": "running",
            "started_at": "2026-08-26T10:00:00"}]
PENDING = [{"id": 1, "stage_type": "layout", "status": "pending"}]
DONE = [{"id": 1, "stage_type": "layout", "status": "completed",
         "started_at": "2026-08-26T10:00:00"}]


# ── 8.1: холста нет → отказ, а не аварийная сборка ───────────────────────

def test_no_canvas_no_editor_and_no_fallback(open_tab, raster):
    """Холста нет и задачи нет: отказ с дверью, аварийный холст не собран."""
    tab = open_tab(_server(raster))

    assert tab._editor is None, "редактор собран на несуществующем холсте"
    assert not _fallback_built(tab), "аварийный холст всё-таки собран"
    assert "не готов" in _screen(tab)
    assert "Контуры" in _screen(tab) and "Подтвердить" in _screen(tab)


def test_no_canvas_but_a_running_task_waits(open_tab, raster):
    """Тот же «холста нет», но задача бежит — ждём, а не отправляем в дверь.

    Утверждается РАЗНИЦА с соседом выше: вход тот же, экраны разные, и
    различает их ровно строка стадии.
    """
    tab = open_tab(_server(raster, stages=RUNNING))

    assert tab._editor is None and not _fallback_built(tab)
    assert "пересчитывается" in _screen(tab), _screen(tab)
    assert "Контуры" not in _screen(tab), "ждущему предложили ещё раз пересчитать"


# ── 8.2: устарел → ждать пересчёт, а не подменять его фолбэком ───────────

def test_stale_canvas_with_a_running_task_waits(open_tab, raster):
    """Главный случай 8.2: холст устарел, сервер уже считает новый.

    Прежде вкладка в этот момент собирала свой аварийный и перекрывала им
    идущий пересчёт — оператор правил подменённый холст.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))     # метка от ЧУЖОГО графа
    tab = open_tab(_server(raster, canvas=stale, stages=RUNNING))

    assert tab._editor is None, "устаревший холст открыт как рабочий"
    assert not _fallback_built(tab), "фолбэк собран поверх идущего пересчёта"
    assert "пересчитывается" in _screen(tab), _screen(tab)


def test_pending_task_counts_as_running(open_tab, raster):
    """Задача в очереди — тоже «поставлена»: `started_at` у неё ещё нет.

    ⚠ Это не экзотика: `/stages` не отдаёт клиенту `created_at` вовсе, и до
    старта воркера у стадии нет НИ ОДНОЙ отметки времени (замер §MEFX8).
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    tab = open_tab(_server(raster, canvas=stale, stages=PENDING))

    assert "пересчитывается" in _screen(tab), _screen(tab)


def test_an_overdue_wait_stops_promising_that_it_will_open(open_tab, raster):
    """Задача бежит дольше предела — обещание «откроется сама» снимается.

    Зависшую насмерть задачу (воркер убит, контейнер пересоздан) сервер сам не
    закроет: `plan_dispatch` на ту же истину отвечает `ALREADY_RUNNING`, новой
    стадии не будет, и экран ждал бы ВЕЧНО. Утверждается РАЗНИЦА: та же вкладка
    на свежей бегущей задаче обещает открыться сама.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    fresh_run = [{"id": 1, "stage_type": "layout", "status": "running",
                  "started_at": datetime.utcnow().isoformat()}]
    tab = open_tab(_server(raster, canvas=stale, stages=fresh_run))
    assert "откроется сама" in _screen(tab), _screen(tab)

    overdue = [{"id": 1, "stage_type": "layout", "status": "running",
                "started_at": (datetime.utcnow()
                               - timedelta(seconds=WAIT_LIMIT_S + 60)).isoformat()}]
    tab.apply_stages(overdue)

    assert "дольше обычного" in _screen(tab), _screen(tab)
    assert "откроется сама" not in _screen(tab), (
        "просроченное ожидание всё ещё обещает открыться само — обещание "
        "без срока годности")
    assert "Контуры" in _screen(tab), "выхода из ожидания оператору не назвали"


def test_stale_canvas_without_a_task_gets_the_door(open_tab, raster):
    """Задачи нет — дверь, а не вечное «пересчитывается».

    Пути без диспатча существуют (сохранение контуров без подтверждения, окно
    `ALREADY_RUNNING`, смена версии раскладки), и обещание пересчёта на них
    было бы ложью без срока годности.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    tab = open_tab(_server(raster, canvas=stale))

    assert tab._editor is None and not _fallback_built(tab)
    assert "не готов" in _screen(tab), _screen(tab)
    assert "Контуры" in _screen(tab) and "Подтвердить" in _screen(tab)
    assert "пересчитывается" not in _screen(tab)
    # ⛔ И ЦЕНА ДВЕРИ НАЗВАНА. Пересчёт снимает с холста метку «правился
    # руками» и перезаписывает его на сервере — ровно это говорила снесённая
    # модалка «Схема изменилась». Экран, который зовёт нажать «Подтвердить»
    # и молчит об этом, стоит оператору часов ручной раскладки.
    assert "не сохранится" in _screen(tab), (
        f"дверь не называет цену — оператор потеряет ручную раскладку молча: "
        f"{_screen(tab)!r}")


def test_a_finished_task_over_a_stale_canvas_does_not_loop(open_tab, raster):
    """Угол `ALREADY_RUNNING`: задача завершена, а холст всё равно протух.

    Перезагружаться тут нельзя — вернётся тот же вердикт. Заперто счётчиком
    заходов на сервер: он обязан остаться единицей при любом числе обновлений
    стадий.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale, stages=DONE)
    tab = open_tab(server)

    assert server.downloads == 1
    for _ in range(5):
        tab.apply_stages(server.get_stages(UID))
    assert server.downloads == 1, (
        "экран перечитывает холст по кругу — оператор видит вечную загрузку")
    assert "не готов" in _screen(tab)


# ── дверь отпирается по готовности (канал `stages_updated`) ──────────────

def test_the_wait_ends_with_the_recomputed_canvas(open_tab, raster):
    """Сценарий целиком: ждём пересчёт → он кончился → открыт ЕГО холст.

    Утверждается не «редактор появился», а ЧЕЙ холст в нём: `laid_out` есть
    только в серверном пересчитанном холсте, в источнике его нет. Аварийная
    сборка такого узла дать не может по построению.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale, stages=RUNNING)
    tab = open_tab(server)
    assert "пересчитывается" in _screen(tab)
    assert server.downloads == 1

    # Воркер досчитал и положил новый холст — ровно на текущую истину.
    server.finish_layout(_canvas(_validated(), extra=LAYOUT_ONLY_NODE))
    tab.apply_stages(server.get_stages(UID))

    assert tab._editor is not None, "дверь не отперлась по готовности"
    assert tab._canvas_gate is None, "экран ожидания остался поверх редактора"
    assert "laid_out" in tab._editor.nodes, "открыт не пересчитанный холст"
    assert not _fallback_built(tab), "по дороге собран аварийный холст"
    assert server.downloads == 2, "холст перечитан не один раз"
    assert tab.btn_save.isEnabled() and tab.btn_confirm.isEnabled(), (
        "кнопки записи остались заперты на открытом холсте")


def test_a_task_that_ends_without_a_canvas_returns_to_the_door(open_tab,
                                                               raster):
    """Задача кончилась, а холста так и нет — дверь, а не вечное ожидание.

    Достижимо при `dispatch_failed` и при отказе задачи: гейт кнопки такую
    стадию запирает сам, но вкладка, уже открытая на ожидании, обязана
    договорить честно.
    """
    server = _server(raster, stages=RUNNING)
    tab = open_tab(server)
    assert "пересчитывается" in _screen(tab)

    server.stages = DONE
    tab.apply_stages(server.get_stages(UID))

    assert tab._editor is None
    assert "не готов" in _screen(tab), _screen(tab)
    assert server.downloads == 2, "перечитать сервер после конца задачи забыли"


def test_foreign_stages_do_not_move_the_screen(open_tab, raster):
    """Стадии ЧУЖОЙ схемы экран не трогают (в клиенте открыта не одна схема)."""
    server = _server(raster, stages=RUNNING)
    tab = open_tab(server)

    tab._on_stages_updated(OTHER_UID, DONE)

    assert "пересчитывается" in _screen(tab), "чужие стадии сдвинули экран"
    assert server.downloads == 1


def test_the_screen_asks_for_the_watch(open_tab, raster):
    """Экран просит слежение: пока вкладка открыта, воркспейс его снимает.

    Без этой просьбы дверь не отопрётся до перезахода в диаграмму — стадии
    просто не приезжают (`_open_tab` → `unwatch`).
    """
    server = _server(raster, stages=RUNNING)
    asked = []

    from ui.tabs.advanced_graph_tab import AdvancedGraphTab  # noqa: F401

    tab = open_tab(server)          # сигнал уже вылетел при построении экрана
    # Пересобираем экран, чтобы поймать сигнал живым слушателем: связь
    # ставится воркспейсом ДО открытия вкладки, здесь её ставим руками.
    tab.layout_watch_requested.connect(lambda: asked.append(True))
    tab._show_canvas_gate("stale")

    assert asked, "экран ожидания слежения не просит"


def test_a_ready_canvas_asks_for_nothing(open_tab, raster):
    """Обратная полярность: свежий холст открывается и экрана не поднимает."""
    fresh = _canvas(_validated(), extra=LAYOUT_ONLY_NODE)
    server = _server(raster, canvas=fresh, stages=DONE)
    asked = []

    tab = open_tab(server)
    tab.layout_watch_requested.connect(lambda: asked.append(True))

    assert tab._editor is not None, "свежий холст не открылся"
    assert tab._canvas_gate is None
    assert "laid_out" in tab._editor.nodes
    assert asked == []
    assert not _fallback_built(tab)


# ── возврат red-team: чего экран НЕ имеет права делать ──────────────────


def test_a_broken_stages_tick_does_not_reload_anything(open_tab, raster):
    """Сбойный тик `/stages` — не «задача кончилась».

    `APIClient.get_stages` глотает отказ сервера и отдаёт `[]`, а
    `StatusProvider._poll` эмитит этот `[]` наравне с настоящими стадиями.
    Раньше пустой список неотличимо значил «задачи нет» → срабатывал переход
    → полная перезагрузка ВСЕХ артефактов. На дрожащей связи это шторм
    перезагрузок, и падают они по той же причине, по которой пришёл `[]`.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale, stages=RUNNING)
    tab = open_tab(server)
    assert "пересчитывается" in _screen(tab)
    assert server.downloads == 1

    for _ in range(5):
        tab.apply_stages([])              # сервер молчит, отдан пустой список

    assert server.downloads == 1, "пустой тик перечитал холст"
    assert "пересчитывается" in _screen(tab), (
        "пустой тик увёл экран в дверь — оператору сказали неправду о сервере")

    # Обратная полярность: НАСТОЯЩАЯ завершённая стадия перезагрузку даёт.
    server.finish_layout(_canvas(_validated(), extra=LAYOUT_ONLY_NODE))
    tab.apply_stages(server.get_stages(UID))
    assert server.downloads == 2, "настоящая готовность перезагрузку не дала"


def test_one_retry_click_is_one_reload(open_tab, raster):
    """Клик «Повторить» стоит РОВНО один заход на сервер.

    Память о бегущей задаче гасится в самом повторе: иначе новый экран внутри
    того же `_on_downloaded` снова видел переход «была → нет» и перезагружался
    ещё раз (замер: один клик = три захода).
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale, stages=RUNNING)
    tab = open_tab(server)
    assert server.downloads == 1

    server.stages = DONE                  # задача кончилась к моменту клика
    tab._canvas_gate_retry.click()

    assert server.downloads == 2, (
        f"клик «Повторить» стоил {server.downloads - 1} заходов вместо одного")


def test_a_failed_retry_keeps_the_way_out(open_tab, raster, monkeypatch):
    """Повтор не удался — экран и кнопка остаются, вкладка не тупик.

    `_retry_download` сносит экран ДО того, как узнает исход загрузки. Если
    обязательный артефакт не отдался, приходит `error`, и без этой ветки
    вкладка оставалась без вердикта, без кнопки и без канала готовности:
    выход — только «← Назад» и заново.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale)
    tab = open_tab(server)
    assert tab._canvas_gate_retry is not None

    # Связь окончательно пропала: обязательный артефакт не отдаётся.
    def _dead(uid, artifact_type, dest_path):
        raise APIError("gateway timeout", 504)

    monkeypatch.setattr(server, "download_artifact", _dead)
    tab._canvas_gate_retry.click()

    assert tab._canvas_gate is not None, "экран не вернулся — вкладка тупик"
    assert tab._canvas_gate_retry.isEnabled(), "повторить больше нечем"
    assert tab._canvas_gate_verdict is not None, (
        "вердикт потерян — канал готовности мёртв, дверь не отопрётся")


def test_tooltips_stop_lying_after_the_canvas_opens(open_tab, raster):
    """Кнопки записи вернули себе не только доступность, но и подсказки.

    Утверждается РАЗНИЦА: на экране отказа подсказка про «холст не открыт»
    стоит, после открытия холста — нет.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale, stages=RUNNING)
    tab = open_tab(server)
    assert "не открыт" in tab.btn_save.toolTip(), tab.btn_save.toolTip()

    server.finish_layout(_canvas(_validated(), extra=LAYOUT_ONLY_NODE))
    tab.apply_stages(server.get_stages(UID))

    assert tab._editor is not None
    for btn in (tab.btn_save, tab.btn_confirm):
        assert "не открыт" not in btn.toolTip(), (
            f"подсказка врёт на открытом холсте: {btn.toolTip()!r}")


def test_the_watch_is_released_when_the_editor_opens(open_tab, raster):
    """Опрос отпускается, как только экран сменился редактором.

    Иначе он живёт всю сессию ручной правки: снятие у `StatusProvider`
    привязано к СМЕНЕ статуса, а после подтверждения контуров статус часто
    не меняется вовсе.
    """
    stale = _canvas(_validated(LAYOUT_ONLY_NODE))
    server = _server(raster, canvas=stale, stages=RUNNING)
    tab = open_tab(server)

    asked = []
    tab.layout_watch_requested.connect(asked.append)

    server.finish_layout(_canvas(_validated(), extra=LAYOUT_ONLY_NODE))
    tab.apply_stages(server.get_stages(UID))

    assert tab._editor is not None
    assert asked and asked[-1] is False, (
        f"слежение не отпущено после открытия холста: {asked}")


# ── шов с воркспейсом: кто кормит вкладку стадиями ───────────────────────

def test_workspace_feeds_stages_and_wakes_the_watch(qapp, monkeypatch):
    """Воркспейс связывает вкладку с опросом и будит его по просьбе экрана.

    Проверяются ДАННЫЕ, а не поля: факт слежения у провайдера и то, что
    стадии доехали до вкладки. Механика (`_was_status_watching` и прочее) —
    не предмет утверждения, она переживёт декомпозицию (волна 10-8).
    """
    import ui.widgets.diagram_workspace as dw
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

    class Provider(QObject):
        status_updated = Signal(str, object)
        stages_updated = Signal(str, object)

        def __init__(self):
            super().__init__()
            self.watched = set()

        def watch(self, uid):
            self.watched.add(uid)

        def unwatch(self, uid):
            self.watched.discard(uid)

        def is_watching(self, uid):
            return uid in self.watched

    seen = []

    class StubTab(QWidget):
        """Вкладка «Ручной правки» в объёме шва: сигнал наружу, слоты внутрь."""

        confirmed = Signal()
        status_message = Signal(str)
        layout_watch_requested = Signal(bool)

        def __init__(self, *a, **kw):
            super().__init__(kw.get("parent"))
            self.known = "не отдавали"
            QVBoxLayout(self).addLayout(QHBoxLayout())

        def set_project_code(self, code):
            pass

        def has_unsaved_changes(self):
            return False

        def set_known_stages(self, stages):
            self.known = stages

        def _on_stages_updated(self, uid, stages):
            seen.append((uid, stages))

    class API:
        def get_diagram(self, uid):
            raise APIError("не нужен этому шву", 500)

        def get_stages(self, uid):
            return []

        def get_ocr_status(self, uid):
            return {"has_ocr_result": False}

        def get_stage_durations(self):
            return {}

    monkeypatch.setattr("ui.tabs.advanced_graph_tab.AdvancedGraphTab", StubTab)

    provider = Provider()
    ws = dw.DiagramWorkspace(API(), provider)
    ws._uid = "mefx8-ws"
    ws._diagram_name = "проба шва"
    ws._open_graph_editor()
    tab = ws._active_tab
    assert isinstance(tab, StubTab), "вкладка не открылась — шва нет"

    # Свои стадии воркспейс отдаёт СРАЗУ — вкладке не за чем идти в сеть.
    assert tab.known != "не отдавали", (
        "кэш стадий вкладке не передан — экран пойдёт спрашивать сам, "
        "синхронно, из GUI-потока")

    # `_open_tab` слежение снял; экран ожидания просит вернуть.
    provider.unwatch("mefx8-ws")
    tab.layout_watch_requested.emit(True)
    assert provider.is_watching("mefx8-ws"), "просьба экрана осталась без ответа"

    provider.stages_updated.emit("mefx8-ws", DONE)
    assert seen and seen[-1][1] == DONE, "стадии до вкладки не доехали"

    # И ОТПУСКАЕТ: экрана больше нет — опрос обязан вернуться в то состояние,
    # в каком его оставил `_open_tab`, иначе он живёт всю сессию правки.
    tab.layout_watch_requested.emit(False)
    assert not provider.is_watching("mefx8-ws"), (
        "опрос остался жить под открытым редактором — два HTTP каждые 2 с "
        "в GUI-потоке, ровно та просадка, ради которой `_open_tab` его глушит")

    ws._stop_ocr_poll()
    ws.hide()
    ws.setParent(None)
    ws.deleteLater()
    qapp.processEvents()
