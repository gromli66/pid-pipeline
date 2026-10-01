# -*- coding: utf-8 -*-
"""Метка модели прикрепления на клиенте (Р1, блок 3): старый холст.

Холст без `canvas_transform.attach_model` собран до модели точек
прикрепления. Пока его не пересоберёт сервер, «Ручная правка» живёт по
правилам 238fe6b: открытие — канон без слотов, порты в середине граней;
жесты — без порта-талии, без MID_SNAP, без модельных хуков редактора
(`_waist_model()`); производный ключ узла `_attach_legacy` в файл не
уходит. Холст с меткой — модель, как в блоках 1–2 (двойники ниже).

Литералы «как 238fe6b» сняты прогоном тех же сценариев в дереве deploy
238fe6b (там метки нет вовсе): совпали до бита (MEASUREMENTS, блок 3).
Координаты: centroid / точки = [y, x]; bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage                 # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from modules.graph.core import contact                   # noqa: E402

IMG_W, IMG_H = 1920, 1080
LEGACY_KEY = "_attach_legacy"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _canvas(mark=None):
    """Коннектор — клапан armatura_electro H — бокс, трубы на y=200 (центр
    рамки клапана: так их сажал 238fe6b); бокс уходит вниз на коннектор k;
    k2 и k3 — свободные коннекторы для add_edge."""
    tr = {"s": 1.0, "offx": 0, "offy": 0}
    if mark:
        tr["attach_model"] = mark

    def conn(nid, y, x, deg):
        return {"id": nid, "type": "connector", "class_id": -1,
                "class_name": "connector", "centroid": [y, x], "bbox": None,
                "segmentation": None, "degree": deg}

    nodes = [
        conn("c", 200.0, 200.0, 1),
        {"id": "v", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [200.0, 300.0], "bbox": [279.0, 181.1, 321.0, 218.9],
         "segmentation": None, "degree": 2},
        {"id": "b", "type": "equipment", "class_id": 99,
         "class_name": "unknow", "centroid": [200.0, 400.0],
         "bbox": [380.0, 180.0, 420.0, 220.0], "segmentation": None,
         "degree": 2},
        conn("k", 300.0, 500.0, 1),
        conn("k2", 206.0, 340.0, 0),
        conn("k3", 100.0, 402.0, 0),
    ]
    links = [
        {"id": "e1", "source": "c", "target": "v",
         "source_point": [200.0, 200.0], "target_point": [200.0, 279.0],
         "waypoints": []},
        {"id": "e2", "source": "v", "target": "b",
         "source_point": [200.0, 321.0], "target_point": [200.0, 380.0],
         "waypoints": []},
        {"id": "e3", "source": "b", "target": "k",
         "source_point": [220.0, 400.0], "target_point": [300.0, 500.0],
         "waypoints": [[300.0, 400.0]]},
    ]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W], "canvas_transform": tr},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _write(tmp_path, g, name="graph_canvas.json"):
    p = tmp_path / name
    p.write_text(json.dumps(g), encoding="utf-8")
    return p


def _editor(qapp, tmp_path, graph, canvas_mode=True):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = _write(tmp_path, graph, "graph.json")
    ed = AdvancedGraphEditor()
    ed._canvas_mode = canvas_mode      # так делает вкладка: флаг ДО load_data
    assert ed.load_data(str(ip), str(gp))
    return ed


def _save(ed, tmp_path, name="saved.json"):
    p = tmp_path / name
    assert ed.model.save(str(p))
    return p, json.loads(p.read_text(encoding="utf-8"))


def _edge(ed, eid):
    return next(e for e in ed.model.edges_data if e["id"] == eid)


def _control(xml, nid):
    return next(line.strip() for line in xml.splitlines()
                if f'fx:id="{nid}"' in line and "<ValveControl " in line)


# ───────────────────────── открытие ─────────────────────────

def test_open_old_canvas_is_238fe6b(tmp_path):
    """Старый холст открывается НЕПОДВИЖНО, как в 238fe6b: трубы в центре
    рамки клапана, файл байт в байт прежний. Двойник с меткой — модель:
    концы уходят в талию (+8.3916), файл переписан — наблюдение состоялось."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _write(tmp_path, _canvas())
    before = p.read_bytes()
    assert _reseat_canvas_endpoints(p) is False
    assert p.read_bytes() == before

    pm = _write(tmp_path, _canvas("waist-1"), "marked.json")
    assert _reseat_canvas_endpoints(pm) is True
    g = json.loads(pm.read_text(encoding="utf-8"))
    assert g["links"][0]["target_point"] == [208.3916, 279.0]
    assert g["links"][1]["source_point"] == [208.3916, 321.0]


def test_open_old_canvas_writes_no_node_key(tmp_path):
    """Открытие переписало старый холст (конец коннектора в 4 px от
    центроида) — в файле нет ни производного ключа узла, ни метки."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = _canvas()
    g["links"][0]["source_point"] = [200.0, 204.0]
    p = _write(tmp_path, g)
    assert _reseat_canvas_endpoints(p) is True
    raw = p.read_text(encoding="utf-8")
    assert LEGACY_KEY not in raw
    assert "attach_model" not in raw
    assert json.loads(raw)["links"][0]["source_point"] == [200.0, 200.0]


def _stacked(mark=None):
    """Три кратные трубы из одной точки грани B стрелки (центроид по X
    не в центре рамки: 613.0 против 613.3) в V-арматуру (репро 51b339ab)."""
    tr = {"s": 1.0, "offx": 0, "offy": 0}
    if mark:
        tr["attach_model"] = mark
    return {
        "directed": False, "multigraph": True,
        "graph": {"image_size": [IMG_H, IMG_W], "canvas_transform": tr},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "napravlenie",
             "centroid": [828.0, 613.0], "bbox": [607.0, 822.0, 619.6, 834.0],
             "degree": 3, "direction": "up"},
            {"id": "v", "type": "equipment", "class_name": "armatura_ruchn",
             "_axis": "V", "centroid": [861.5, 613.0],
             "bbox": [602.5, 840.5, 623.5, 882.5], "degree": 3},
        ],
        "links": [{"id": f"e{i}", "source": "a", "target": "v",
                   "source_point": [834.0, 613.0],
                   "target_point": [840.5, 613.0], "waypoints": []}
                  for i in (1, 2, 3)],
        "text_blocks": [], "bindings": []}


def test_open_old_canvas_keeps_stack_as_238fe6b(tmp_path):
    """Стопка кратных концов на старом холсте разводится как в 238fe6b:
    слот соседа выбирается без порядка К7 (он — часть модели: раскладка
    грани `seat_face`), пересадка откатывается гейтом «косая хуже стопки»,
    файл не переписан. Двойник с меткой: средняя труба уходит в середину
    рамки (613.3) по правилу К7."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _write(tmp_path, _stacked())
    before = p.read_bytes()
    assert _reseat_canvas_endpoints(p) is False
    assert p.read_bytes() == before

    pm = _write(tmp_path, _stacked("waist-1"), "marked.json")
    assert _reseat_canvas_endpoints(pm) is True
    got = [e["source_point"]
           for e in json.loads(pm.read_text(encoding="utf-8"))["links"]]
    assert got == [[834.0, 613.0], [834.0, pytest.approx(613.3)],
                   [834.0, 613.0]]


def test_open_old_canvas_migrates_stubs_as_238fe6b(tmp_path):
    """Две трубы в грань R бокса вне середины (88 и 112), к каждой —
    перпендикулярный стаб. Старый холст: граней «со слотами модели» нет,
    концы идут в конкурс портов и садятся в слоты движка (93.333 и
    106.667), как в 238fe6b. Если бы открытие считало грань разложенной
    моделью (`seating.slot_faces`), концы остались бы на 88 и 112."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = {"directed": False, "multigraph": True,
         "graph": {"image_size": [IMG_H, IMG_W], "canvas_transform": {}},
         "nodes": [
             {"id": "a", "type": "equipment", "class_name": "unknow",
              "centroid": [100.0, 100.0], "bbox": [80, 80, 120, 120]},
             {"id": "b0", "type": "connector", "class_name": "connector",
              "centroid": [200.0, 300.0], "bbox": None},
             {"id": "b1", "type": "connector", "class_name": "connector",
              "centroid": [20.0, 300.0], "bbox": None}],
         "links": [
             {"id": "e0", "source": "a", "target": "b0",
              "source_point": [88.0, 120.0], "target_point": [200.0, 300.0],
              "waypoints": [[88.0, 200.0], [200.0, 200.0]]},
             {"id": "e1", "source": "a", "target": "b1",
              "source_point": [112.0, 120.0], "target_point": [20.0, 300.0],
              "waypoints": [[112.0, 200.0], [20.0, 200.0]]}],
         "text_blocks": [], "bindings": []}
    p = _write(tmp_path, g)
    assert _reseat_canvas_endpoints(p) is True
    got = [e["source_point"]
           for e in json.loads(p.read_text(encoding="utf-8"))["links"]]
    assert got == [[pytest.approx(93.3333), 120.0],
                   [pytest.approx(106.6667), 120.0]]


# ─────────────── Д1: открытие после чужого жеста оператора ───────────────

def test_open_after_foreign_drag_and_undo_is_238fe6b(qapp, tmp_path):
    """Оператор тянет клапан старого холста, Ctrl+Z, сохраняет: в файле
    трубы прежние, ключа узла и метки нет; открытие сохранённого — no-op,
    выгрузка — старая поправка (181.1 − 0.222·37.8 = 172.7)."""
    from modules.canvas_to_fxml import generate_canvas_fxml
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    ed = _editor(qapp, tmp_path, _canvas())
    assert ed.model.attach_legacy is True
    ed.start_drag_node("v")
    ed.drag_node_to(330.0, 240.0)
    ed.end_drag_node()
    ed.undo()

    p, g = _save(ed, tmp_path)
    assert g["links"] == _canvas()["links"]
    assert not any(LEGACY_KEY in n for n in g["nodes"])
    assert "attach_model" not in g["graph"]["canvas_transform"]
    assert any(LEGACY_KEY in n for n in ed.model.nodes.values()), \
        "живые узлы сохранением не мутируются"

    assert _reseat_canvas_endpoints(p) is False
    assert 'layoutY="172.7"' in _control(generate_canvas_fxml(g), "v")


def test_old_canvas_drag_keeps_pipes_at_frame_middle(qapp, tmp_path):
    """Жест на старом холсте — без талии: после переноса клапана трубы в
    центре новой рамки (y1 + 18.9 = 240.0), выгрузка со старой поправкой
    (221.1 − 8.3916 = 212.7), повторное открытие — no-op. Двойник с меткой:
    трубы в талии (248.3916), контрол 1:1 с рамкой (221.1)."""
    from modules.canvas_to_fxml import generate_canvas_fxml
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    for mark, y, layout_y in ((None, 240.0, "212.7"),
                              ("waist-1", 248.3916, "221.1")):
        sub = tmp_path / str(mark)
        sub.mkdir()
        ed = _editor(qapp, sub, _canvas(mark))
        ed.start_drag_node("v")
        ed.drag_node_to(330.0, 240.0)
        ed.end_drag_node()
        assert ed.nodes["v"]["bbox"] == [309.0, 221.1, 351.0, 258.9]
        assert _edge(ed, "e1")["target_point"] == [pytest.approx(y), 309.0]
        assert _edge(ed, "e2")["source_point"] == [pytest.approx(y), 351.0]
        p, g = _save(ed, sub)
        assert f'layoutY="{layout_y}"' in _control(generate_canvas_fxml(g),
                                                   "v")
        assert _reseat_canvas_endpoints(p) is False, mark


# ─────────────── жесты старого холста не включают модель ───────────────

def test_old_canvas_new_valve_stays_out_of_model(qapp, tmp_path):
    """Новый клапан на старом холсте — как в 238fe6b: без `_axis`, без
    `_axis_free`, центроид в центре рамки, порта-талии нет; ключ узла стоит,
    но в файл не пишется. Двойник с меткой — модель блока 1 (Р3)."""
    ed = _editor(qapp, tmp_path, _canvas())
    assert ed._waist_model() is False
    nid = ed.add_equipment_node(600.0, 400.0, 3, "armatura_electro",
                                width=42, height=37.8, enter_resize=False)
    n = ed.nodes[nid]
    assert n.get(LEGACY_KEY) is True
    assert "_axis" not in n and "_axis_free" not in n
    assert n["centroid"] == [400.0, 600.0]
    assert contact.face_refs(n, tuple(n["bbox"])) is None
    _p, g = _save(ed, tmp_path)
    assert not any(LEGACY_KEY in x for x in g["nodes"])

    sub = tmp_path / "marked"
    sub.mkdir()
    ed = _editor(qapp, sub, _canvas("waist-1"))
    assert ed._waist_model() is True
    nid = ed.add_equipment_node(600.0, 400.0, 3, "armatura_electro",
                                width=42, height=37.8, enter_resize=False)
    n = ed.nodes[nid]
    assert LEGACY_KEY not in n
    assert (n["_axis"], n["_axis_free"]) == ("H", True)
    assert n["centroid"] == [pytest.approx(408.3916), 600.0]


def test_old_canvas_add_edge_does_not_lay_out_face(qapp, tmp_path):
    """add_edge в занятую грань L бокса на старом холсте: новый конец в
    свободный слот (206.667), соседняя труба e2 остаётся в середине (200.0)
    — раскладки грани целиком (К5) нет; сохранение и повторное открытие —
    no-op. Двойник с меткой: грань разложена, e2 уехала в слот 193.333."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    for mark, e2_end in ((None, [200.0, 380.0]),
                         ("waist-1", [pytest.approx(193.3333), 380.0])):
        sub = tmp_path / str(mark)
        sub.mkdir()
        ed = _editor(qapp, sub, _canvas(mark))
        assert ed.add_edge("k2", "b") is not False
        new = next(e for e in ed.model.edges_data if e["source"] == "k2")
        assert new["target_point"] == [pytest.approx(206.6667), 380.0]
        assert _edge(ed, "e2")["target_point"] == e2_end
        if mark is None:
            p, g = _save(ed, sub)
            links = copy.deepcopy(g["links"])
            assert _reseat_canvas_endpoints(p) is False
            assert json.loads(p.read_text(encoding="utf-8"))["links"] == links


def test_old_canvas_single_pipe_holds_the_middle(qapp, tmp_path):
    """MID_SNAP — модель: на старом холсте одиночная прямая труба в
    свободную грань T садится ровно в середину (400.0) с коленом, как в
    238fe6b; с меткой — уступает середину на 2 px ради прямой (402.0)."""
    for mark, want, bent in ((None, [180.0, 400.0], True),
                             ("waist-1", [180.0, 402.0], False)):
        sub = tmp_path / str(mark)
        sub.mkdir()
        ed = _editor(qapp, sub, _canvas(mark))
        ed.add_edge("k3", "b")
        new = next(e for e in ed.model.edges_data if e["source"] == "k3")
        assert new["target_point"] == want, mark
        assert bool(new["waypoints"]) is bent, mark


def _axis_across_edge(mark=None):
    """Клапан armatura_electro с `_axis` H, но труба входит СВЕРХУ: ось по
    рёбрам (238fe6b) — VERTICAL, по `contact.orientation` (модель) —
    HORIZONTAL. Так на фикстуре старого холста узел h2 (red-team блока 3)."""
    g = _canvas(mark)
    v = next(n for n in g["nodes"] if n["id"] == "v")
    v["bbox"] = [279.0, 170.0, 321.0, 230.0]           # 42×60
    v["degree"] = 1
    g["nodes"][0]["centroid"] = [100.0, 300.0]
    g["links"] = [{"id": "e1", "source": "c", "target": "v",
                   "source_point": [100.0, 300.0],
                   "target_point": [170.0, 300.0], "waypoints": []}]
    return g


def test_old_canvas_resize_orients_by_edges_as_238fe6b(qapp, tmp_path):
    """«Размеры» на старом холсте ориентируют рамку по рёбрам, как 238fe6b:
    труба сверху → длинная сторона по высоте, рамка 46×64 вокруг центроида
    (литерал снят в дереве 238fe6b). С меткой — модель блока 1: ось `_axis`
    (HORIZONTAL), длинная сторона по ширине."""
    ed = _editor(qapp, tmp_path, _axis_across_edge())
    assert ed._node_orientation("v") == "VERTICAL"
    ed.set_resize_class("armatura_electro")
    ed.apply_resize(width=46.0, height=64.0)
    assert ed.nodes["v"]["bbox"] == [277.0, 168.0, 323.0, 232.0]

    sub = tmp_path / "marked"
    sub.mkdir()
    ed = _editor(qapp, sub, _axis_across_edge("waist-1"))
    assert ed._node_orientation("v") == "HORIZONTAL"
    ed.set_resize_class("armatura_electro")
    ed.apply_resize(width=46.0, height=64.0)
    x1, y1, x2, y2 = ed.nodes["v"]["bbox"]
    assert (x2 - x1, y2 - y1) == (pytest.approx(64.0), pytest.approx(46.0))


# ───────────────────────── режим по метке ─────────────────────────

def test_mode_follows_the_mark_and_the_file_keeps_it(qapp, tmp_path):
    """Режим берётся из метки при каждой загрузке: сохранение старого
    холста метку не ставит, помеченного — не снимает; растр («Проверка
    схемы») не размечается вовсе."""
    ed = _editor(qapp, tmp_path, _canvas())
    assert ed.model.attach_legacy is True
    p, g = _save(ed, tmp_path)
    ed2 = _editor(qapp, tmp_path, g)
    assert ed2.model.attach_legacy is True

    sub = tmp_path / "marked"
    sub.mkdir()
    ed = _editor(qapp, sub, _canvas("waist-1"))
    assert ed.model.attach_legacy is False
    assert not any(LEGACY_KEY in n for n in ed.model.nodes.values())
    _p, g = _save(ed, sub)
    assert g["graph"]["canvas_transform"]["attach_model"] == "waist-1"

    sub = tmp_path / "raster"
    sub.mkdir()
    ed = _editor(qapp, sub, _canvas(), canvas_mode=False)
    assert ed.model.attach_legacy is False
    assert not any(LEGACY_KEY in n for n in ed.model.nodes.values())


# ───── холст прежней метки в редакторе — таблица своей метки (ADR-009 п. 5) ─────

TABLE_KEY = "_attach_table"
_WY = 508.3916                 # талия ELECTRIC_VLV H по 'waist-1' (y)


def _waist1_canvas():
    """c — клапан armatura_electro H — c2, трубы в талии по 'waist-1'."""
    def conn(nid, x):
        return {"id": nid, "type": "connector", "class_id": -1,
                "class_name": "connector", "centroid": [_WY, x],
                "bbox": None, "segmentation": None, "degree": 1}

    g = _canvas("waist-1")
    g["nodes"] = [conn("c", 400.0), conn("c2", 600.0),
                  {"id": "v", "type": "equipment", "class_id": 3,
                   "class_name": "armatura_electro", "_axis": "H",
                   "centroid": [_WY, 500.0],
                   "bbox": [479.0, 481.1, 521.0, 518.9],
                   "segmentation": None, "degree": 2}]
    g["links"] = [
        {"id": "e1", "source": "c", "target": "v",
         "source_point": [_WY, 400.0], "target_point": [_WY, 479.0],
         "waypoints": []},
        {"id": "e2", "source": "v", "target": "c2",
         "source_point": [_WY, 521.0], "target_point": [_WY, 600.0],
         "waypoints": []},
    ]
    return g


def test_prior_mark_canvas_gestures_keep_its_waist(qapp, tmp_path,
                                                   monkeypatch):
    """Д1: после подъёма метки ('waist-2' с другой талией ELECTRIC_VLV)
    оператор открывает холст 'waist-1': перенос клапана вдоль трубы держит
    трубы прямыми в талии 'waist-1'; новый клапан получает ключ таблицы и
    центроид в талии 'waist-1'; в файл ключ не уходит, метка остаётся."""
    new = dict(contact.WAIST_TABLES["waist-1"], ELECTRIC_VLV=(0.25, 0.25))
    monkeypatch.setattr(contact, "WAIST_TABLES",
                        dict(contact.WAIST_TABLES, **{"waist-2": new}))
    monkeypatch.setattr(contact, "ATTACH_MODEL", "waist-2")
    monkeypatch.setattr(contact, "SKIN_CONTACT_OFFSET", dict(new))

    ed = _editor(qapp, tmp_path, _waist1_canvas())
    assert ed._waist_model() is True
    assert ed.nodes["v"][TABLE_KEY] == "waist-1"
    ed.start_drag_node("v")
    ed.drag_node_to(530.0, _WY)
    ed.end_drag_node()
    assert _edge(ed, "e1")["target_point"] == pytest.approx([_WY, 509.0])
    assert _edge(ed, "e2")["source_point"] == pytest.approx([_WY, 551.0])
    assert not _edge(ed, "e1")["waypoints"] and \
        not _edge(ed, "e2")["waypoints"]

    nid = ed.add_equipment_node(800.0, 400.0, 3, "armatura_electro",
                                width=42, height=37.8, enter_resize=False)
    n = ed.nodes[nid]
    assert n[TABLE_KEY] == "waist-1"
    assert n["centroid"] == [pytest.approx(400.0 + 0.222 * 37.8), 800.0]

    _p, g = _save(ed, tmp_path)
    assert not any(TABLE_KEY in x for x in g["nodes"])
    assert g["graph"]["canvas_transform"]["attach_model"] == "waist-1"
