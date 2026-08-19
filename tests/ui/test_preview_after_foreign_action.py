# -*- coding: utf-8 -*-
"""Пункт 1.4 дороги, ВТОРАЯ доработка по возврату ревизии связки 1-6+1-31+1-32.

Зачем отдельный файл. Набор `test_preview_not_welded_into_command.py` (первая
доработка) поднимает превью на СВЕЖЕЙ вкладке — и потому был зелёным при живом
дефекте вместе с наборами 1-31 и 1-32: 40 passed, весь `tests/ui` 534 passed,
оба гейта exit 0 (§83в). Здесь каждый прогон идёт ПОСЛЕ чужого действия
оператора либо по пути, где дропа нет вовсе. Это разные обстановки, а не
разные утверждения, поэтому они и живут отдельно.

Что нашла ревизия (§83б, §83ж) — ДВЕ независимые половины одного инварианта
«Ctrl+Z после любой команды, нажатой при живом превью, возвращает схему
к состоянию ДО превью»:

  А. `drop_uncommitted_preview()` снимает превью только пока ЖИВ БАЗЛАЙН.
     Сторож протухания — равенство `undo_mgr.revision`, а он растёт на ЛЮБОЙ
     мутации стека. Обоснование ветки «протух → бросить без отката» верно
     ровно для undo/redo, которые ПЕРЕСОБИРАЮТ модель (`model.restore`), но
     `OptimizeEdgeCommand.undo` и `DragNodeCommand.undo` правят словари НА
     МЕСТЕ: один Ctrl+Z по такой команде двигает `revision`, не трогая превью,
     и все ШЕСТЬ потребителей лекарства (четыре кнопки 1-6, воронка 1-32, путь
     записи 1.5) вырождаются в no-op ОДНОВРЕМЕННО. Минимальная форма — три
     действия оператора: перенос узла → «Размеры» + превью → Ctrl+Z →
     «Сохранить» кладёт на сервер `[-10.0, 188.0, 80.0, 278.0]` при исходных
     `[25, 215, 45, 251]`, стек 0, `can_undo` False, диалогов ноль (§83.6).

  Б. Пути, которые строят точку возврата в режиме «Размеры» и дропа не делают
     ВООБЩЕ — им Ctrl+Z не нужен, базлайн при этом ЖИВ: правка полигона
     (`_enter_polygon_editing_mode` не зовёт `set_mode`, а ветка полигона
     в `mousePressEvent` стоит раньше сторожа резайза), ПКМ по приколотому
     концу трубы (`contextMenuEvent` не проверяет ни режим, ни состояние)
     и фиксация призрака вставки (Ctrl+V в базовом → «Размеры» → Ctrl+ЛКМ:
     призрак переживает смену инструмента, §86.13).

  В. ТРЕТЬЯ доработка (§90.15, §90.16): сторож отвечал «ВСЁ ИЛИ НИЧЕГО» на
     МНОЖЕСТВО узлов. Чужой Ctrl+Z по переносу возвращает ОДНОМУ узлу набора
     centroid+bbox+segmentation и честно стирает превью только на нём — а
     базлайн объявлялся мёртвым для ВСЕХ 17, и превью остальных вваривалось.
     Те же три действия, что в А, только перенесённый узел — ИЗ набора:
     на сервер уезжал `node_11` = `[-10.0, 188.0, 80.0, 278.0]` при стеке 0,
     а на полигонах «Применить» ×2 давало 2420×300 при недостижимых 605×75.
     Обстановка та же, что у А, поэтому тесты живут здесь, а не отдельно;
     ⛔ отличие в ОДНОМ: набор СМЕШАННЫЙ — часть элементов тронута чужим
     действием, часть нет (`PROTOCOL §3`). Половина А перебирала только
     однородный случай («узел ВНЕ набора»), и агрегатный вердикт выглядел
     верным ровно поэтому.

⛔ На ПОЛИГОНАХ вторая половина — не потеря отмены, а ПОРЧА ДАННЫХ:
`_resize_node_poly` масштабирует ОТНОСИТЕЛЬНО базлайна, а `_resize_node_box`
задаёт размер АБСОЛЮТНО. Пересъём базлайна поверх живого превью даёт
605×75 → ×2 → 1210×150 → «Применить» тем же ×2 → 2420×300, и исходные 605×75
не достижимы НИКАКИМ числом Ctrl+Z (§83.27). На боксах та же дорога
самолечится — вот почему это дожило: полигонов в наборах превью не было ни
одного, весь материал ходил по боксовому `armatura_ruchn`. Поэтому здесь
ОБЯЗАТЕЛЕН второй корпус — `089feca2` с четырьмя настоящими полигонами.

Проверяются только ДАННЫЕ (принцип набора 0.4): геометрия узлов, JSON, ушедший
серверу, глубина стека. Ни одного утверждения про `_current_mode`. Числа
абсолютные и заперты с двух сторон — и «размер вернулся», и «это не превью».

⚠ Обстановка: кнопки глушат исключения в `QMessageBox.warning`, и падение
внутри подвесило бы набор модальным диалогом вместо падения (четвёртый исход
`PROTOCOL §5`). Диалоги подменены, каждый тест утверждает ФАКТ их невызова.
⚠ Виджеты сносятся детерминированно (`tab.cleanup()` в фикстуре): брошенные
на сборщик мусора виджеты Qt детонируют отложенным удалением у соседа,
который первым крутит очередь событий (замер 1-32, `PROTOCOL §5`).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox     # noqa: E402
from PySide6.QtGui import QImage, QColor                    # noqa: E402

from tools import corpus                                    # noqa: E402

# ── корпус боксов: та же фикстура, что у первой доработки ────────────────
BOX_UID = "d74eb9f1"        # 66 узлов / 63 ребра, корпус-фикстура в git
BOX_CLASS = "armatura_ruchn"
BOX_NID = "node_11"         # bbox [25, 215, 45, 251] = 20×36
BOX_BBOX = [25, 215, 45, 251]
BOX_W, BOX_H = 20, 36
SIDE = 90                                     # цель превью: квадрат 90×90
BOX_PREVIEW = [-10.0, 188.0, 80.0, 278.0]
BOX_AREA = 720                                # area узла-свидетеля до превью
FOREIGN = "node_20"         # узел ВНЕ набора — «чужое действие оператора»
# ⛔ ГРАНИЦА (третий возврат, §90.15): «вне набора» — только ОДНА клетка.
# Ниже — вторая: чужое действие по узлу ИЗ набора. `set_resize_class` даёт
# 17 экземпляров `armatura_ruchn`, и `node_14` — один из них.
INSIDE = "node_14"          # узел ИЗ набора — его касается чужой откат
INSIDE_BBOX = [1258, 251, 1309, 279]          # 51×28
INSIDE_CENTROID = [265, 1283]                 # [y, x] — центроиды в этой оси
INSIDE_AREA = 1428
INSIDE_PREVIEW_AREA = 8100.0                  # 90×90 — след превью в `area`

# ── корпус полигонов: четыре настоящих полигона (требование ревизора) ────
POLY_UID = "089feca2"       # 72 узла, 4 узла с segmentation
POLY_CLASS = "armatura_ruchn"
POLY_BOX_NID = "node_21"    # бокс набора: [2072, 1423, 2112, 1486] = 40×63
POLY_BOX_BBOX = [2072, 1423, 2112, 1486]
POLY_BOX_W, POLY_BOX_H = 40, 63
POLY_SIDE = 200                               # цель превью боксов: 200×200
POLY_BOX_PREVIEW = [1992.0, 1354.0, 2192.0, 1554.0]
POLY_NID = "node_30"        # полигон 605×75, 8 вершин
POLY_W, POLY_H = 605.0, 75.0
POLY_FOREIGN = "node_50"    # узел для чужого переноса на этом корпусе
# ⛔ Та же граница на полигонах: набор ЧЕТЫРЁХ полигонов, чужой откат
# по одному из них. Класс `unknow` — 7 узлов, из них 4 с контуром.
POLY_SET_CLASS = "unknow"
POLY_SET = {"node_4", "node_6", "node_29", "node_30"}
POLY_INSIDE = "node_29"     # полигон ИЗ набора: 1247×314
POLY_INSIDE_W, POLY_INSIDE_H = 1247.0, 314.0

TOL = 1e-6


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeAPI:
    """Сервер: помнит РАЗОБРАННОЕ содержимое каждой заливки графа."""

    def __init__(self):
        self.uploads = []

    def _remember(self, kind, uid, path):
        with open(path, encoding="utf-8") as fh:
            self.uploads.append((kind, uid, json.load(fh)))
        return True

    def upload_canvas_graph(self, uid, path):
        return self._remember("canvas", uid, path)

    def upload_validated_graph(self, uid, path):
        return self._remember("validated", uid, path)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _white_raster(uid, tmp_path_factory):
    path = corpus.graph_path(uid)
    assert path is not None, f"корпус-фикстура {uid} не найдена (tools/corpus.py)"
    h, w = json.loads(path.read_text(encoding="utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp(f"raster_{uid}") / f"{uid}.png"
    assert img.save(str(png))
    return str(png)


@pytest.fixture(scope="module")
def box_raster(tmp_path_factory):
    return _white_raster(BOX_UID, tmp_path_factory)


@pytest.fixture(scope="module")
def poly_raster(tmp_path_factory):
    return _white_raster(POLY_UID, tmp_path_factory)


@pytest.fixture
def dialogs(monkeypatch):
    """Все модальные диалоги → журнал ФАКТОВ вызова (`PROTOCOL §5`)."""
    calls = []
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, _n=name, **kw: calls.append((_n, a[1:3]))))
    return calls


def _make_tab(uid, raster, monkeypatch, layout_applied=False):
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor
    from modules.graph.core import canvas_state

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    tab = AdvancedGraphTab(uid, "проба 1.4-fix2", FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(uid)))
    editor.resize(1400, 900)
    if layout_applied:
        # Ветка кнопки выбирается по метке холста: с раскладкой — сглаживание,
        # без неё — родной auto_fix. Корпус-фикстура метки не несёт.
        editor.graph_data.setdefault("graph", {})["canvas_transform"] = {
            "layout_applied": True,
            "layout_version": canvas_state.layout_version(),
        }
    assert canvas_state.has_layout(editor.graph_data or {}) is layout_applied
    tab._editor = editor
    tab._on_editor_ready()
    return tab


@pytest.fixture
def box_tab(qapp, box_raster, monkeypatch):
    """Фолбэк-холст на корпусе боксов: кнопка уходит в `auto_fix` (цепочки)."""
    tab = _make_tab(BOX_UID, box_raster, monkeypatch, layout_applied=False)
    assert list(tab._editor.nodes[BOX_NID]["bbox"]) == BOX_BBOX, "фикстура сдвинулась"
    yield tab
    tab.cleanup()


@pytest.fixture
def box_layout_tab(qapp, box_raster, monkeypatch):
    """Холст после раскладки: та же кнопка уходит в `smooth_canvas`."""
    tab = _make_tab(BOX_UID, box_raster, monkeypatch, layout_applied=True)
    yield tab
    tab.cleanup()


@pytest.fixture
def poly_tab(qapp, poly_raster, monkeypatch):
    """Корпус с настоящими полигонами (`089feca2`, 4 узла с segmentation)."""
    tab = _make_tab(POLY_UID, poly_raster, monkeypatch, layout_applied=False)
    ed = tab._editor
    assert list(ed.nodes[POLY_BOX_NID]["bbox"]) == POLY_BOX_BBOX, "фикстура сдвинулась"
    assert _size(ed, POLY_NID) == pytest.approx((POLY_W, POLY_H), abs=TOL)
    assert len(ed.nodes[POLY_NID]["segmentation"]) >= 6, "node_30 не полигон"
    yield tab
    tab.cleanup()


# ── измерители: только данные ────────────────────────────────────────────

def _geom(editor):
    """Снимок геометрии всех узлов — база сравнений «данные вернулись»."""
    return json.dumps({nid: [nd.get("bbox"), nd.get("centroid"),
                             nd.get("segmentation"), nd.get("area")]
                       for nid, nd in editor.nodes.items()}, sort_keys=True)


def _size(editor, nid):
    """(ширина, высота) рамки узла — то, что оператор видит на схеме."""
    bb = editor.nodes[nid]["bbox"]
    return (bb[2] - bb[0], bb[3] - bb[1])


def _sent_bbox(tab, nid):
    """bbox узла в ПОСЛЕДНЕМ графе, который вкладка отдала серверу."""
    uploads = tab.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    for node in uploads[-1][2]["nodes"]:
        if node.get("id") == nid:
            return node.get("bbox")
    raise AssertionError(f"{nid} нет в отправленном графе")


def _last_step(editor):
    """Описание верхнего шага стека — им доказывается, ЧТО именно легло."""
    return editor.undo_mgr.undo_stack[-1].description


# ── жесты оператора ──────────────────────────────────────────────────────

def _foreign_move(editor, nid, dx=7.0):
    """ЧУЖОЕ действие оператора: перенос узла — команда, правящая НА МЕСТЕ.

    Именно она (`DragNodeCommand.undo` → `_apply_state`) двигает `revision`,
    не пересобирая модель, и делает сторож протухания лжецом.

    «Чужое» здесь про инструмент, а не про узел: жест одинаково законен и по
    узлу ВНЕ набора (`FOREIGN`), и по узлу ИЗ него (`INSIDE`) — вторая клетка
    и оказалась неперебранной (§90.15).
    """
    editor.set_mode("idle")
    cy, cx = editor.nodes[nid]["centroid"]
    editor.start_drag_node(nid)
    editor.drag_node_to(cx + dx, cy)
    editor.end_drag_node()
    assert editor.undo_mgr.can_undo, "перенос не встал в стек — тест бессмыслен"


def _open_resize(editor, cls):
    editor.set_mode("resize_objects")
    editor.set_resize_class(cls)


def _box_preview(editor):
    """Живое превью 90×90 на боксовом наборе корпуса `d74eb9f1`."""
    editor.preview_resize(width=SIDE, height=SIDE)
    assert editor.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_PREVIEW, abs=TOL), \
        "превью ничего не изменило — тест бессмыслен"


def _poly_box_preview(editor):
    """Живое превью 200×200 на боксовом наборе корпуса `089feca2`."""
    editor.preview_resize(width=POLY_SIDE, height=POLY_SIDE)
    assert editor.nodes[POLY_BOX_NID]["bbox"] == pytest.approx(
        POLY_BOX_PREVIEW, abs=TOL), "превью ничего не изменило — тест бессмыслен"


def _poly_edge_point(editor):
    """Точка на первом ребре оверлея полигона — цель Ctrl+ЛКМ «добавить»."""
    poly = editor._poly_overlay.get_polygon()
    return (poly[0] + poly[2]) / 2.0, (poly[1] + poly[3]) / 2.0


# =========================================================================
# ПОЛОВИНА А — сторож протухания: лекарство обязано работать и после
#              чужого Ctrl+Z по команде, правящей модель НА МЕСТЕ
# =========================================================================

def test_save_after_foreign_undo_sends_bbox_from_before_the_preview(
        box_tab, dialogs):
    """Минимальная форма дефекта (§83.6): три действия, ни одной кнопки-команды.

    Перенос узла → «Размеры» + превью → Ctrl+Z → «Сохранить». Две трети цены
    дефекта: своей команды тут нет вовсе, ломается путь записи 1.5.
    """
    ed = box_tab._editor
    _foreign_move(ed, FOREIGN)
    _open_resize(ed, BOX_CLASS)
    _box_preview(box_tab._editor)

    ed.undo()                                   # Ctrl+Z по чужому переносу

    assert box_tab._save_graph() is True
    assert dialogs == []

    sent = _sent_bbox(box_tab, BOX_NID)
    assert sent == pytest.approx(BOX_BBOX, abs=TOL)
    assert sent != pytest.approx(BOX_PREVIEW, abs=TOL)
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)


def test_auto_fix_after_foreign_undo_undo_returns_to_state_before_preview(
        box_tab, dialogs):
    """Кнопка «Авто-выравнивание», ветка цепочек, поверх протухшего базлайна."""
    ed = box_tab._editor
    geom0 = _geom(ed)                            # схема как её загрузили
    _foreign_move(ed, FOREIGN)
    assert _geom(ed) != geom0, "чужой перенос ничего не сдвинул"
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()                                    # базлайн объявляется мёртвым
    box_tab._auto_fix()

    assert dialogs == []
    assert _last_step(ed) == "Auto-Fix (chains)", "ушли не в ту ветку кнопки"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert _size(ed, BOX_NID) != pytest.approx((SIDE, SIDE), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_smooth_after_foreign_undo_undo_returns_to_state_before_preview(
        box_layout_tab, dialogs):
    """Та же кнопка, вторая ветка (холст после раскладки → сглаживание)."""
    ed = box_layout_tab._editor
    geom0 = _geom(ed)
    _foreign_move(ed, FOREIGN)
    assert _geom(ed) != geom0, "чужой перенос ничего не сдвинул"
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()
    box_layout_tab._auto_fix()

    assert dialogs == []
    assert _last_step(ed) == "Сглаживание", "ушли не в ту ветку кнопки"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_optimize_all_after_foreign_undo_undo_returns_to_state_before_preview(
        box_tab, dialogs):
    """Кнопка «Оптимизировать все» — третий потребитель лекарства."""
    ed = box_tab._editor
    geom0 = _geom(ed)
    _foreign_move(ed, FOREIGN)
    assert _geom(ed) != geom0, "чужой перенос ничего не сдвинул"
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()
    box_tab._optimize_all_edges()

    assert dialogs == []
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _geom(ed) == geom0


def test_batch_delete_after_foreign_undo_undo_returns_to_state_before_preview(
        box_tab, dialogs):
    """Кнопка «Удалить выделенное» — четвёртый потребитель лекарства."""
    ed = box_tab._editor
    geom0 = _geom(ed)
    _foreign_move(ed, FOREIGN)
    assert _geom(ed) != geom0, "чужой перенос ничего не сдвинул"
    _open_resize(ed, BOX_CLASS)
    ed.selected_nodes.add("node_30")
    _box_preview(ed)

    ed.undo()
    box_tab._batch_delete()

    assert dialogs == []
    assert "node_30" not in ed.nodes, "жертва не удалилась — тест бессмыслен"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_ocr_funnel_after_foreign_undo_undo_returns_to_state_before_preview(
        box_tab, dialogs):
    """Воронка 1-32 (`_ocr_push_snapshot`) — пятый потребитель лекарства.

    Одного пути хватает: это ЕДИНСТВЕННАЯ точка построения точек возврата
    всего OCR-слоя (§83.19), остальные четыре идут через неё.
    """
    ed = box_tab._editor
    geom0 = _geom(ed)
    _foreign_move(ed, FOREIGN)
    assert _geom(ed) != geom0, "чужой перенос ничего не сдвинул"
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()
    cmd = ed._ocr_push_snapshot("Проба OCR-команды")
    ed._ocr_commit(cmd)

    assert dialogs == []
    assert _last_step(ed) == "Проба OCR-команды"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_apply_resize_after_foreign_undo_undo_returns_the_original_size(
        box_tab, dialogs):
    """§83.9 — худший случай половины А: отмена не возвращала 20×36 ВОВСЕ.

    При протухшем базлайне `apply_resize` снимал точку возврата ПОВЕРХ живого
    превью, и отмена до дна стека оставляла узел 90×90.
    """
    ed = box_tab._editor
    _foreign_move(ed, FOREIGN)
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()
    ed.apply_resize(width=SIDE, height=SIDE)

    assert dialogs == []
    assert _size(ed, BOX_NID) == pytest.approx((SIDE, SIDE), abs=TOL), \
        "«Применить» не применило размер — тест бессмыслен"

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert _size(ed, BOX_NID) != pytest.approx((SIDE, SIDE), abs=TOL)


def test_drop_works_when_a_return_point_was_built_past_it(box_tab, dialogs):
    """Лекарство — МЕХАНИЗМ, а не набор заплат на известных путях.

    Ревизия назвала дефект «в механизме, а не в пути»: сторож протухания
    считает `revision`, а он растёт на ЛЮБОЙ мутации стека. Здесь точка
    возврата строится МИМО дропа (как это делает любой ещё не найденный путь),
    и лекарство обязано сработать всё равно — иначе следующий такой путь снова
    даст молчаливую порчу.
    """
    from ui.editors.commands.advanced_commands import AutoFixCommand

    ed = box_tab._editor
    geom0 = _geom(ed)
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    foreign = AutoFixCommand(ed.model, ed._redraw_all)     # мимо дропа
    foreign.description = "Чужая команда мимо дропа"
    foreign.execute()
    foreign.finalize()
    ed.undo_mgr.push_executed(foreign)

    assert not ed._resize_baseline_alive() or True         # состояние не важно
    ed.drop_uncommitted_preview()

    assert dialogs == []
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert _geom(ed) == geom0


# =========================================================================
# ПОЛОВИНА Б — пути БЕЗ дропа: базлайн ЖИВ, Ctrl+Z не нужен
# =========================================================================

def test_polygon_editing_over_live_preview_undo_returns_to_state_before_preview(
        poly_tab, dialogs):
    """§83.26: правка полигона живёт ВНУТРИ «Размеров» и точку возврата строит.

    `_enter_polygon_editing_mode` не зовёт `set_mode`, а ветка полигона
    в `mousePressEvent` стоит раньше сторожа резайза — режим остаётся
    `resize_objects`, превью живо, и шаг «Добавить вершину» ложится поверх него.
    """
    ed = poly_tab._editor
    geom0 = _geom(ed)
    _open_resize(ed, POLY_CLASS)
    _poly_box_preview(ed)

    ed._enter_polygon_editing_mode(POLY_NID)     # Ctrl+2ЛКМ по полигону
    assert ed._poly_edit_node == POLY_NID, "в правку полигона не вошли"
    ed._poly_add_vertex(0, *_poly_edge_point(ed))   # Ctrl+ЛКМ по ребру

    assert dialogs == []
    assert _last_step(ed) == "Добавить вершину", "шаг не построен — тест бессмыслен"
    assert _size(ed, POLY_BOX_NID) == pytest.approx((POLY_BOX_W, POLY_BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_polygon_vertex_delete_over_live_preview_does_not_weld_it(
        poly_tab, dialogs):
    """Тот же путь вторым жестом: Ctrl+ПКМ по вершине — «Удалить вершину»."""
    ed = poly_tab._editor
    geom0 = _geom(ed)
    _open_resize(ed, POLY_CLASS)
    _poly_box_preview(ed)

    ed._enter_polygon_editing_mode(POLY_NID)
    poly = ed._poly_overlay.get_polygon()
    ed._ctrl_right_click_delete(poly[0], poly[1])   # Ctrl+ПКМ по вершине 0

    assert dialogs == []
    assert _last_step(ed) == "Удалить вершину", "шаг не построен — тест бессмыслен"
    assert _size(ed, POLY_BOX_NID) == pytest.approx((POLY_BOX_W, POLY_BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_polygon_vertex_drag_over_live_preview_does_not_weld_it(
        poly_tab, dialogs):
    """Третий жест: Ctrl+ЛКМ-протяжка вершины — «Двигать вершину»."""
    ed = poly_tab._editor
    geom0 = _geom(ed)
    _open_resize(ed, POLY_CLASS)
    _poly_box_preview(ed)

    ed._enter_polygon_editing_mode(POLY_NID)
    poly = ed._poly_overlay.get_polygon()
    ed._ctrl_lmb_start_x, ed._ctrl_lmb_start_y = poly[0], poly[1]
    ed._start_ctrl_drag(POLY_NID)
    assert ed._poly_overlay.is_dragging, "протяжка вершины не началась"
    ed._update_ctrl_drag(poly[0] + 25.0, poly[1] + 25.0)
    ed._end_ctrl_drag()

    assert dialogs == []
    assert _last_step(ed) == "Двигать вершину", "шаг не построен — тест бессмыслен"
    assert _size(ed, POLY_BOX_NID) == pytest.approx((POLY_BOX_W, POLY_BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_unpin_endpoint_over_live_preview_does_not_weld_it(poly_tab, dialogs):
    """§83.28: ПКМ по приколотому концу трубы — `contextMenuEvent` без сторожа.

    Ни `_current_mode`, ни `display_regime` он не проверяет, а `_unpin_endpoint`
    строит `AutoFixCommand` мимо дропа. Пин ставится штатным API портовой
    модели — тем же, что пишет жест Э5.
    """
    from ui.editors import port_model as pm

    ed = poly_tab._editor
    edge = next(e for e in ed.edges_data
                if POLY_BOX_NID in (e.get("source"), e.get("target")))
    role = "source" if edge["source"] == POLY_BOX_NID else "target"
    key = ed.model.edge_key(edge["source"], edge["target"])
    point = edge[f"{role}_point"]
    pm.set_edge_pin(ed.nodes[POLY_BOX_NID], edge, role, point[0], point[1])
    assert pm.edge_pin(edge, role) is not None, "пин не поставился"

    geom0 = _geom(ed)
    _open_resize(ed, POLY_CLASS)
    _poly_box_preview(ed)

    assert ed._unpin_endpoint(key, role) is True

    assert dialogs == []
    assert _last_step(ed) == "Отвязать вход", "шаг не построен — тест бессмыслен"
    assert _size(ed, POLY_BOX_NID) == pytest.approx((POLY_BOX_W, POLY_BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_paste_ghost_over_live_preview_does_not_weld_it(box_tab, dialogs):
    """§86.13 (находка этой доработки): призрак вставки переживает «Размеры».

    Ctrl+V в базовом состоянии поднимает призрак, кнопка «Размеры» его не
    гасит (гасит только перерисовка сцены), и Ctrl+ЛКМ фиксирует вставку
    `SnapshotCommand`-ом мимо дропа — при ЖИВОМ базлайне, как и полигон с ПКМ.
    """
    ed = box_tab._editor
    ed.set_mode("idle")
    ed.selected_nodes.add(FOREIGN)
    ed._copy_selected_nodes()
    assert (ed._node_clipboard or {}).get("nodes"), "буфер пуст — тест бессмыслен"
    ed._start_paste_ghost()                      # Ctrl+V
    assert ed._paste_ghost is not None

    geom0 = _geom(ed)
    _open_resize(ed, BOX_CLASS)
    assert ed._paste_ghost is not None, "призрак не пережил вход — путь недостижим"
    _box_preview(ed)

    ed._commit_paste_ghost()                     # Ctrl+ЛКМ

    assert dialogs == []
    assert _last_step(ed) == "Вставить узлы", "шаг не построен — тест бессмыслен"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


def test_preview_raised_inside_polygon_editing_is_not_welded(poly_tab, dialogs):
    """Обратный порядок жестов: сначала правка полигона, превью — ПОСЛЕ входа.

    Панель «Размеров» из правки полигона не исчезает, поэтому превью можно
    поднять и после входа. Вход дропом эту половину не закрывает — точку
    возврата строит каждая операция с вершинами.
    """
    ed = poly_tab._editor
    geom0 = _geom(ed)
    _open_resize(ed, POLY_CLASS)
    ed._enter_polygon_editing_mode(POLY_NID)
    _poly_box_preview(ed)                        # превью ПОСЛЕ входа

    ed._poly_add_vertex(0, *_poly_edge_point(ed))

    assert dialogs == []
    assert _last_step(ed) == "Добавить вершину"
    assert _size(ed, POLY_BOX_NID) == pytest.approx((POLY_BOX_W, POLY_BOX_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


# =========================================================================
# ПОЛИГОНЫ — цена дефекта здесь ДРУГАЯ: не отмена, а исходный размер
# =========================================================================

def test_polygon_apply_after_foreign_undo_is_not_squared(poly_tab, dialogs):
    """§83.27: `_resize_node_poly` масштабирует ОТНОСИТЕЛЬНО базлайна.

    Пересъём базлайна поверх живого превью возводит масштаб в квадрат:
    605×75 → превью ×2 → 1210×150 → «Применить» ×2 → 2420×300. Отмена
    до дна стека возвращала только к превью — исходные 605×75 недостижимы.
    """
    ed = poly_tab._editor
    _foreign_move(ed, POLY_FOREIGN)
    ed.set_mode("resize_objects")
    ed._resize_class = "unknow"
    ed._resize_sel = {POLY_NID}                  # чисто полигонный набор
    ed._update_resize_panel()
    assert ed._resize_kind() == "poly", "набор не полигонный — тест бессмыслен"

    ed.preview_resize(scale=2.0)
    assert _size(ed, POLY_NID) == pytest.approx((2 * POLY_W, 2 * POLY_H), abs=TOL)

    ed.undo()                                    # базлайн объявляется мёртвым
    ed.apply_resize(scale=2.0)

    assert dialogs == []
    assert _size(ed, POLY_NID) == pytest.approx((2 * POLY_W, 2 * POLY_H), abs=TOL)
    assert _size(ed, POLY_NID) != pytest.approx((4 * POLY_W, 4 * POLY_H), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _size(ed, POLY_NID) == pytest.approx((POLY_W, POLY_H), abs=TOL)


def test_polygon_vertex_edit_over_its_own_preview_keeps_the_original_size(
        poly_tab, dialogs):
    """Превью САМОГО полигона + правка его вершин: исходный размер обязан
    остаться достижимым.

    Оверлей вершин строится из `segmentation` узла, то есть из ПРЕВЬЮ.
    Если снять превью, не пересобрав оверлей, `_poly_write_node` впишет
    масштабированный контур обратно — уже отдельным шагом undo.
    """
    ed = poly_tab._editor
    geom0 = _geom(ed)
    ed.set_mode("resize_objects")
    ed._resize_class = "unknow"
    ed._resize_sel = {POLY_NID}
    ed._update_resize_panel()

    ed.preview_resize(scale=2.0)
    assert _size(ed, POLY_NID) == pytest.approx((2 * POLY_W, 2 * POLY_H), abs=TOL)

    ed._enter_polygon_editing_mode(POLY_NID)
    ed._poly_add_vertex(0, *_poly_edge_point(ed))

    assert dialogs == []
    assert _last_step(ed) == "Добавить вершину"
    assert _size(ed, POLY_NID) == pytest.approx((POLY_W, POLY_H), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _geom(ed) == geom0


# =========================================================================
# ОБРАТНАЯ ГРАНИЦА И5 — картинку у оператора не отбираем без команды
# =========================================================================

def test_unpin_without_a_pin_keeps_the_preview_alive(poly_tab, dialogs):
    """ПКМ по НЕприколотому концу команды не строит — превью остаётся живым."""
    ed = poly_tab._editor
    from ui.editors import port_model as pm

    edge = next(e for e in ed.edges_data
                if POLY_BOX_NID in (e.get("source"), e.get("target")))
    role = "source" if edge["source"] == POLY_BOX_NID else "target"
    key = ed.model.edge_key(edge["source"], edge["target"])
    assert pm.edge_pin(edge, role) is None, "конец уже приколот — тест бессмыслен"

    _open_resize(ed, POLY_CLASS)
    _poly_box_preview(ed)
    depth0 = ed.undo_mgr.stack_depth

    assert ed._unpin_endpoint(key, role) is False

    assert dialogs == []
    assert ed.undo_mgr.stack_depth == depth0, "команда всё-таки встала в стек"
    assert ed.nodes[POLY_BOX_NID]["bbox"] == pytest.approx(POLY_BOX_PREVIEW, abs=TOL)


def test_polygon_entry_on_a_box_keeps_the_preview_alive(poly_tab, dialogs):
    """Вход в правку полигона по БОКСУ невозможен — превью остаётся живым."""
    ed = poly_tab._editor
    _open_resize(ed, POLY_CLASS)
    _poly_box_preview(ed)

    ed._enter_polygon_editing_mode(POLY_BOX_NID)   # у бокса нет segmentation

    assert dialogs == []
    assert ed._poly_edit_node is None, "вошли в правку полигона по боксу"
    assert ed.nodes[POLY_BOX_NID]["bbox"] == pytest.approx(POLY_BOX_PREVIEW, abs=TOL)


def test_empty_paste_buffer_keeps_the_preview_alive(box_tab, dialogs):
    """Ctrl+V при пустом буфере призрака не поднимает — снимать нечего."""
    ed = box_tab._editor
    ed.set_mode("idle")
    assert not (ed._node_clipboard or {}).get("nodes")
    ed._start_paste_ghost()
    assert ed._paste_ghost is None, "призрак поднялся из пустого буфера"

    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)
    depth0 = ed.undo_mgr.stack_depth

    ed._commit_paste_ghost()                       # призрака нет — no-op

    assert dialogs == []
    assert ed.undo_mgr.stack_depth == depth0
    assert ed.nodes[BOX_NID]["bbox"] == pytest.approx(BOX_PREVIEW, abs=TOL)


# =========================================================================
# ПОЛОВИНА В — СМЕШАННОЕ состояние набора: чужой откат коснулся ОДНОГО
#              его узла, остальных не трогал. Сторож обязан отвечать
#              ПОЭЛЕМЕНТНО, а не одним «да/нет» на всё множество
#              (`PROTOCOL §3`, третий возврат пункта)
# =========================================================================

def _sent_geom(tab):
    """Геометрия ВСЕХ узлов в последнем графе, ушедшем серверу.

    Сверяется целиком, а не по одному узлу: цена дефекта — превью у 16 узлов
    из 17, и проверка «свидетель вернулся» одна прошла бы мимо остальных.
    """
    uploads = tab.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    return {n["id"]: [n.get("bbox"), n.get("centroid"),
                      n.get("segmentation"), n.get("area")]
            for n in uploads[-1][2]["nodes"]}


def _model_geom(editor):
    """То же по модели — ключи те же, что у `_sent_geom`."""
    return {nid: [nd.get("bbox"), nd.get("centroid"),
                  nd.get("segmentation"), nd.get("area")]
            for nid, nd in editor.nodes.items()}


def _open_poly_set(editor):
    """Штатный полигонный набор: класс целиком → кнопка «только полигоны»."""
    editor.set_mode("resize_objects")
    editor.set_resize_class(POLY_SET_CLASS)      # 7 узлов, набор смешанный
    editor.resize_filter("poly")                 # кнопка панели → 4 полигона
    assert editor._resize_kind() == "poly", "набор не полигонный — тест бессмыслен"
    assert editor._resize_sel == POLY_SET, "состав набора сдвинулся"


def test_save_after_undo_of_a_move_inside_the_set_sends_no_preview_at_all(
        box_tab, dialogs):
    """§90.15 — минимальная форма третьего возврата: три штатных действия.

    Перенос узла ИЗ набора → «Размеры» + превью → Ctrl+Z → «Сохранить».
    Откат честно стирает превью на перенесённом узле, и агрегатный сторож
    объявлял базлайн мёртвым для всех 17 — превью ОСТАЛЬНЫХ уезжало серверу.
    """
    ed = box_tab._editor
    geom0 = _model_geom(ed)
    _foreign_move(ed, INSIDE)
    _open_resize(ed, BOX_CLASS)
    assert INSIDE in ed._resize_sel, "перенесённый узел не попал в набор"
    assert len(ed._resize_sel) == 17, "набор класса сдвинулся — тест бессмыслен"
    _box_preview(box_tab._editor)

    ed.undo()                                   # Ctrl+Z по переносу ИЗ набора

    assert box_tab._save_graph() is True
    assert dialogs == []
    assert ed.undo_mgr.stack_depth == 0
    assert ed.undo_mgr.can_undo is False

    sent = _sent_geom(box_tab)
    assert sent[BOX_NID][0] == pytest.approx(BOX_BBOX, abs=TOL)
    assert sent[BOX_NID][0] != pytest.approx(BOX_PREVIEW, abs=TOL)
    # Ни у одного узла набора, а не только у свидетеля.
    assert [nid for nid in geom0 if sent[nid] != geom0[nid]] == []


def test_area_of_the_node_the_undo_touched_is_not_left_at_the_preview_value(
        box_tab, dialogs):
    """Вторая ось того же дефекта: откат вернул узлу НЕ ВСЕ поля.

    `DragNodeCommand.undo` кладёт назад centroid/bbox/segmentation и не
    трогает `area` — превью в непокрытом поле пережило бы откат навсегда
    и уехало на сервер (замер §97.3: 1428 → 8100 у самого перенесённого).
    Поэтому сторож сверяет отпечаток ПО ПОЛЯМ, а не одним кортежем.
    """
    ed = box_tab._editor
    _foreign_move(ed, INSIDE)
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)
    assert ed.nodes[INSIDE]["area"] == pytest.approx(INSIDE_PREVIEW_AREA, abs=TOL), \
        "превью не тронуло area — тест бессмыслен"

    ed.undo()

    assert box_tab._save_graph() is True
    assert dialogs == []

    sent = _sent_geom(box_tab)
    assert sent[INSIDE][3] == pytest.approx(INSIDE_AREA, abs=TOL)
    assert sent[INSIDE][3] != pytest.approx(INSIDE_PREVIEW_AREA, abs=TOL)
    assert sent[BOX_NID][3] == pytest.approx(BOX_AREA, abs=TOL)


def test_the_node_the_undo_touched_keeps_what_its_own_undo_left(
        box_tab, dialogs):
    """Граница §48 на смешанном наборе: чужой откат НЕ отменяется.

    Два переноса подряд, отмена одного: перенесённый узел обязан остаться
    там, куда его вернул ОПЕРАТОР, — не на исходном месте и не в превью.
    Без второго переноса «после отката» и «исходное» совпадают, и тест не
    отличил бы возврат по базлайну от правильного поведения.
    """
    ed = box_tab._editor
    _foreign_move(ed, INSIDE, dx=7.0)
    _foreign_move(ed, INSIDE, dx=11.0)
    assert ed.undo_mgr.stack_depth == 2, "два переноса — не два шага стека"
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()                                   # отменён ТОЛЬКО второй перенос
    after_undo = list(ed.nodes[INSIDE]["centroid"])
    assert after_undo != INSIDE_CENTROID, "отмена вернула узел на исходное место"

    assert box_tab._save_graph() is True
    assert dialogs == []

    sent = _sent_geom(box_tab)
    assert sent[INSIDE][1] == pytest.approx(after_undo, abs=TOL)
    assert sent[INSIDE][1] != pytest.approx(INSIDE_CENTROID, abs=TOL)
    assert (sent[INSIDE][0][2] - sent[INSIDE][0][0]) == pytest.approx(
        INSIDE_BBOX[2] - INSIDE_BBOX[0], abs=TOL), "у перенесённого узла превью"
    assert sent[BOX_NID][0] == pytest.approx(BOX_BBOX, abs=TOL)


_MIXED_COMMANDS = {
    "auto_fix": lambda tab: tab._auto_fix(),
    "optimize_all": lambda tab: tab._optimize_all_edges(),
    "batch_delete": lambda tab: tab._batch_delete(),
    "ocr_funnel": lambda tab: tab._editor._ocr_commit(
        tab._editor._ocr_push_snapshot("Проба OCR-команды")),
}


@pytest.mark.parametrize("action", sorted(_MIXED_COMMANDS))
def test_command_after_undo_of_a_move_inside_the_set_does_not_weld_the_preview(
        box_tab, dialogs, action):
    """П. 3 объёма: четыре кнопки и воронка OCR на СМЕШАННОМ наборе.

    Все они ходят через `drop_uncommitted_preview()`, поэтому агрегатный
    вердикт выключал их все разом — здесь каждая проверена своим прогоном.
    """
    ed = box_tab._editor
    geom0 = _model_geom(ed)
    _foreign_move(ed, INSIDE)
    _open_resize(ed, BOX_CLASS)
    if action == "batch_delete":
        ed.selected_nodes.add("node_30")         # жертва ВНЕ набора «Размеров»
    _box_preview(ed)

    ed.undo()                                    # чужой откат по узлу ИЗ набора
    assert ed.undo_mgr.stack_depth == 0
    _MIXED_COMMANDS[action](box_tab)

    assert dialogs == []
    assert ed.undo_mgr.stack_depth >= 1, "команда не построила шаг — тест бессмыслен"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)
    assert _size(ed, BOX_NID) != pytest.approx((SIDE, SIDE), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _model_geom(ed) == geom0


def test_smooth_after_undo_of_a_move_inside_the_set_does_not_weld_the_preview(
        box_layout_tab, dialogs):
    """Вторая ветка той же кнопки (холст после раскладки → сглаживание)."""
    ed = box_layout_tab._editor
    geom0 = _model_geom(ed)
    _foreign_move(ed, INSIDE)
    _open_resize(ed, BOX_CLASS)
    _box_preview(ed)

    ed.undo()
    box_layout_tab._auto_fix()

    assert dialogs == []
    assert _last_step(ed) == "Сглаживание", "ушли не в ту ветку кнопки"
    assert _size(ed, BOX_NID) == pytest.approx((BOX_W, BOX_H), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _model_geom(ed) == geom0


def test_polygon_apply_after_undo_of_a_move_inside_the_set_is_not_squared(
        poly_tab, dialogs):
    """§90.16 — цена дефекта на полигонах: масштаб в квадрате.

    Набор из четырёх полигонов, чужой откат по ОДНОМУ из них. Пересъём
    базлайна поверх живого превью остальных давал 605×75 → ×2 → «Применить»
    ×2 → 2420×300, и исходные 605×75 не достижимы никаким числом Ctrl+Z.
    """
    ed = poly_tab._editor
    _foreign_move(ed, POLY_INSIDE)
    _open_poly_set(ed)

    ed.preview_resize(scale=2.0)
    assert _size(ed, POLY_NID) == pytest.approx((2 * POLY_W, 2 * POLY_H), abs=TOL), \
        "превью ничего не изменило — тест бессмыслен"

    ed.undo()                                    # чужой откат по узлу ИЗ набора
    ed.apply_resize(scale=2.0)

    assert dialogs == []
    assert _size(ed, POLY_NID) == pytest.approx((2 * POLY_W, 2 * POLY_H), abs=TOL)
    assert _size(ed, POLY_NID) != pytest.approx((4 * POLY_W, 4 * POLY_H), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _size(ed, POLY_NID) == pytest.approx((POLY_W, POLY_H), abs=TOL)
    assert _size(ed, POLY_INSIDE) == pytest.approx(
        (POLY_INSIDE_W, POLY_INSIDE_H), abs=TOL)


def test_polygon_preview_repeated_after_undo_of_a_move_inside_the_set_is_not_squared(
        poly_tab, dialogs):
    """Тот же квадрат на СОСЕДНЕМ пути: второе превью вместо «Применить».

    Пересъём базлайна живёт и в `preview_resize`, и в `apply_resize` — путь
    «бегунок дёрнули ещё раз» достижим тем же жестом и той же ценой.
    """
    ed = poly_tab._editor
    _foreign_move(ed, POLY_INSIDE)
    _open_poly_set(ed)

    ed.preview_resize(scale=2.0)
    ed.undo()
    ed.preview_resize(scale=2.0)                 # второй тик бегунка

    assert dialogs == []
    assert _size(ed, POLY_NID) == pytest.approx((2 * POLY_W, 2 * POLY_H), abs=TOL)
    assert _size(ed, POLY_NID) != pytest.approx((4 * POLY_W, 4 * POLY_H), abs=TOL)

    ed._exit_resize_objects()                    # Esc — превью брошено

    assert _size(ed, POLY_NID) == pytest.approx((POLY_W, POLY_H), abs=TOL)
