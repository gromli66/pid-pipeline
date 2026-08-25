# -*- coding: utf-8 -*-
"""Золотой характеризационный тест посадки «Контуров» (блок 4, пункт 4.1).

Зачем. Диспетчер выбора `connect_*` выносится из
`ContourEditor._recalculate_edges_for_node` в чистую функцию
`graph_geometry.dispatch_connect`, чтобы им же пользовалась «Проверка схемы».
Контурных тестов на момент выноса НЕ СУЩЕСТВОВАЛО (греп по `tests/` дал только
`test_geometry_guard.py`, и тот считает записи регуляркой), поэтому гейт
«прогнать контурные тесты» без этого файла был бы фиктивным.

Что заморожено. ЛЕГАСИ-ветвление скопировано сюда дословно (`_legacy_dispatch`)
как эталон и сверяется с вынесенной функцией на матрице **8 веток × оба порядка
узлов**. Главное место ошибки при выносе — ТРИ ветки с ПЕРЕВЁРНУТОЙ распаковкой
(`p2, p1, _ = ...`, `contour_editor.py:337/:343/:347`): в них первый результат
`connect_*` принадлежит ЦЕЛИ, а не источнику. (План болей называл две таких
ветки — их три; расхождение занесено в журнал.)

Матрица покрытия проверяется САМА (`test_matrix_covers_every_branch`): список
веток снят с исходного ветвления, а не выбран на глаз, — иначе это диагональ,
а не множество.

⚠ Ряды ветки «наименее диагональное» (приоритет 3/4) ПЕРЕСНЯТЫ ОСОЗНАННО
правкой 4.4.7 (ближняя стенка вместо дальней) — отдельным коммитом, эталон
здесь считается тем же кодом, поэтому пересъём виден в диффе `connect_*`,
а не в этом файле.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from ui.editors.graph_geometry import (             # noqa: E402
    connect_bbox_bbox, connect_bbox_polygon,
    connect_polygon_polygon, connect_point_bbox,
    connect_point_polygon, dispatch_connect,
)

R = 8           # CONNECTOR_MARKER_RADIUS «Контуров» (legacy)


# ── ЭТАЛОН: дословная копия легаси-ветвления ─────────────────────────────

def _legacy_bbox(node, connector_radius):
    """Копия `BaseGraphEditor._get_node_bbox` (base_graph_editor.py:1113-1124)."""
    node_type = node.get("type", "connector")
    bbox = node.get("bbox")
    if node_type == "equipment" and bbox and len(bbox) == 4:
        return bbox
    cx, cy = node["centroid"][1], node["centroid"][0]
    r = connector_radius
    return [cx - r, cy - r, cx + r, cy + r]


def _legacy_dispatch(src_node, tgt_node, connector_radius=R):
    """Дословная копия `contour_editor.py:306-355` (ветвление + распаковка).

    Возвращает ((sx, sy), (tx, ty), branch) — точки в (x, y) и ИМЯ ветки.
    """
    src_seg = src_node.get("segmentation")
    tgt_seg = tgt_node.get("segmentation")
    src_bbox = _legacy_bbox(src_node, connector_radius)
    tgt_bbox = _legacy_bbox(tgt_node, connector_radius)

    src_type = src_node.get("type", "connector")
    tgt_type = tgt_node.get("type", "connector")

    src_has_poly = (
        src_type == "equipment"
        and src_seg
        and isinstance(src_seg, list)
        and len(src_seg) >= 6
    )
    tgt_has_poly = (
        tgt_type == "equipment"
        and tgt_seg
        and isinstance(tgt_seg, list)
        and len(tgt_seg) >= 6
    )

    src_is_connector = src_type == "connector"
    tgt_is_connector = tgt_type == "connector"

    if src_is_connector and tgt_has_poly:
        pt = (src_node["centroid"][1], src_node["centroid"][0])
        p1, p2, _ = connect_point_polygon(pt, tgt_seg)
        branch = "conn_poly"
    elif tgt_is_connector and src_has_poly:
        pt = (tgt_node["centroid"][1], tgt_node["centroid"][0])
        p2, p1, _ = connect_point_polygon(pt, src_seg)
        branch = "poly_conn"                      # распаковка перевёрнута
    elif src_is_connector:
        pt = (src_node["centroid"][1], src_node["centroid"][0])
        p1, p2, _ = connect_point_bbox(pt, tgt_bbox)
        branch = "conn_bbox"
    elif tgt_is_connector:
        pt = (tgt_node["centroid"][1], tgt_node["centroid"][0])
        p2, p1, _ = connect_point_bbox(pt, src_bbox)
        branch = "bbox_conn"                      # распаковка перевёрнута
    elif src_has_poly and tgt_has_poly:
        p1, p2, _ = connect_polygon_polygon(src_seg, tgt_seg)
        branch = "poly_poly"
    elif src_has_poly:
        p2, p1, _ = connect_bbox_polygon(tgt_bbox, src_seg)
        branch = "poly_bbox"                      # распаковка перевёрнута
    elif tgt_has_poly:
        p1, p2, _ = connect_bbox_polygon(src_bbox, tgt_seg)
        branch = "bbox_poly"
    else:
        p1, p2, _ = connect_bbox_bbox(src_bbox, tgt_bbox)
        branch = "bbox_bbox"

    return (p1, p2, branch)


ALL_BRANCHES = {"conn_poly", "poly_conn", "conn_bbox", "bbox_conn",
                "poly_poly", "poly_bbox", "bbox_poly", "bbox_bbox"}
INVERTED_BRANCHES = {"poly_conn", "bbox_conn", "poly_bbox"}


# ── строители узлов ──────────────────────────────────────────────────────

def _conn(nid, cx, cy):
    return {"id": nid, "type": "connector", "centroid": [float(cy), float(cx)],
            "bbox": None, "segmentation": None, "class_id": -1,
            "class_name": "connector", "degree": 0}


def _box(nid, x1, y1, x2, y2):
    return {"id": nid, "type": "equipment",
            "centroid": [(y1 + y2) / 2.0, (x1 + x2) / 2.0],
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
            "segmentation": None, "class_id": 1, "class_name": "unknow",
            "degree": 0}


def _poly(nid, x1, y1, x2, y2):
    """Ромб по серединам граней bbox — форма ЗАВЕДОМО не совпадает с рамкой."""
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    seg = [mx, float(y1), float(x2), my, mx, float(y2), float(x1), my]
    n = _box(nid, x1, y1, x2, y2)
    n["segmentation"] = seg
    return n


# ── матрица: 4 взаимных положения × 4 вида узла × оба порядка ────────────

# Узел A всегда здесь; партнёр B — в одном из четырёх положений.
A_RECT = (100, 100, 200, 200)
PLACES = {
    # имя           bbox партнёра              (створ / без створа / наложение)
    "right_in_span": (350, 100, 450, 200),     # строго справа, в створе
    "below_in_span": (100, 350, 200, 450),     # строго снизу, в створе
    "out_of_span":   (350, 10, 450, 90),       # справа-выше: створа нет → фолбэк
    "overlapping":   (150, 150, 250, 250),     # перекрытие по обеим осям
}
KINDS = ("box", "poly", "conn")


def _make(kind, nid, rect):
    x1, y1, x2, y2 = rect
    if kind == "box":
        return _box(nid, x1, y1, x2, y2)
    if kind == "poly":
        return _poly(nid, x1, y1, x2, y2)
    return _conn(nid, (x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _matrix():
    """[(cell_id, src_node, tgt_node)] — обе роли для каждой геометрии."""
    cells = []
    for place, rect in PLACES.items():
        for ka in KINDS:
            for kb in KINDS:
                a = _make(ka, f"a_{place}_{ka}_{kb}", A_RECT)
                b = _make(kb, f"b_{place}_{ka}_{kb}", rect)
                cells.append((f"{place}|{ka}->{kb}", a, b))
                # ОБРАТНЫЙ порядок — своя пара узлов, чтобы id не пересекались
                a2 = _make(ka, f"c_{place}_{ka}_{kb}", A_RECT)
                b2 = _make(kb, f"d_{place}_{ka}_{kb}", rect)
                cells.append((f"{place}|{kb}<-{ka}", b2, a2))
    return cells


MATRIX = _matrix()


# ── 1. Перебор ведётся списком, а не выборкой ────────────────────────────

def test_matrix_covers_every_branch():
    """Матрица обязана задевать ВСЕ 8 веток исходного ветвления.

    Список веток снят с самого ветвления (`ALL_BRANCHES`), а не выбран
    на глаз: если ветку добавят, тест покраснеет на непокрытой.
    """
    hit = {_legacy_dispatch(s, t)[2] for _, s, t in MATRIX}
    assert hit == ALL_BRANCHES, f"не покрыты: {sorted(ALL_BRANCHES - hit)}"
    # три ветки с перевёрнутой распаковкой — главное место ошибки при выносе
    assert INVERTED_BRANCHES <= hit


def test_matrix_hits_both_orders_of_every_pair():
    """Каждая геометрическая пара прогоняется в ОБА порядка узлов."""
    seen = {}
    for cell, s, t in MATRIX:
        place = cell.split("|")[0]
        seen.setdefault(place, set()).add(_legacy_dispatch(s, t)[2])
    for place, branches in seen.items():
        assert INVERTED_BRANCHES <= branches, (
            f"{place}: перевёрнутые ветки не задеты — {sorted(branches)}")


# ── 2. Вынос БИТ-В-БИТ ───────────────────────────────────────────────────

@pytest.mark.parametrize("cell,src,tgt", MATRIX,
                         ids=[c[0] for c in MATRIX])
def test_dispatch_matches_legacy_branching(cell, src, tgt):
    """`dispatch_connect` == легаси-ветвление, точка в точку."""
    p1, p2, branch = _legacy_dispatch(src, tgt, R)
    got = dispatch_connect(src, tgt, connector_radius=R)
    assert got == (p1, p2), f"{cell} (ветка {branch}) разошлась"


def test_dispatch_honours_connector_radius():
    """Радиус коннектора — параметр, а не константа внутри функции.

    Он же — единственная утечка self-состояния при выносе: у холста
    `CONNECTOR_MARKER_RADIUS` = 4.0, у растровых вкладок — 8.
    """
    conn = _conn("c", 400.0, 150.0)
    box = _box("b", *A_RECT)
    r8 = dispatch_connect(conn, box, connector_radius=8)
    r4 = dispatch_connect(conn, box, connector_radius=4)
    # у пары conn->bbox радиус в геометрию не входит (конец = центроид)
    assert r8 == r4
    # а у пары conn->conn партнёр — виртуальный бокс радиуса r
    c2 = _conn("c2", 400.0, 150.0)
    c1 = _conn("c1", 150.0, 150.0)
    assert dispatch_connect(c1, c2, connector_radius=8)[1][0] == 400.0 - 8
    assert dispatch_connect(c1, c2, connector_radius=4)[1][0] == 400.0 - 4


# ── 3. Контракт ошибок ───────────────────────────────────────────────────

def test_dispatch_raises_instead_of_inventing_a_point():
    """Битые данные — исключение, а НЕ тихий [0,0].

    Контуры ловят его сами и оставляют старые точки (:357-361); у `add_edge`
    старых точек нет, поэтому «Проверка схемы» падает на канонную посадку —
    но решение принимает ВЫЗЫВАЮЩИЙ, а не диспетчер.
    """
    broken = {"id": "x", "type": "connector", "centroid": None,
              "bbox": None, "segmentation": None}
    with pytest.raises(Exception):
        dispatch_connect(broken, _box("b", *A_RECT), connector_radius=R)


# ── 4. Тот же ответ на живом ContourEditor ───────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _contour_editor_over_matrix(qapp, tmp_path):
    """Один редактор на всю матрицу: ячейки разнесены по сетке 1000 px."""
    from ui.editors.contour_editor import ContourEditor

    nodes, links, expect = [], [], {}
    for i, (cell, src, tgt) in enumerate(MATRIX):
        dx, dy = (i % 8) * 1000.0, (i // 8) * 1000.0
        s, t = _shift(src, dx, dy), _shift(tgt, dx, dy)
        nodes += [s, t]
        links.append({"source": s["id"], "target": t["id"],
                      "source_point": [0.0, 0.0], "target_point": [0.0, 0.0],
                      "waypoints": []})
        p1, p2, _b = _legacy_dispatch(s, t, R)
        expect[(s["id"], t["id"])] = ([p1[1], p1[0]], [p2[1], p2[0]])

    graph = {"directed": False, "multigraph": False,
             "graph": {"image_size": [400, 600]},
             "nodes": nodes, "links": links,
             "text_blocks": [], "bindings": []}

    img = QImage(600, 400, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = ContourEditor()
    assert ed.load_data(str(ip), str(gp))
    assert ed.CONNECTOR_MARKER_RADIUS == R, "легаси-радиус «Контуров» уехал"
    return ed, expect


def _shift(node, dx, dy):
    n = dict(node)
    n["centroid"] = [node["centroid"][0] + dy, node["centroid"][1] + dx]
    if node.get("bbox"):
        x1, y1, x2, y2 = node["bbox"]
        n["bbox"] = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
    if node.get("segmentation"):
        seg = node["segmentation"]
        n["segmentation"] = [v + (dx if k % 2 == 0 else dy)
                             for k, v in enumerate(seg)]
    return n


def test_contour_recalc_is_bit_exact_on_the_whole_matrix(qapp, tmp_path):
    """Путь «Контуров» не сдвинулся ни на пиксель ни в одной ячейке матрицы."""
    ed, expect = _contour_editor_over_matrix(qapp, tmp_path)

    for nid in list(ed.nodes):
        ed._recalculate_edges_for_node(nid)

    checked = 0
    for (s_id, t_id), (sp, tp) in expect.items():
        e = ed.model.find_edge_data(ed.model.edge_key(s_id, t_id))
        assert e is not None, f"ребро {s_id}-{t_id} потерялось"
        assert e["source_point"] == sp, f"{s_id}-{t_id}: source_point"
        assert e["target_point"] == tp, f"{s_id}-{t_id}: target_point"
        checked += 1
    assert checked == len(MATRIX)


def _first_edge(ed):
    s_id, t_id = MATRIX[0][1]["id"], MATRIX[0][2]["id"]
    return s_id, ed.model.find_edge_data(ed.model.edge_key(s_id, t_id))


def test_contour_keeps_old_points_when_geometry_fails(qapp, tmp_path,
                                                      monkeypatch):
    """Контракт ошибок «Контуров»: при исключении старые точки остаются.

    Отказ подкладывается в САМ диспетчер, а не в данные: битые данные ломают
    заодно отрисовку меток стороны, и тест начал бы судить рендер вместо
    контракта. Подмена проверяется на факт вызова — переезд `dispatch_connect`
    в другой модуль сделал бы её немой, а не красной.
    """
    ed, _ = _contour_editor_over_matrix(qapp, tmp_path)
    s_id, edge = _first_edge(ed)
    edge["source_point"] = [11.0, 22.0]
    edge["target_point"] = [33.0, 44.0]

    calls = []

    def _boom(*a, **kw):
        calls.append(1)
        raise ValueError("геометрия не сошлась")

    monkeypatch.setattr("ui.editors.contour_editor.dispatch_connect", _boom)
    ed._recalculate_edges_for_node(s_id)

    assert calls, "подмена не сработала — путь посадки идёт мимо неё"
    assert edge["source_point"] == [11.0, 22.0]
    assert edge["target_point"] == [33.0, 44.0]


def test_broken_node_no_longer_escapes_the_handler(qapp, tmp_path):
    """ЗАЯВЛЕННАЯ разница выноса — единственная, и она не пиксельная.

    До выноса виртуальный бокс считался ДО `try` (`_get_node_bbox` на
    :308-309), поэтому узел без центроида и без рамки ронял весь жест
    правки контура наружу. Теперь бокс считает `dispatch_connect` внутри
    `try` — такой узел попадает в общий контракт ошибок «оставить старые
    точки». Поведение сузилось только на битых данных; пиксели пары
    заморожены матрицей выше.
    """
    ed, _ = _contour_editor_over_matrix(qapp, tmp_path)
    s_id, edge = _first_edge(ed)
    edge["source_point"] = [11.0, 22.0]
    edge["target_point"] = [33.0, 44.0]
    ed.nodes[s_id]["centroid"] = None
    ed.nodes[s_id]["bbox"] = None

    ed._recalculate_edges_for_node(s_id)        # раньше — исключение наружу

    assert edge["source_point"] == [11.0, 22.0]
    assert edge["target_point"] == [33.0, 44.0]
