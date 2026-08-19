# -*- coding: utf-8 -*-
"""Пункт 1.4 дороги, ДОРАБОТКА по возврату ревизии связки 1.1+1.4+1.5 (2026-08-19).

Зачем. Живое превью панели «Размеры» мутирует МОДЕЛЬ мимо стека команд. Пока
оператор не нажал «Применить», это не правка, а картинка: её снимают выход из
режима, смена набора (1.4) и запись на сервер (1.5). Дефект нашла ревизия
СВЯЗКОЙ — на шве, которого не видел ни один из трёх собственных гейтов:

    превью → кнопка «Авто-выравнивание» → Ctrl+Z

Кнопка строит `SnapshotCommand`, а его `_before` снимается с модели КАК ЕСТЬ,
то есть **вместе с превью**. Дальше `undo_mgr.revision` растёт, сторож
протухания (`_resize_baseline_alive`) объявляет базлайн мёртвым — и лечение
1.4 «протух → бросить без отката» доводит дело до конца: превью остаётся жить,
вернуть его нечем. Замер `MEASUREMENTS §52` до правки, обе ветки кнопки:
Ctrl+Z возвращает `node_11` = `[-10.0, 188.0, 80.0, 278.0]` (превью 90×90)
вместо исходных `[25, 215, 45, 251]` (20×36), и то же самое уходит на сервер.

Лечение «бросить без отката» верно ровно для одного случая — undo/redo:
`model.restore` пересобирает модель, и превью физически исчезает вместе с ней.
Для КОМАНД-НА-МЕСТЕ оно неверно. Поздний откат тут не помогает: `_before`
команды уже отравлен, и Ctrl+Z вернул бы превью обратно даже из чистой модели.
Значит превью снимается ДО того, как команда снимет свою точку возврата.

Инвариант пункта: **Ctrl+Z после любой команды-на-месте, нажатой при живом
превью, возвращает схему к состоянию ДО превью — бит-в-бит.** Проверяются
только ДАННЫЕ (принцип набора 0.4): геометрия узлов, JSON, ушедший серверу,
глубина стека. Ни одного утверждения про `_current_mode`.

Числа абсолютные и заперты с двух сторон: и «размер вернулся к 20×36», и «это
не 90×90 превью» — иначе тест остался бы зелёным на пустом графе.

⚠ Обстановка: `_auto_fix` глушит исключение `QMessageBox.warning`, и падение
внутри кнопки подвесило бы набор модальным диалогом вместо падения (четвёртый
исход `PROTOCOL §5`, пойман в 1.5 и независимо в 1.3). Диалог подменён, и
каждый тест утверждает ФАКТ его невызова — таймаут тут не судья.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox     # noqa: E402
from PySide6.QtGui import QImage, QColor                    # noqa: E402

from tools import corpus                                    # noqa: E402

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
CLASS = "armatura_ruchn"  # 17 экземпляров, все — боксы
NID = "node_11"           # bbox [25, 215, 45, 251] = 20×36, centroid [233, 35]
VICTIM = "node_20"        # узел вне набора — жертва «Удалить выделенное»

BASE_BBOX = [25, 215, 45, 251]
BASE_W, BASE_H = 20, 36                   # исходный размер рамки
SIDE = 90                                 # цель превью: квадрат 90×90
PREVIEW_BBOX = [-10.0, 188.0, 80.0, 278.0]
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


@pytest.fixture(scope="module")
def raster(tmp_path_factory):
    """Белый растр размером с корпусную схему (редактор грузит граф + картинку)."""
    path = corpus.graph_path(UID)
    assert path is not None, f"корпус-фикстура {UID} не найдена (tools/corpus.py)"
    h, w = json.loads(path.read_text(encoding="utf-8"))["graph"]["image_size"]
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    png = tmp_path_factory.mktemp("raster") / f"{UID}.png"
    assert img.save(str(png))
    return str(png)


@pytest.fixture
def dialogs(monkeypatch):
    """Подменённый модальный диалог + журнал вызовов.

    Без подмены зелёный путь молчит, а КРАСНЫЙ вешает набор: `_auto_fix` и
    `_save_graph` показывают `QMessageBox.warning` из-под `except`. Висящий
    набор на CI читается как «долго», а не как «сломано» (`PROTOCOL §5`).
    """
    calls = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **kw: calls.append(a[1:3])))
    return calls


def _make_tab(raster, monkeypatch, layout_applied):
    from ui.tabs.base_graph_tab import BaseGraphTab
    from ui.tabs.advanced_graph_tab import AdvancedGraphTab
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor
    from modules.graph.core import canvas_state

    monkeypatch.setattr(BaseGraphTab, "_download_artifacts", lambda self: None)

    tab = AdvancedGraphTab(UID, "проба 1.4-fix", FakeAPI())
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    if layout_applied:
        # Ветка кнопки выбирается по метке холста: с раскладкой — сглаживание
        # (`smooth_canvas`), без неё — родной `auto_fix`. Корпус-фикстура метки
        # не несёт, поэтому вторая ветка ставится меткой.
        editor.graph_data.setdefault("graph", {})["canvas_transform"] = {
            "layout_applied": True,
            "layout_version": canvas_state.layout_version(),
        }
    assert canvas_state.has_layout(editor.graph_data or {}) is layout_applied
    tab._editor = editor
    tab._on_editor_ready()
    editor.set_mode("resize_objects")
    editor.set_resize_class(CLASS)
    assert editor._resize_kind() == "box", "набор должен быть чисто боксовым"
    assert list(editor.nodes[NID]["bbox"]) == BASE_BBOX, "фикстура сдвинулась"
    return tab


@pytest.fixture
def fallback_tab(qapp, raster, monkeypatch):
    """Вкладка на фолбэк-холсте: кнопка уходит в `auto_fix` (цепочки)."""
    tab = _make_tab(raster, monkeypatch, layout_applied=False)
    yield tab
    tab.cleanup()


@pytest.fixture
def layout_tab(qapp, raster, monkeypatch):
    """Вкладка на холсте после раскладки: кнопка уходит в `smooth_canvas`."""
    tab = _make_tab(raster, monkeypatch, layout_applied=True)
    yield tab
    tab.cleanup()


def _geom(editor):
    """Снимок геометрии всех узлов — база сравнений «данные вернулись»."""
    return json.dumps({nid: [nd.get("bbox"), nd.get("centroid"),
                             nd.get("segmentation"), nd.get("area")]
                       for nid, nd in editor.nodes.items()}, sort_keys=True)


def _size(editor, nid=NID):
    """(ширина, высота) рамки узла — то, что оператор видит на схеме."""
    bb = editor.nodes[nid]["bbox"]
    return (bb[2] - bb[0], bb[3] - bb[1])


def _sent_bbox(tab, nid=NID):
    """bbox узла в ПОСЛЕДНЕМ графе, который вкладка отдала серверу."""
    uploads = tab.api_client.uploads
    assert uploads, "на сервер не ушло ничего — сверять нечего"
    for node in uploads[-1][2]["nodes"]:
        if node.get("id") == nid:
            return node.get("bbox")
    raise AssertionError(f"{nid} нет в отправленном графе")


def _preview(tab):
    """Живое превью 90×90 на наборе — то, что оператор видит до «Применить»."""
    ed = tab._editor
    ed.preview_resize(width=SIDE, height=SIDE)
    assert ed.nodes[NID]["bbox"] == pytest.approx(PREVIEW_BBOX, abs=TOL), \
        "превью ничего не изменило — тест бессмыслен"


def _last_step(editor):
    """Описание верхнего шага стека отмены — им доказывается ВЕТКА кнопки."""
    return editor.undo_mgr.undo_stack[-1].description


# ── шов: «Авто-выравнивание» поверх живого превью, обе ветки ──────────────

def test_auto_fix_fallback_branch_undo_returns_to_state_before_preview(
        fallback_tab, dialogs):
    """Фолбэк-холст: Ctrl+Z после кнопки возвращает схему к состоянию до превью."""
    ed = fallback_tab._editor
    geom0 = _geom(ed)

    _preview(fallback_tab)
    fallback_tab._auto_fix()

    assert dialogs == [], "кнопка упала в диалог — судить нечем"
    assert _last_step(ed) == "Auto-Fix (chains)", "ушли не в ту ветку кнопки"
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)
    assert _size(ed) != pytest.approx((SIDE, SIDE), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)


def test_auto_fix_layout_branch_undo_returns_to_state_before_preview(
        layout_tab, dialogs):
    """Холст после раскладки: то же самое через ветку сглаживания."""
    ed = layout_tab._editor
    geom0 = _geom(ed)

    _preview(layout_tab)
    layout_tab._auto_fix()

    assert dialogs == [], "кнопка упала в диалог — судить нечем"
    assert _last_step(ed) == "Сглаживание", "ушли не в ту ветку кнопки"
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)
    assert _size(ed) != pytest.approx((SIDE, SIDE), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)


def test_auto_fix_over_preview_does_not_upload_it(fallback_tab, dialogs):
    """Превью, пережившее кнопку, уезжало на сервер — «Сохранить» после кнопки."""
    _preview(fallback_tab)
    fallback_tab._auto_fix()

    assert fallback_tab._save_graph() is True
    assert dialogs == []

    bb = _sent_bbox(fallback_tab)
    assert bb[2] - bb[0] == pytest.approx(BASE_W, abs=TOL)
    assert bb[3] - bb[1] == pytest.approx(BASE_H, abs=TOL)
    assert bb != pytest.approx(PREVIEW_BBOX, abs=TOL)


# ── та же семья: остальные команды-на-месте, достижимые в режиме ──────────

def test_optimize_all_edges_over_preview_leaves_nothing_to_weld(
        fallback_tab, dialogs):
    """Кнопка «Оптимизировать все» — та же команда-на-месте (замер §52)."""
    ed = fallback_tab._editor
    geom0 = _geom(ed)

    _preview(fallback_tab)
    fallback_tab._optimize_all_edges()

    assert dialogs == []
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)

    while ed.undo_mgr.can_undo:
        ed.undo()

    assert _geom(ed) == geom0


def test_batch_delete_over_preview_leaves_nothing_to_weld(fallback_tab, dialogs):
    """«Удалить выделенное» (Delete) — та же команда-на-месте (замер §52)."""
    ed = fallback_tab._editor
    geom0 = _geom(ed)
    ed.selected_nodes.add(VICTIM)

    _preview(fallback_tab)
    fallback_tab._batch_delete()

    assert dialogs == []
    assert VICTIM not in ed.nodes, "жертва не удалилась — тест бессмыслен"
    assert _size(ed) == pytest.approx((BASE_W, BASE_H), abs=TOL)

    ed.undo()

    assert _geom(ed) == geom0


# ── характеризация: чего снятие превью ломать не должно ──────────────────

def test_command_without_live_preview_is_untouched(fallback_tab, dialogs):
    """Без живого превью кнопка работает как раньше: точка возврата — «Применить».

    Запирает правку с другой стороны: снятие превью не имеет права откатывать
    ЗАФИКСИРОВАННЫЙ размер.
    """
    ed = fallback_tab._editor

    _preview(fallback_tab)
    ed.apply_resize(width=SIDE, height=SIDE)     # превью подтверждено командой
    applied = _geom(ed)
    assert _size(ed) == pytest.approx((SIDE, SIDE), abs=TOL)

    fallback_tab._auto_fix()

    assert dialogs == []
    assert _size(ed) == pytest.approx((SIDE, SIDE), abs=TOL), \
        "снятие превью откатило подтверждённый размер"

    ed.undo()

    assert _geom(ed) == applied


def test_command_that_pushes_nothing_keeps_the_preview_alive(
        fallback_tab, dialogs):
    """Кнопка, не построившая точки возврата, превью НЕ снимает.

    «Удалить выделенное» при пустом выделении уходит в «Нечего удалять»:
    вваривать нечего, значит и отбирать у оператора картинку не за что.
    """
    ed = fallback_tab._editor
    assert not ed.selected_nodes and not ed.selected_edges

    _preview(fallback_tab)
    depth0 = len(ed.undo_mgr.undo_stack)
    fallback_tab._batch_delete()

    assert dialogs == []
    assert len(ed.undo_mgr.undo_stack) == depth0, "команда всё-таки встала в стек"
    assert _size(ed) == pytest.approx((SIDE, SIDE), abs=TOL)


def test_preview_alone_still_reverts_on_mode_exit(fallback_tab, dialogs):
    """Штатный выход 1.4 жив: без команды превью снимается выходом из режима."""
    ed = fallback_tab._editor
    geom0 = _geom(ed)

    _preview(fallback_tab)
    ed.set_mode("idle")

    assert dialogs == []
    assert _geom(ed) == geom0
    assert len(ed.undo_mgr.undo_stack) == 0
