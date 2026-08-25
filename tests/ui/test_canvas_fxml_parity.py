# -*- coding: utf-8 -*-
"""Паритет линии: толщина и разрывы на холсте == в FXML (блок 2, mefx-2).

Договор 1:1 записан в коде (`base_graph_editor.py:102-110`): «холст —
ФИНАЛЬНАЯ система координат: что оператор видит, то и уйдёт в FXML»,
`EDGE_WIDTH == graph_to_fxml.LINE_STROKE_WIDTH`, «иначе редактор врёт».

Три места, где он врал (замер §MEFX2):

* **2.1** холстовый путь звал генератор с `use_diameter=True`
  (`canvas_to_fxml.py:318`), и труба Dv300 уезжала в FXML толщиной 12.0
  при 2.0 на экране — вшестеро. Решение Максима №1: толщина в FXML имеет
  ровно два источника — дефолт 2.0 либо кисть оператора (`render_width`);
  диаметр ни при чём.
* **2.2** `render_width` попадал в перо только при `display_regime == "style"`
  или включённых скинах (`advanced_graph_editor.py:492-494`) — в состояниях
  «base» и «ОКР привязка» оператор видел 2.0 там, где в выгрузке 7.0.
* **2.3** разрывы мостов («— | —») считал только генератор
  (`compute_bridge_cuts`), на экране их не было вовсе. Решение Максима №2:
  показывать при включённых скинах, ⛔ в граф НЕ писать — это отрисовка.

⛔ **Фикстура поднята ЗА ПОРОГ и порог заперт.** `d74eb9f1.json` в исходных
байтах не несёт НИ ОДНОГО ребра с `diameter_value` или `render_width`
(замер §MEFX2а), поэтому «расхождений 0» на ней было бы сравнением нуля
с нулём, а зонд «вернуть EDGE_WIDTH в ветку render_width» не покраснел бы
вовсе. Толщины дописываются В ПАМЯТИ (файл фикстуры менять нельзя —
`tests/fixtures/graph/README.md`), их число заперто абсолютным числом,
и отдельным замком проверено, что старое правило давало ДРУГУЮ толщину.

⛔ **Разрывы сверяются на `089feca2.json`, а не на фикстуре гейта.**
`d74eb9f1` даёт РОВНО НОЛЬ мостов (замер §MEFX2б) — тот же порог, что
и у толщины, только его синтетикой не поднять: мост это геометрия целого
листа. `089feca2` несёт 5 разорванных рёбер и 7 разрывов на настоящих
пересечениях. Расхождение с буквой гейта — строкой в журнал.

Координаты двойственны (`CODING_GUIDE §6`): `centroid`/`source_point` =
`[y, x]`, `bbox` = `[x1, y1, x2, y2]`. Генератор живёт в `(x, y)`
(`convert_point`), холст рисует `path.moveTo(point[1], point[0])` —
ось сверяется на этой границе.
"""
import json
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QColor, QPainterPath      # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

from modules.canvas_to_fxml import generate_canvas_fxml      # noqa: E402
from modules.graph_to_fxml import (                          # noqa: E402
    LINE_STROKE_WIDTH,
    calculate_diameter_stroke,
    compute_bridge_cuts,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "fixtures", "graph")

#: Раствор кисти оператора и его же число в выгрузке (режим «Размер ребра»
#: пишет `int`, минимум 1 — `advanced_graph_editor.py:2721`).
BRUSH = 7.0
#: Диаметр, который старое правило превращало в 12.0 (clamp сверху).
DV = 300.0

#: Сколько рёбер фикстуры несут кисть и сколько — диаметр (замок порога).
RW_COUNT = 5
DV_COUNT = 5

#: Толщина по умолчанию: одна константа на экран и на файл.
BASE = 2.0

#: Мосты `089feca2` при `use_diameter=False`, `bridge_gap_factor=3.0`
#: (замер §MEFX2б: разорванных рёбер 5, разрывов 7). Список абсолютный —
#: считать его вызовом проверяемой функции значит вывести вход из выхода.
BRIDGE_IDS = ("edge_2", "edge_4", "edge_14", "edge_18", "edge_44")
BRIDGE_EDGES = 5
BRIDGE_CUTS = 7

#: Множитель разрыва, отличный от дефолта 3.0 — им проверяется, что регулятор
#: доходит до отрисовки, а не что «случайно совпало с дефолтом».
WIDE_GAP = 6.0


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _fixture_graph(name: str) -> dict:
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _thickness_graph() -> dict:
    """`d74eb9f1` плюс толщины: первые пять рёбер — кисть, следующие пять — диаметр."""
    g = _fixture_graph("d74eb9f1")
    links = g["links"]
    for e in links[:RW_COUNT]:
        e["render_width"] = int(BRUSH)
    for e in links[RW_COUNT:RW_COUNT + DV_COUNT]:
        e["diameter_value"] = DV
    return g


def _write(tmp_path, graph: dict):
    """Граф на диск + крошечный растр.

    Растр к паритету отношения не имеет (сцена в холстовых координатах, а
    подложка вписывается scale-трансформом) — берём маленький, чтобы набор
    не платил за копию картинки 1978x1247 на каждый тест.
    """
    img = QImage(200, 150, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    return str(ip), str(gp)


def _new_editor(tmp_path, graph: dict):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    ed = AdvancedGraphEditor()
    ed._canvas_mode = True                 # «Ручная правка» — единственный холст
    assert ed.load_data(*_write(tmp_path, graph))
    return ed


def _dispose(ed, qapp):
    """Снос детерминированный, а не «пусть соберёт сборщик» (PROTOCOL §5, замер 1-32)."""
    ed.set_mode("idle")
    ed.scene.clear()
    ed.setParent(None)
    ed.deleteLater()
    qapp.processEvents()


@pytest.fixture
def thick(qapp, tmp_path):
    g = _thickness_graph()
    ed = _new_editor(tmp_path, g)
    yield ed, g
    _dispose(ed, qapp)


def _bridge_graph() -> dict:
    """`089feca2` плюс диаметр на разрываемых рёбрах.

    Без него аргумент `use_diameter=False` НЕДОКАЗУЕМ: в исходных байтах
    диаметров нет, все толщины 2.0, и сторож остался бы зелёным при
    возврате диаметра в путь разрывов. С Dv300 старое правило дало бы тем
    же пяти рёбрам разрыв 36 px вместо 6 — ровно то изменение мостов,
    которым решение №1 оплачено (замер §MEFX2б).
    """
    g = _fixture_graph("089feca2")
    for e in g["links"]:
        if e["id"] in BRIDGE_IDS:
            e["diameter_value"] = DV
    return g


@pytest.fixture
def bridged(qapp, tmp_path):
    g = _bridge_graph()
    ed = _new_editor(tmp_path, g)
    yield ed, g
    _dispose(ed, qapp)


# ── чтение выгрузки ──────────────────────────────────────────────────────

def _fxml_strokes(xml: str) -> dict:
    """{edge_id: толщина} по всем `<Line>`/`<Polyline>` рёбер выгрузки.

    Разорванное ребро эмитится кусками `edge_N_b0`, `edge_N_b1` …
    (`graph_to_fxml.py:1686`) — толщина у кусков одна, склеиваем по ребру.
    """
    out = {}
    for tag in re.findall(r'<(?:Line|Polyline)\s[^>]*/>', xml):
        m = re.search(r'fx:id="([^"]+)"', tag)
        w = re.search(r'strokeWidth="([0-9.]+)"', tag)
        if not m or not w:
            continue
        eid = re.sub(r'_b\d+$', '', m.group(1))
        out.setdefault(eid, set()).add(float(w.group(1)))
    return {k: v.pop() for k, v in out.items() if len(v) == 1}


def _fxml_segments(xml: str) -> dict:
    """{edge_id: сколько кусков у ребра в выгрузке} — 1 у целого, >1 у разорванного."""
    out = {}
    for tag in re.findall(r'<(?:Line|Polyline)\s[^>]*/>', xml):
        m = re.search(r'fx:id="([^"]+)"', tag)
        if not m:
            continue
        out[re.sub(r'_b\d+$', '', m.group(1))] = \
            out.get(re.sub(r'_b\d+$', '', m.group(1)), 0) + 1
    return out


# ── чтение сцены ─────────────────────────────────────────────────────────

def _scene_pen(ed, edge: dict) -> float:
    item = ed.edge_items[ed.model.edge_key(edge["source"], edge["target"])]
    return round(item.pen().widthF(), 1)


def _scene_segments(ed, edge: dict) -> int:
    """Сколько отдельных кусков в нарисованной полилинии ребра.

    Ребро — ОДИН `QGraphicsPathItem` (`edge_items`), разрыв внутри него это
    новый подпуть, то есть лишний `moveTo`.
    """
    path = ed.edge_items[ed.model.edge_key(edge["source"], edge["target"])].path()
    return sum(1 for i in range(path.elementCount())
               if path.elementAt(i).type == QPainterPath.ElementType.MoveToElement)


# ── замки обстановки ─────────────────────────────────────────────────────

def test_холст_и_файл_держат_одну_константу_толщины(thick):
    """`EDGE_WIDTH == LINE_STROKE_WIDTH == 2.0` — иначе весь набор сравнивает разное."""
    ed, _ = thick
    assert ed.EDGE_WIDTH == BASE
    assert LINE_STROKE_WIDTH == BASE


def test_фикстура_поднята_за_порог(thick):
    """Без этого замка «расхождений 0» было бы сравнением нуля с нулём.

    Исходные байты `d74eb9f1` не несут ни `render_width`, ни `diameter_value`
    (замер §MEFX2а), а зонд «вернуть EDGE_WIDTH в ветку render_width» на
    такой фикстуре не краснеет: краснеть нечему.
    """
    ed, g = thick
    links = g["links"]
    assert sum(1 for e in links if e.get("render_width")) == RW_COUNT
    assert sum(1 for e in links if e.get("diameter_value")) == DV_COUNT
    assert len(ed.edge_items) == len(links), "ключи рёбер схлопнулись"
    # Порог заперт с той стороны: старое правило давало ДРУГУЮ толщину.
    assert calculate_diameter_stroke(DV, BASE, 1.0) == 12.0
    assert calculate_diameter_stroke(DV, BASE, 1.0) != BASE


def test_фикстура_разрывов_несёт_мосты(bridged):
    """`089feca2` — 5 разорванных рёбер, 7 разрывов (замер §MEFX2б).

    На фикстуре гейта `d74eb9f1` мостов РОВНО НОЛЬ, там сверять нечего.
    """
    ed, g = bridged
    cuts = compute_bridge_cuts(ed.edges_data, ed.nodes, base_stroke=BASE,
                               use_diameter=False, graph_scale=1.0,
                               bridge_gap_factor=3.0)
    assert set(cuts) == set(BRIDGE_IDS)
    assert len(cuts) == BRIDGE_EDGES
    assert sum(len(v) for v in cuts.values()) == BRIDGE_CUTS
    # Оба аргумента выгрузки на этой фикстуре РАЗЛИЧАЮЩИЕ — иначе сторож,
    # который их сверяет, зелен при любом их значении.
    assert cuts != compute_bridge_cuts(ed.edges_data, ed.nodes, base_stroke=BASE,
                                       use_diameter=True, graph_scale=1.0,
                                       bridge_gap_factor=3.0)
    assert cuts != compute_bridge_cuts(ed.edges_data, ed.nodes, base_stroke=BASE,
                                       use_diameter=False, graph_scale=1.0,
                                       bridge_gap_factor=WIDE_GAP)


# ── 2.1 + 2.2: толщина ───────────────────────────────────────────────────

@pytest.mark.parametrize("regime", ["base", "style", "ocr"])
def test_перо_каждого_ребра_равно_толщине_в_выгрузке(thick, regime):
    """Главный гейт блока: расхождений 0 во ВСЕХ режимах, кроме `perp`."""
    ed, g = thick
    ed.set_display_regime(regime)
    strokes = _fxml_strokes(generate_canvas_fxml(g))
    assert len(strokes) == len(g["links"]), "не все рёбра доехали до выгрузки"

    mismatch = [(e["id"], _scene_pen(ed, e), strokes[e["id"]])
                for e in g["links"] if _scene_pen(ed, e) != strokes[e["id"]]]
    assert mismatch == []


def test_кисть_оператора_видна_и_на_экране_и_в_файле(thick):
    """2.2: `render_width` — перо сцены, не только предпросмотр скинов."""
    ed, g = thick
    strokes = _fxml_strokes(generate_canvas_fxml(g))
    for e in g["links"][:RW_COUNT]:
        assert _scene_pen(ed, e) == BRUSH
        assert strokes[e["id"]] == BRUSH


def test_диаметр_на_толщину_не_влияет(thick):
    """2.1 (решение №1): Dv300 уходит в файл как 2.0, а не как 12.0."""
    ed, g = thick
    strokes = _fxml_strokes(generate_canvas_fxml(g))
    for e in g["links"][RW_COUNT:RW_COUNT + DV_COUNT]:
        assert e.get("render_width") is None
        assert _scene_pen(ed, e) == BASE
        assert strokes[e["id"]] == BASE


def test_режим_перпендикулярности_толщину_оператора_не_показывает(thick):
    """Единственное исключение 2.2: в `perp` ширина — сигнал «ребро плохое».

    Скины при этом старше режима: включённый предпросмотр FXML показывает
    кисть и здесь (так было и до правки — `advanced:493`).
    """
    ed, g = thick
    ed.set_display_regime("perp")
    for e in g["links"][:RW_COUNT]:
        assert _scene_pen(ed, e) in (BASE, BASE + 1)
    ed.set_show_skins(True)
    for e in g["links"][:RW_COUNT]:
        assert _scene_pen(ed, e) == BRUSH


# ── 2.3: разрывы ─────────────────────────────────────────────────────────

def test_без_скинов_рёбра_целые(bridged):
    """Вне предпросмотра холст рисует трубу как раньше — одним куском."""
    ed, g = bridged
    assert ed.show_skins is False
    assert [e["id"] for e in g["links"] if _scene_segments(ed, e) != 1] == []


def test_разрывы_в_скинах_совпадают_с_выгрузкой(bridged):
    """Гейт 2.3: сколько кусков у ребра на экране, столько и в файле."""
    ed, g = bridged
    ed.set_show_skins(True)
    segs = _fxml_segments(generate_canvas_fxml(g))

    mismatch = [(e["id"], _scene_segments(ed, e), segs[e["id"]])
                for e in g["links"] if _scene_segments(ed, e) != segs[e["id"]]]
    assert mismatch == []
    # И это не «везде по одному»: разрывы на сцене действительно есть.
    assert sum(1 for e in g["links"] if _scene_segments(ed, e) > 1) == BRIDGE_EDGES


def test_разрывы_считаются_аргументами_выгрузки(bridged):
    """«Те же аргументы, что уйдут в выгрузку» — не вторая копия правила."""
    ed, g = bridged
    ed.set_show_skins(True)
    assert ed._bridge_cuts == compute_bridge_cuts(
        ed.edges_data, ed.nodes, base_stroke=BASE, use_diameter=False,
        graph_scale=1.0, bridge_gap_factor=3.0)


def test_регулятор_разрыва_доходит_до_экрана(bridged):
    """2.4: `bridge_gap_factor` — единственный регулятор, уходящий в выгрузку.

    Множитель берём отличный от дефолта 3.0, иначе тест зелен и при
    выброшенном регуляторе.
    """
    ed, g = bridged
    ed.bridge_gap_factor = WIDE_GAP
    ed.set_show_skins(True)
    wide = compute_bridge_cuts(ed.edges_data, ed.nodes, base_stroke=BASE,
                               use_diameter=False, graph_scale=1.0,
                               bridge_gap_factor=WIDE_GAP)
    narrow = compute_bridge_cuts(ed.edges_data, ed.nodes, base_stroke=BASE,
                                 use_diameter=False, graph_scale=1.0,
                                 bridge_gap_factor=3.0)
    assert wide != narrow, "множитель не меняет разрывы — тест декоративен"
    assert ed._bridge_cuts == wide


def test_разрывы_переживают_пересборку_сцены(bridged):
    """Шов с блоком 1: «Светлый лист» рвёт сцену целиком — разрывы обязаны вернуться.

    `set_light_theme` уходит в `setup_scene` (`scene.clear()` + полная
    пересборка), а не в `_redraw_all`; пересчёт разрывов обязан стоять на
    ОБОИХ путях. Переключатель сверяется с ПРОТИВОПОЛОЖНЫМ значением:
    при `light == self._light_theme` он выходит сразу, и тест с дефолтным
    `True` был бы зелёным на любом коде.
    """
    ed, g = bridged
    ed.set_show_skins(True)
    before = {e["id"]: _scene_segments(ed, e) for e in g["links"]}
    assert ed._light_theme is True, "дефолт темы сдвинулся"
    ed.set_light_theme(False)
    after = {e["id"]: _scene_segments(ed, e) for e in g["links"]}
    assert after == before
    assert sum(1 for v in after.values() if v > 1) == BRIDGE_EDGES


def test_разрывы_в_граф_не_пишутся(bridged):
    """⛔ Решение №2: разрывы вычисляемые, файл от предпросмотра не меняется."""
    ed, g = bridged
    before = json.dumps(g, sort_keys=True)
    ed.set_show_skins(True)
    ed.set_show_skins(False)
    ed.set_show_skins(True)
    assert json.dumps(ed.model.graph_data, sort_keys=True) == before
    assert all("cuts" not in e and "bridge_cuts" not in e for e in ed.edges_data)


# ── доработка mefx-2: разрывы пересчитываются на завершении жеста ─────────
#
# Возврат приёмки Максима 2026-08-25: «скины/разрывы видно, но новые после
# перетаскивания коннектора — не всегда». Пересчёт жил ТОЛЬКО в
# `_before_draw_all_edges`, то есть в полной перерисовке; жесты, которые
# обновляют пути рёбер поштучно (`_update_edge_path`), оставляли на сцене
# разрывы прошлой перерисовки — новое пересечение без разрыва, снятое
# пересечение с разрывом, висящим в воздухе.
#
# ⛔ Разрывы считаются по ВСЕМУ листу, поэтому проверяется НЕ ребро жеста.
# Оператор тянет конец `edge_1`, а рвётся `edge_2`, к которому он не
# прикасался: связь «жест → затронутые рёбра» через инцидентность НЕ
# работает, и набор заперт именно на этом (замер §MEFX2ж).

#: Синтетический лист доработки: труба сверху вниз мимо горизонтальной,
#: пересечения нет; тяга её конца на угол блока даёт РОВНО один мост.
CROSS_NONE = {"edge_1": 1, "edge_2": 1}          # кусков у каждого ребра
CROSS_ONE = {"edge_1": 1, "edge_2": 2}           # один разрыв на `edge_2`
#: Куда тянуть конец `edge_1` на блоке: угол (500, 500) — труба уходит
#: правее и пересекает `edge_2`; (400, 500) — центр нижней грани, как было.
EP_CROSS = (500.0, 500.0)
EP_BACK = (400.0, 500.0)


def _crossing_graph() -> dict:
    """Блок, длинная труба от него вниз и короткая горизонтальная сбоку.

    Числа подобраны так, что мост рождается ровно от одного жеста и вдали
    от узлов (`BRIDGE_NODE_CLEARANCE_PX` = 15): пересечение ложится в
    (500, 700), ближайший узел `la` — в (430, 700).
    Координаты двойственны: `centroid`/`*_point` = `[y, x]`, `bbox` = `[x, y]`.
    """
    nodes = [
        {"id": "blk", "type": "equipment", "centroid": [400.0, 400.0],
         "bbox": [300.0, 300.0, 500.0, 500.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "far", "type": "connector", "centroid": [900.0, 400.0],
         "bbox": None, "segmentation": None, "class_id": -1,
         "class_name": "connector", "degree": 1},
        {"id": "la", "type": "connector", "centroid": [700.0, 430.0],
         "bbox": None, "segmentation": None, "class_id": -1,
         "class_name": "connector", "degree": 1},
        {"id": "lb", "type": "connector", "centroid": [700.0, 620.0],
         "bbox": None, "segmentation": None, "class_id": -1,
         "class_name": "connector", "degree": 1},
    ]
    links = [
        {"id": "edge_1", "source": "blk", "target": "far",
         "source_point": [500.0, 400.0], "target_point": [900.0, 400.0],
         "waypoints": []},
        {"id": "edge_2", "source": "la", "target": "lb",
         "source_point": [700.0, 430.0], "target_point": [700.0, 620.0],
         "waypoints": []},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [1000, 1200]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


@pytest.fixture
def crossing(qapp, tmp_path):
    ed = _new_editor(tmp_path, _crossing_graph())
    ed.set_show_skins(True)
    yield ed
    _dispose(ed, qapp)


def _pieces(ed) -> dict:
    """{edge_id: сколько кусков нарисовано} по ЖИВЫМ предметам сцены."""
    out = {}
    for e in ed.edges_data:
        key = ed.model.edge_key(e["source"], e["target"])
        if key in ed.edge_items:
            out[e["id"]] = _scene_segments(ed, e)
    return out


def _pieces_expected(ed) -> dict:
    """Сколько кусков ДОЛЖНО быть по данным — счётом от генератора выгрузки."""
    cuts = compute_bridge_cuts(ed.edges_data, ed.nodes, base_stroke=BASE,
                               use_diameter=False, graph_scale=1.0,
                               bridge_gap_factor=ed.bridge_gap_factor)
    out = {}
    for e in ed.edges_data:
        if ed.model.edge_key(e["source"], e["target"]) in ed.edge_items:
            out[e["id"]] = 1 + len(cuts.get(e["id"], ()))
    return out


def _drag_endpoint(ed, key, endpoint, x, y):
    """Полный жест переноса конца ребра: press -> move -> release."""
    ed._start_endpoint_drag((key, endpoint))
    ed._drag_endpoint_to(x, y)
    ed._end_endpoint_drag()


def test_синтетика_доработки_рождает_ровно_один_мост(crossing):
    """Замок обстановки: без него «разрыв появился» сравнивало бы ноль с нулём.

    До жеста мостов НЕТ вовсе, после — ровно один, и рвётся ЧУЖОЕ ребро.
    Числа абсолютные, а не полученные вызовом проверяемого механизма.
    """
    ed = crossing
    key = ed.model.edge_key("blk", "far")
    assert _pieces_expected(ed) == CROSS_NONE
    _drag_endpoint(ed, key, "source", *EP_CROSS)
    assert _pieces_expected(ed) == CROSS_ONE
    # Ребро жеста — `edge_1`, а разрыв достаётся `edge_2`: связь по
    # инцидентности здесь заведомо не работает.
    assert ed.model.find_edge_data(key)["id"] == "edge_1"


def test_тяга_коннектора_рвёт_чужую_трубу_без_переключения_скинов(crossing):
    """⭐ Гейт возврата: разрыв появляется НА ЗАВЕРШЕНИИ жеста, а не после
    следующей полной перерисовки и не после переключения «Скинов»."""
    ed = crossing
    assert _pieces(ed) == CROSS_NONE
    _drag_endpoint(ed, ed.model.edge_key("blk", "far"), "source", *EP_CROSS)
    assert _pieces(ed) == CROSS_ONE
    assert _pieces(ed) == _pieces_expected(ed)


def test_обратная_тяга_убирает_разрыв(crossing):
    """Симметрия: снятое пересечение не оставляет разрыв висеть в воздухе."""
    ed = crossing
    key = ed.model.edge_key("blk", "far")
    _drag_endpoint(ed, key, "source", *EP_CROSS)
    assert _pieces(ed) == CROSS_ONE
    _drag_endpoint(ed, key, "source", *EP_BACK)
    assert _pieces(ed) == CROSS_NONE
    assert _pieces(ed) == _pieces_expected(ed)


def test_undo_и_redo_тяги_дают_ту_же_картину_что_жест(crossing):
    """Требование возврата: откат и повтор рисуют ровно то же, что прямой жест."""
    ed = crossing
    before = _pieces(ed)
    _drag_endpoint(ed, ed.model.edge_key("blk", "far"), "source", *EP_CROSS)
    after = _pieces(ed)
    assert after == CROSS_ONE and before == CROSS_NONE, "жест не сдвинул картину"
    ed.undo()
    assert _pieces(ed) == before
    ed.redo()
    assert _pieces(ed) == after


def test_тяга_узла_тоже_доносит_разрыв_до_сцены(crossing):
    """Не только коннектор: снапшот узла закрывается `finalize()`, а он
    перерисовку НЕ зовёт (`undo_manager.py`) — путь был тот же битый."""
    ed = crossing
    assert _pieces(ed) == CROSS_NONE
    ed.start_drag_node("la")
    ed.drag_node_to(370.0, 700.0)     # горизонтальная труба уезжает под блок
    ed.end_drag_node()
    assert _pieces(ed) == CROSS_ONE
    assert _pieces(ed) == _pieces_expected(ed)


def test_протяжка_waypoint_и_её_redo_доносят_разрывы(crossing):
    """Гранулярная команда: у неё и `execute`, и `redo` идут мимо перерисовки."""
    ed = crossing
    key = ed.model.edge_key("la", "lb")
    ed._add_waypoint_on_segment(key, 0, 520.0, 700.0)
    assert _pieces(ed) == CROSS_NONE, "добавление waypoint мостов не рождает"

    ed._start_waypoint_drag((key, 0))
    ed._drag_waypoint_to(350.0, 800.0)    # колено ныряет за вертикальную трубу
    ed._end_waypoint_drag()
    crossed = _pieces(ed)
    assert crossed == {"edge_1": 1, "edge_2": 3}, "два моста на одном ребре"
    assert crossed == _pieces_expected(ed)

    ed.undo()
    assert _pieces(ed) == CROSS_NONE
    ed.redo()
    assert _pieces(ed) == crossed


def test_удалённый_мост_не_оставляет_разрыв_в_воздухе(crossing):
    """Обратная половина боли: ребро-мост убрали, а разрыв на чужой трубе
    остался. Затронуто ребро, которого в жесте нет вовсе."""
    ed = crossing
    _drag_endpoint(ed, ed.model.edge_key("blk", "far"), "source", *EP_CROSS)
    assert _pieces(ed) == CROSS_ONE

    ed.selected_edges = {ed.model.edge_key("blk", "far")}
    ed.batch_delete()
    assert _pieces(ed) == {"edge_2": 1}
    assert _pieces(ed) == _pieces_expected(ed)


def test_вне_скинов_пересчёт_не_запускается(crossing):
    """Цена платится только за предпросмотр: без скинов жест разрывов не считает."""
    ed = crossing
    ed.set_show_skins(False)
    _drag_endpoint(ed, ed.model.edge_key("blk", "far"), "source", *EP_CROSS)
    assert ed._bridge_cuts == {}
    assert _pieces(ed) == CROSS_NONE


def _gap_centers(ed, edge) -> list:
    """Середины разрывов НА ЭКРАНЕ — `(x, y)` между кусками полилинии.

    Разрыв это дыра между соседними подпутями одного `QGraphicsPathItem`,
    поэтому читается ровно там же, где его видит оператор, а не в данных.
    """
    path = ed.edge_items[ed.model.edge_key(edge["source"], edge["target"])].path()
    pts = [(path.elementAt(i).x, path.elementAt(i).y, path.elementAt(i).type)
           for i in range(path.elementCount())]
    return [(round((pts[i - 1][0] + pts[i][0]) / 2, 1),
             round((pts[i - 1][1] + pts[i][1]) / 2, 1))
            for i in range(1, len(pts))
            if pts[i][2] == QPainterPath.ElementType.MoveToElement]


def test_разрыв_едет_за_пересечением_а_не_за_ключом(crossing):
    """⛔ Симметрической разности КЛЮЧЕЙ мало — адрес работы шире.

    Тяга узла `la` укорачивает горизонтальную трубу, само пересечение
    остаётся на месте (500, 700), а разрыв в данных меняет только
    АРК-ДЛИНУ: 70 → 20 от нового начала. Ключ `edge_2` при этом в обоих
    наборах, разность ключей пуста — и по ней ребро не перерисовалось бы,
    а кадры протяжки уже применили старые 70 к новой полилинии, посадив
    дыру на x = 480 + 70 = 550, мимо трубы.
    """
    ed = crossing
    _drag_endpoint(ed, ed.model.edge_key("blk", "far"), "source", *EP_CROSS)
    e2 = ed.model.find_edge_data(ed.model.edge_key("la", "lb"))
    assert _gap_centers(ed, e2) == [(500.0, 700.0)]

    ed.start_drag_node("la")
    ed.drag_node_to(480.0, 700.0)
    ed.end_drag_node()
    assert _pieces(ed) == CROSS_ONE, "мост никуда не делся"
    assert _gap_centers(ed, e2) == [(500.0, 700.0)], "дыра уехала с пересечения"


def test_пачка_команд_синхронизируется_один_раз(crossing):
    """«Оптимизировать все» — одно нажатие, но команда на каждое ребро.

    Пересчёт квадратичен, поэтому на время пачки дверь глушится
    (`_bridge_cuts_batch`), а картинка сводится один раз в `finally`.
    Проверяется и то, что глушитель снят: залипший флаг заморозил бы
    разрывы до следующей полной перерисовки.
    """
    ed = crossing
    key = ed.model.edge_key("blk", "far")
    _drag_endpoint(ed, key, "source", *EP_CROSS)
    assert _pieces(ed) == CROSS_ONE
    # Оптимизация выпрямляет ИМЕННО ребро-мост: колено уходит, пересечение
    # с ним — тоже. Взять здесь `edge_2` значило бы получить тест, зелёный
    # и при выброшенном сведении (замер §MEFX2ж, зонд 3).
    ed.edge_perp_scores[key] = {"is_good": False, "score": 0.1,
                                "source_angle": 45, "target_angle": 45}

    assert ed.optimize_all_edges() == 1, "пачке нечего делать — тест декоративен"
    assert ed._bridge_cuts_batch is False
    assert _pieces(ed) == CROSS_NONE
    assert _pieces(ed) == _pieces_expected(ed)
