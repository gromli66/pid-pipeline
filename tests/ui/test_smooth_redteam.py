# -*- coding: utf-8 -*-
"""Кнопка «Выровнять» в живом редакторе — находки red-team блока 6
(решение Максима 01.10: чинить все шесть).

Сценарии — живой `AdvancedGraphEditor` в режиме холста (`_canvas_mode` до
`load_data`, как ставит вкладка), перед кнопкой — чужое действие оператора
(протяжка узла и её отмена Ctrl+Z). Координаты двойственны (CODING_GUIDE §6):
centroid / точки = [y, x], bbox = [x1, y1, x2, y2].
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from modules.graph.core import contact               # noqa: E402
from modules.graph.core import edit_smooth as es     # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _canvas(nodes, links, model=True):
    meta = {"image_size": [1080, 1920]}
    if model:
        meta["canvas_transform"] = {"attach_model": contact.ATTACH_MODEL}
    return {"directed": False, "multigraph": False, "graph": meta,
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _editor(tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor
    img = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    ed = AdvancedGraphEditor()
    ed._canvas_mode = True
    assert ed.load_data(str(ip), str(gp))
    return ed


def _drag_and_undo(ed, nid, dx, dy):
    """Чужое действие оператора: протяжка узла и её отмена (Ctrl+Z)."""
    before = json.dumps(ed.model.graph_data, sort_keys=True)
    y, x = ed.nodes[nid]["centroid"]
    ed.set_mode("drag_node")
    ed.start_drag_node(nid)
    ed.drag_node_to(x + dx, y + dy)
    ed.end_drag_node()
    ed.set_mode("idle")
    ed.undo_mgr.undo()
    assert json.dumps(ed.model.graph_data, sort_keys=True) == before


def _valve(nid, bb, cls="armatura_electro", axis="H"):
    return {"id": nid, "type": "equipment", "class_name": cls, "class_id": 3,
            "centroid": [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2],
            "bbox": list(bb), "_axis": axis, "segmentation": None}


def _box(nid, x1, y1, x2, y2):
    return {"id": nid, "type": "equipment", "class_name": "unknow",
            "class_id": 99, "centroid": [(y1 + y2) / 2, (x1 + x2) / 2],
            "bbox": [x1, y1, x2, y2], "segmentation": None}


# ── (2) ход не уводит узел за лист ─────────────────────────────────────
def _edge_of_sheet(x1, right):
    """Репро red-team e7_offsheet в живом редакторе: H-клапан с рамкой от
    x1, труба из талии внутренней грани к боксу q в 3.4 px от грани —
    вылет меньше клиренса лестницы маршрута, «е» отводит клапан наружу,
    к краю листа, на 3.1 px."""
    bb = (x1, 280.0, x1 + 42.0, 318.0)
    v = _valve("v", bb)
    fx = bb[0] if right else bb[2]
    qx = fx - 3.4 if right else fx + 3.4
    w = contact.face_refs(v, bb)["L" if right else "R"]
    q = _box("q", qx - 20.0, 500.0, qx + 20.0, 540.0)
    e = {"id": "em", "source": "v", "target": "q", "source_point": [w, fx],
         "target_point": [500.0, qx], "waypoints": []}
    return _canvas([v, q], [e])


@pytest.mark.parametrize("right", [False, True], ids=["слева", "справа"])
def test_button_keeps_nodes_on_the_sheet(qapp, tmp_path, right):
    """Находка red-team (2): у края листа кнопка уводила клапан за лист (x1
    1 -> −2.1, x2 1919 -> 1922.1; гейт судил только габарит). Теперь узлы в
    листе — клапан на месте. В 5 px от края «е» жив: клапан отходит на
    3.1 px и остаётся в листе."""
    x1 = 1877.0 if right else 1.0
    ed = _editor(tmp_path, _edge_of_sheet(x1, right))
    _drag_and_undo(ed, "q", 30.0, 0.0)
    ed.smooth_canvas()
    g = ed.model.graph_data
    assert es.sheet_out(g) == 0.0
    assert ed.nodes["v"]["bbox"] == [x1, 280.0, x1 + 42.0, 318.0]

    d = tmp_path / "room"
    d.mkdir()
    room = x1 + (-5.0 if right else 5.0)
    ed = _editor(d, _edge_of_sheet(room, right))
    ed.smooth_canvas()
    bb = ed.nodes["v"]["bbox"]
    assert bb[0] == pytest.approx(room + (3.1 if right else -3.1))
    assert es.sheet_out(ed.model.graph_data) == 0.0


# ── (3) allow_node_shift=False: оборудование стоит на всех ступенях ────
def test_no_node_shift_keeps_the_valve(qapp, tmp_path):
    """Находка red-team (3): `smooth_canvas(allow_node_shift=False)` держала
    только сдвиг узла (ступень 4) — «е» отводил клапан. Холст модели, в 5 px
    от края листа: с флагом клапан бит-в-бит; без флага «е» отводит его на
    3.1 px."""
    bb = [5.0, 280.0, 47.0, 318.0]
    ed = _editor(tmp_path, _edge_of_sheet(5.0, False))
    _drag_and_undo(ed, "q", 30.0, 0.0)
    st = ed.smooth_canvas(allow_node_shift=False)
    assert ed.nodes["v"]["bbox"] == bb, st

    d = tmp_path / "free"
    d.mkdir()
    ed = _editor(d, _edge_of_sheet(5.0, False))
    ed.smooth_canvas()
    assert ed.nodes["v"]["bbox"][0] == pytest.approx(5.0 - 3.1)


# ── (5) почти-прямая из коннектора в талию: FXML в талии после открытия ─
def _conn(nid, x, y):
    return {"id": nid, "type": "connector", "class_id": -1,
            "class_name": "connector", "centroid": [y, x], "bbox": None,
            "segmentation": None, "degree": 0}


def _draw_node(ed, cls, x1, y1, x2, y2):
    """Оператор ставит узел из списка классов (протяжкой рамки)."""
    before = set(ed.nodes)
    ed.set_pending_node_class({"id": 0, "name": cls})
    ed.set_mode("add_node_from_list")
    h = ed._mode_handlers["add_node_from_list"]
    h.on_press(ed, x1, y1, None)
    h.on_move(ed, x2, y2, None)
    h.on_release(ed, x2, y2, None)
    (nid,) = set(ed.nodes) - before
    return nid


def _draw_edge(ed, a, b):
    """Оператор ведёт ребро от центроида a к центроиду b."""
    from ui.editors.mode_handlers.advanced_handlers import \
        AddEdgeWithWaypointsHandler
    h = AddEdgeWithWaypointsHandler()
    ed.clear_selection()
    ya, xa = ed.nodes[a]["centroid"]
    yb, xb = ed.nodes[b]["centroid"]
    n0 = len(ed.edges_data)
    h.on_press(ed, xa, ya, None)
    h.on_press(ed, xb, yb, None)
    assert len(ed.edges_data) == n0 + 1


def _fxml_miss(graph, nid):
    """{ребро: промах конца трубы мимо талии контрола FXML, px} у клапана
    nid (талия — от контрола выгрузки и доли таблицы скина)."""
    import copy
    import re
    from modules.canvas_to_fxml import generate_canvas_fxml
    fx = generate_canvas_fxml(copy.deepcopy(graph))
    m = re.search(r'<(\w+Control) fx:id="%s"([^>]*)/>' % re.escape(nid), fx)
    at = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
    lx, ly, pw, ph = (float(at[k]) for k in
                      ("layoutX", "layoutY", "prefWidth", "prefHeight"))
    ori = at.get("orientation", "")
    node = {n["id"]: n for n in graph["nodes"]}[nid]
    info = contact.skin_info(node)
    hf, vf = contact.SKIN_CONTACT_OFFSET.get(info[1], (0.0, 0.0))
    cx, cy = lx + pw / 2, ly + ph / 2
    if ori.startswith("HORIZONTAL") or not ori:
        i, w = 0, cy + hf * ph
    elif ori == "VERTICAL_REVERSE":
        i, w = 1, cx + vf * ph
    else:
        i, w = 1, cx - vf * ph
    out = {}
    for e in graph["links"]:
        for key, end in (("source_point", "source"),
                         ("target_point", "target")):
            if e.get(end) == nid:
                out[e["id"]] = abs(e[key][i] - w)
    return out


@pytest.mark.parametrize("cls,w,h,off,jog", [
    ("armatura_electro", 42.0, 37.8, -9.0, -9.0),
    ("predohran", 42.0, 31.5, 6.0, 0.0)], ids=["armatura_electro",
                                                 "predohran"])
def test_button_save_open_keeps_pipe_in_waist(qapp, tmp_path, cls, w, h,
                                              off, jog):
    """Находка red-team (5), сценарий e3v: оператор ставит V-клапан между
    коннекторами и ведёт к нему трубы; труба из верхнего коннектора
    приходит в талию почти прямой (0.608 / 0.74 px). Открытие спрямляло её
    по коннектору мимо талии — FXML промахивался больше допуска 0.35 px.
    Теперь «Выровнять» -> сохранить -> открыть: промах FXML <= 0.35 у
    обеих труб клапана. Без кнопки та же ловушка на месте (фикстура)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    def run(tag, button):
        d = tmp_path / tag
        d.mkdir()
        ed = _editor(d, _canvas([_conn("cL", 300.0, 150.0),
                                 _conn("cR", 300.0 + jog, 460.0)], []))
        xc = 300.0 + off
        nid = _draw_node(ed, cls, xc - h / 2, 300.0 - w / 2, xc + h / 2,
                         300.0 + w / 2)
        _draw_edge(ed, "cL", nid)
        _draw_edge(ed, nid, "cR")
        if button:
            ed.smooth_canvas()
        p = d / "saved.json"
        assert ed.save_graph(str(p))
        _reseat_canvas_endpoints(p)
        return _fxml_miss(json.loads(p.read_text(encoding="utf-8")), nid)

    miss = run("button", True)
    assert len(miss) == 2 and max(miss.values()) <= 0.35, miss
    assert max(run("bare", False).values()) > 0.35, \
        "фикстура: без кнопки открытие обязано уводить конец из талии"


# ── (7) кратная пара: лестница маршрута прокладывает своё ребро ────────
def _multi_canvas(e1_last):
    """Репро red-team e6_multi: клапан V и бокс unknow U связаны ДВУМЯ
    рёбрами (Р7): e1 со ступенькой 8 px (кандидат кнопки), e2 — прямая;
    слева коннектор cL прямой e0 в талию V. e1_last — e1 последним в списке
    рёбер (индекс пары `find_edge_data` отдаёт последнее ребро пары)."""
    def edge(eid, s, t, sp, tp, wps=()):
        return {"id": eid, "source": s, "target": t, "source_point": list(sp),
                "target_point": list(tp),
                "waypoints": [list(w) for w in wps]}
    v = _valve("V", (379.0, 289.5, 421.0, 310.5), cls="armatura_ruchn")
    u = _box("U", 600.0, 260.0, 640.0, 340.0)
    e1 = edge("e1", "V", "U", (295.0, 421.0), (287.0, 600.0),
              [(295.0, 500.0), (287.0, 500.0)])
    e2 = edge("e2", "V", "U", (305.0, 421.0), (305.0, 600.0))
    e0 = edge("e0", "cL", "V", (300.0, 250.0), (300.0, 379.0))
    links = [e2, e0, e1] if e1_last else [e1, e2, e0]
    return _canvas([v, u, _conn("cL", 250.0, 300.0)], links)


@pytest.mark.parametrize("e1_last", [False, True],
                         ids=["e1 первым", "e1 последним"])
def test_multi_pair_routes_its_own_edge(qapp, tmp_path, monkeypatch,
                                        e1_last):
    """Находка red-team (7): `route_fn` редактора адресовал ребро ПАРОЙ узлов
    (`edge_key` + `find_edge_data`), и у кратной пары V–U ступени «б», «е»
    и колено прокладывали ЧУЖОЕ ребро — при e1 первым в списке маршрут
    строился у прямой e2. Теперь лестница маршрута получает то ребро,
    которое ей отдал движок: каждый вызов лестницы из кнопки прокладывает
    ровно своё ребро (e1 — кандидат — среди них)."""
    ed = _editor(tmp_path, _multi_canvas(e1_last))
    # чужое действие оператора у коннектора (протяжка узла самой пары и
    # Ctrl+Z — test_multi_pair_gesture_undo_redo)
    _drag_and_undo(ed, "cL", 0.0, 30.0)
    pairs, routed = [], []
    real_smooth = es.smooth

    def spy_smooth(graph, route_fn=None, **kw):
        def spy_route(edge):
            n = len(routed)
            ok = route_fn(edge)
            pairs.append((edge.get("id"), routed[n:]))
            return ok
        return real_smooth(graph, route_fn=spy_route, **kw)

    real_route = ed._route_orthogonal

    def spy(live, *a, **k):
        routed.append(live.get("id"))
        return real_route(live, *a, **k)

    monkeypatch.setattr(es, "smooth", spy_smooth)
    ed._route_orthogonal = spy
    ed.smooth_canvas()
    assert "e1" in [a for a, _got in pairs], pairs
    assert all(got == [a] for a, got in pairs), pairs


# ── попутная находка (7): жест узла кратной пары и Ctrl+Z / Ctrl+Y ─────
def _links(ed):
    return {e["id"]: json.dumps(e, sort_keys=True) for e in ed.model.edges_data}


@pytest.mark.parametrize("gesture", ["протяжка", "ресайз", "ресайз с пинами"])
@pytest.mark.parametrize("nid", ["V", "U"])
@pytest.mark.parametrize("e1_last", [False, True],
                         ids=["e1 первым", "e1 последним"])
def test_multi_pair_gesture_undo_redo(qapp, tmp_path, e1_last, nid, gesture):
    """Попутная находка red-team блока 6 (MEASUREMENTS §144): снимки жеста
    для отмены (`DragNodeCommand`, `ResizeNodeCommand`) адресовали рёбра
    ПАРОЙ узлов (`edge_key` + индекс `find_edge_data` — последнее ребро
    пары), и у кратной пары V–U Ctrl+Z возвращал одно ребро из двух: у
    другого оставались конец и waypoints жеста (так же у deploy 238fe6b).
    Теперь снимки — по id ребра (`GraphDataModel.edge_ref`): Ctrl+Z
    возвращает обе трубы пары бит-в-бит (и пины входов, которые ресайз
    масштабирует), Ctrl+Y — состояние жеста."""
    from modules.graph.core import ports
    ed = _editor(tmp_path, _multi_canvas(e1_last))
    if gesture == "ресайз с пинами":
        for e in ed.model.edges_data:
            if e["id"] in ("e1", "e2"):
                role = "source" if e["source"] == nid else "target"
                pt = e[role + "_point"]
                assert ports.set_edge_pin(ed.nodes[nid], e, role, pt[1], pt[0])
    before = _links(ed)
    if gesture == "протяжка":
        y, x = ed.nodes[nid]["centroid"]
        ed.set_mode("drag_node")
        ed.start_drag_node(nid)
        ed.drag_node_to(x, y + 30.0)
        ed.end_drag_node()
        ed.set_mode("idle")
    else:
        bb = list(ed.nodes[nid]["bbox"])
        ed._start_resize(nid)
        ed._on_node_resized(nid, [bb[0], bb[1], bb[2] + 20.0, bb[3] + 30.0])
        ed._commit_resize(nid)
    after = _links(ed)
    assert after["e1"] != before["e1"] and after["e2"] != before["e2"],         "фикстура: жест ведёт обе трубы пары"
    ed.undo_mgr.undo()
    assert _links(ed) == before
    ed.undo_mgr.redo()
    assert _links(ed) == after


@pytest.mark.parametrize("with_id", [True, False], ids=["с id", "без id"])
def test_drag_amnesty_base_is_gesture_input(qapp, tmp_path, with_id):
    """Бэкап протяжки теперь адресован `edge_ref` (id ребра, без id — ключ
    пары), и амнистия жеста (`_amnesty_ids`) берёт из него базу — полилинию
    ребра на ВХОДЕ жеста, а не текущего кадра (репро 222222). Стережёт, что
    смена адреса бэкапа базу не потеряла: труба на входе шла сквозь B, кадр
    увёл её — B остаётся в амнистии."""
    e = {"source": "c", "target": "T", "source_point": [500.0, 100.0],
         "target_point": [500.0, 400.0], "waypoints": []}
    if with_id:
        e["id"] = "e"
    graph = _canvas([_conn("c", 100.0, 500.0), _box("B", 200, 480, 240, 520),
                     _box("T", 400, 480, 440, 520)], [e])
    ed = _editor(tmp_path, graph)
    ed.set_mode("drag_node")
    ed.start_drag_node("c")
    live = ed.model.edges_data[0]
    live["source_point"], live["target_point"] = [700.0, 100.0], [700.0, 400.0]
    ed._amnesty_cache = {}
    thru, _along = ed._amnesty_ids(live)
    ed.end_drag_node()
    assert "B" in thru
