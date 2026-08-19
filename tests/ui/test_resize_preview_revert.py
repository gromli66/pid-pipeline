# -*- coding: utf-8 -*-
"""Пункт 1.4 дороги (Р9) — откат превью панели «Размеры».

Зачем. `preview_resize` мутирует МОДЕЛЬ, а не только сцену, и в undo не пишет.
Геометрия рамок от базлайна уже идемпотентна (сделано ранее), но два остатка
живы (аудит этапа 1, ловушка `1.4`):

  • **пины вне базлайна.** `rescale_edge_pins` умножает `dx/dy` НА МЕСТЕ, а
    `_capture_resize_base` их не снимает — каждый тик бегунка домножает уже
    домноженное. Замер MEASUREMENTS §48 на `node_11` фикстуры (рамка 20×36,
    цель 90×90 ⇒ sx=4.5): пин `dx` 10.0 → 45.0 → 202.5 за два тика ОДНИМ И ТЕМ
    ЖЕ значением, после «Применить» — 911.25 при полуширине узла 45;
  • **брошенное превью.** Выход из режима (Esc / кнопка) и любая смена набора
    сбрасывали базлайн, НЕ вернув модель: геометрия изменена, `undo_stack`
    пуст — вернуть нечем.

Что здесь проверяется — только ДАННЫЕ (принцип набора 0.4): геометрия узлов,
пины рёбер, глубина стека undo. Ни одного утверждения про `_current_mode` и
прочую внутреннюю кухню — она уезжает вместе с декомпозицией UI (этап 10).
`set_mode` в тестах — setup (так его зовёт кнопка вкладки), не предмет проверки.

Числа абсолютные и заперты с двух сторон: сравнение идёт с точным ожидаемым
значением при `abs=1e-6`, а не с «меньше чем». Допуск ≥4 px набора 0.4 здесь
не нужен — панель зовёт `preview_resize`/`apply_resize` числами, через
`mapFromScene` с его округлением до целого пикселя вьюпорта ничего не идёт;
единственный настоящий жест (Esc) координат не несёт.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication              # noqa: E402
from PySide6.QtCore import Qt, QEvent                   # noqa: E402
from PySide6.QtGui import QKeyEvent, QImage, QColor     # noqa: E402

from tools import corpus                                # noqa: E402
from ui.editors import port_model                       # noqa: E402

UID = "d74eb9f1"          # 66 узлов / 63 ребра, корпус-фикстура в git
CLASS = "armatura_ruchn"  # 17 экземпляров, все — боксы
NID = "node_11"           # bbox [25, 215, 45, 251] = 20×36, centroid [233, 35]

BASE_BBOX = [25, 215, 45, 251]
BASE_CX, BASE_CY = 35.0, 233.0            # centroid = [y, x]
SIDE = 90                                 # цель превью: квадрат 90×90
OTHER_SIDE = 60                           # второе значение — для проверки утечки базлайна
# 90×90 вокруг центроида — ориентация рамки на результат не влияет (стороны равны)
WANT_BBOX = [-10.0, 188.0, 80.0, 278.0]
PIN_DX0, PIN_DY0 = 10.0, 0.0              # пин на середине правой грани базовой рамки
SX = SIDE / (BASE_BBOX[2] - BASE_BBOX[0])          # 90/20 = 4.5
WANT_PIN_DX = PIN_DX0 * SX                         # 45.0 — пин остаётся на грани
TOL = 1e-6


# ── харнесс ──────────────────────────────────────────────────────────────

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
def ed(qapp, raster):
    """Редактор с корпусной схемой — так его собирает вкладка."""
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(raster, str(corpus.graph_path(UID)))
    editor.resize(1400, 900)
    return editor


@pytest.fixture
def armed(ed):
    """Режим «Размер объектов» на классе CLASS + пин на инцидентном ребре.

    Пин ставится ровно на середину правой грани базовой рамки — так его кладёт
    оператор, дотянув конец трубы до грани; после честного resize он обязан
    остаться на грани новой рамки.
    """
    ed.set_mode("resize_objects")
    ed.set_resize_class(CLASS)
    assert ed._resize_kind() == "box", "набор должен быть чисто боксовым"
    assert list(ed.nodes[NID]["bbox"]) == BASE_BBOX, "фикстура сдвинулась"

    node = ed.nodes[NID]
    edge, role = _incident(ed, NID)
    assert port_model.set_edge_pin(node, edge, role, BASE_BBOX[2], BASE_CY) is not None
    pin = port_model.edge_pin(edge, role)
    assert (pin["dx"], pin["dy"]) == (PIN_DX0, PIN_DY0)
    return ed


def _incident(editor, nid):
    """Первое ребро, инцидентное узлу, и роль его конца на этом узле."""
    for e in editor.edges_data:
        if e.get("source") == nid:
            return e, "source"
        if e.get("target") == nid:
            return e, "target"
    raise AssertionError(f"у {nid} нет инцидентных рёбер — фикстура сдвинулась")


def _pin(editor, nid):
    """Пин конца инцидентного ребра на узле (dict {'dx','dy'}) — по ЖИВОЙ модели.

    Ищется заново каждый раз: `model.restore` (undo) подменяет edges_data
    новыми объектами, и ссылка, взятая до отката, указывала бы в мусор.
    """
    edge, role = _incident(editor, nid)
    return port_model.edge_pin(edge, role)


def _geom(editor):
    """Снимок геометрии всех узлов — база сравнений «данные вернулись»."""
    return json.dumps({nid: [nd.get("bbox"), nd.get("centroid"),
                             nd.get("segmentation"), nd.get("area")]
                       for nid, nd in editor.nodes.items()}, sort_keys=True)


def _pins(editor):
    """Снимок ВСЕХ пинов схемы — превью трогает их мимо undo, поэтому отдельно."""
    return json.dumps([[e.get("source"), e.get("target"),
                        e.get("pin_source"), e.get("pin_target")]
                       for e in editor.edges_data], sort_keys=True)


def _esc(editor):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))


# ── дефект 1: превью идемпотентно по пинам ───────────────────────────────

def test_preview_twice_leaves_pin_on_frame(armed):
    """Два тика бегунка ОДНИМ значением дают один и тот же пин.

    Было: dx 10.0 → 45.0 → 202.5 (домножение уже домноженного).
    """
    edge, role = _incident(armed, NID)

    armed.preview_resize(width=SIDE, height=SIDE)
    after_first = dict(port_model.edge_pin(edge, role))
    armed.preview_resize(width=SIDE, height=SIDE)
    after_second = dict(port_model.edge_pin(edge, role))

    assert after_first["dx"] == pytest.approx(WANT_PIN_DX, abs=TOL)
    assert after_first["dy"] == pytest.approx(0.0, abs=TOL)
    assert after_second["dx"] == pytest.approx(WANT_PIN_DX, abs=TOL)
    assert after_second["dy"] == pytest.approx(0.0, abs=TOL)


def test_preview_pin_stays_on_right_edge_of_new_frame(armed):
    """Пин, стоявший на середине правой грани, остаётся на ней после превью."""
    armed.preview_resize(width=SIDE, height=SIDE)
    armed.preview_resize(width=SIDE, height=SIDE)

    bb = armed.nodes[NID]["bbox"]
    assert bb == pytest.approx(WANT_BBOX, abs=TOL)
    pin = _pin(armed, NID)
    assert BASE_CX + pin["dx"] == pytest.approx(bb[2], abs=TOL)
    assert BASE_CY + pin["dy"] == pytest.approx((bb[1] + bb[3]) / 2, abs=TOL)


def test_apply_after_preview_keeps_pin_on_frame(armed):
    """«Применить» после превью не даёт пину третьего домножения.

    Было: dx 202.5 → 911.25 при полуширине узла 45 — конец трубы в двадцати
    рамках от узла.
    """
    armed.preview_resize(width=SIDE, height=SIDE)
    armed.preview_resize(width=SIDE, height=SIDE)
    armed.apply_resize(width=SIDE, height=SIDE)

    bb = armed.nodes[NID]["bbox"]
    assert bb[2] - bb[0] == pytest.approx(SIDE, abs=TOL)
    assert bb[3] - bb[1] == pytest.approx(SIDE, abs=TOL)
    pin = _pin(armed, NID)
    assert pin["dx"] == pytest.approx(WANT_PIN_DX, abs=TOL)
    assert pin["dy"] == pytest.approx(0.0, abs=TOL)


# ── дефект 2: брошенное превью откатывается ──────────────────────────────

def test_abandoned_preview_reverts_on_escape(armed):
    """Esc из режима возвращает модель к состоянию до превью — бит-в-бит."""
    geom0, pins0 = _geom(armed), _pins(armed)
    depth0 = len(armed.undo_mgr.undo_stack)

    armed.preview_resize(width=SIDE, height=SIDE)
    assert _geom(armed) != geom0, "превью ничего не изменило — тест бессмыслен"
    assert armed.nodes[NID]["bbox"] == pytest.approx(WANT_BBOX, abs=TOL)

    _esc(armed)

    assert _geom(armed) == geom0
    assert _pins(armed) == pins0
    assert len(armed.undo_mgr.undo_stack) == depth0


def test_abandoned_preview_reverts_on_selection_change(armed):
    """Смена набора (кнопка «по одному») тоже возвращает модель."""
    geom0, pins0 = _geom(armed), _pins(armed)
    depth0 = len(armed.undo_mgr.undo_stack)

    armed.preview_resize(width=SIDE, height=SIDE)
    assert armed.nodes[NID]["bbox"] == pytest.approx(WANT_BBOX, abs=TOL)

    armed.resize_select_one_mode()

    assert _geom(armed) == geom0
    assert _pins(armed) == pins0
    assert len(armed.undo_mgr.undo_stack) == depth0


def test_abandoned_preview_does_not_leak_into_next_baseline(armed):
    """Брошенное превью не становится точкой возврата следующего «Применить».

    Смотреть надо именно на undo: сама рамка идемпотентна, поэтому утечка
    базлайна по геометрии превью не видна — она вылезает тем, что Ctrl+Z
    возвращает к БРОШЕННОМУ состоянию вместо исходного.
    """
    geom0, pins0 = _geom(armed), _pins(armed)

    armed.preview_resize(width=SIDE, height=SIDE)   # превью бросают...
    armed.resize_select_one_mode()                  # ...сменой набора
    armed.set_resize_class(CLASS)                   # набор собран заново
    armed.preview_resize(width=OTHER_SIDE, height=OTHER_SIDE)
    armed.apply_resize(width=OTHER_SIDE, height=OTHER_SIDE)

    bb = armed.nodes[NID]["bbox"]
    assert bb[2] - bb[0] == pytest.approx(OTHER_SIDE, abs=TOL)
    assert bb[3] - bb[1] == pytest.approx(OTHER_SIDE, abs=TOL)

    armed.undo()

    assert _geom(armed) == geom0
    assert _pins(armed) == pins0


def test_undo_during_live_preview_is_not_resurrected(armed):
    """Ctrl+Z посреди превью не отменяется последующим откатом брошенного.

    `model.restore` подменяет `nodes`/`edges_data` новыми объектами, и базлайн,
    снятый до отката, описывает уже отменённое состояние: без проверки на
    протухание откат брошенного превью вернул бы ровно то, что оператор
    только что отменил.
    """
    geom0, pins0 = _geom(armed), _pins(armed)

    armed.preview_resize(width=SIDE, height=SIDE)
    armed.apply_resize(width=SIDE, height=SIDE)      # шаг в стеке отмены
    applied = _geom(armed)

    armed.preview_resize(width=OTHER_SIDE, height=OTHER_SIDE)   # живое превью...
    armed.undo()                                                # ...и Ctrl+Z посреди него
    assert _geom(armed) == geom0
    assert _pins(armed) == pins0

    armed.set_mode("idle")          # превью брошено уже после отката

    assert _geom(armed) == geom0, "откат брошенного превью воскресил отменённое"
    assert _geom(armed) != applied
    assert _pins(armed) == pins0


def test_apply_after_undo_returns_to_the_state_operator_saw(armed):
    """Точка возврата «Применить» — состояние ПОСЛЕ Ctrl+Z, а не до него.

    Базлайн живого превью и есть `_before` будущей команды. Если Ctrl+Z прошёл
    посреди превью и базлайн не пересняли, следующее «Применить» запомнит
    отменённое состояние — и Ctrl+Z после него вернёт оператора туда, где он
    уже не был. Через саму рамку это не видно: она идемпотентна, и арифметика
    сходится к тому же размеру от любого базлайна.
    """
    armed.preview_resize(width=SIDE, height=SIDE)
    armed.apply_resize(width=SIDE, height=SIDE)
    applied = _geom(armed)                      # 90×90 — оператор это видел

    armed.preview_resize(width=OTHER_SIDE, height=OTHER_SIDE)   # живое превью...
    armed.undo()                                                # ...и Ctrl+Z в нём
    undone = _geom(armed)
    assert undone != applied

    armed.apply_resize(width=40, height=40)
    armed.undo()

    assert _geom(armed) == undone
    assert _geom(armed) != applied


def test_preview_after_undone_drag_sizes_around_current_centroid(armed):
    """Превью после отменённой протяжки строит рамку вокруг ТЕКУЩЕГО центроида.

    Базлайн хранит и центроид тоже. Протянутый и затем отменённый узел — тот
    случай, где протухший базлайн виден напрямую: рамка превью села бы вокруг
    точки, из которой узел уже вернулся.
    """
    moved_cx, moved_cy = BASE_CX + 300.0, BASE_CY + 200.0
    armed.start_drag_node(NID)
    armed.drag_node_to(moved_cx, moved_cy)
    armed.end_drag_node()
    assert armed.nodes[NID]["centroid"][1] == pytest.approx(moved_cx, abs=TOL)

    armed.preview_resize(width=SIDE, height=SIDE)   # базлайн снят на сдвинутом узле
    armed.undo()                                    # протяжка отменена
    assert armed.nodes[NID]["centroid"][1] == pytest.approx(BASE_CX, abs=TOL)

    armed.preview_resize(width=OTHER_SIDE, height=OTHER_SIDE)

    bb = armed.nodes[NID]["bbox"]
    half = OTHER_SIDE / 2
    assert bb == pytest.approx([BASE_CX - half, BASE_CY - half,
                                BASE_CX + half, BASE_CY + half], abs=TOL)


# ── характеризация: что откат ломать не должен ───────────────────────────

def test_apply_survives_panel_refresh(armed):
    """«Применить» доводит дело до конца: сброс панели в конце не откатывает.

    Ловушка правки: `apply_resize` заканчивается тем же `_update_resize_panel`,
    что чинит брошенное превью, — и на входе в него базлайн ещё лежит.
    """
    armed.preview_resize(width=SIDE, height=SIDE)
    armed.apply_resize(width=SIDE, height=SIDE)

    bb = armed.nodes[NID]["bbox"]
    assert bb[2] - bb[0] == pytest.approx(SIDE, abs=TOL)
    assert bb[3] - bb[1] == pytest.approx(SIDE, abs=TOL)
    assert len(armed.undo_mgr.undo_stack) == 1


def test_undo_after_apply_restores_geometry_and_pins(armed):
    """Ctrl+Z после «Применить» возвращает и рамки, и пины — бит-в-бит."""
    geom0, pins0 = _geom(armed), _pins(armed)

    armed.preview_resize(width=SIDE, height=SIDE)
    armed.apply_resize(width=SIDE, height=SIDE)
    assert _geom(armed) != geom0

    armed.undo()

    assert _geom(armed) == geom0
    assert _pins(armed) == pins0


def test_preview_leaves_nodes_outside_set_untouched(armed):
    """Превью двигает только набор: соседи вне набора не шелохнулись.

    Заодно запирает откат: он не имеет права трогать чужое.
    """
    outside = sorted(set(armed.nodes) - set(armed._resize_sel))
    assert len(outside) == 49, "состав фикстуры сдвинулся"
    before = {nid: json.dumps([armed.nodes[nid].get("bbox"),
                               armed.nodes[nid].get("centroid")], sort_keys=True)
              for nid in outside}

    armed.preview_resize(width=SIDE, height=SIDE)

    after = {nid: json.dumps([armed.nodes[nid].get("bbox"),
                              armed.nodes[nid].get("centroid")], sort_keys=True)
             for nid in outside}
    assert after == before
