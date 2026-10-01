# -*- coding: utf-8 -*-
"""Клиент против модели точек прикрепления (решение заказчика 2026-09-22).

Холст, собранный сервером, открывается в «Ручной правке» НЕПОДВИЖНО; жест
оператора держит трубу в выверенном порту клапана; скин рисуется той же
ориентацией, что уйдёт в FXML (иначе нарисованная талия и посадка
разъезжаются — до правки на шести классах привод был нарисован справа, а в
САПФИР он слева).

Координаты двойственны (CODING_GUIDE §6): centroid / source_point /
target_point = [y, x]; bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage, QTransform     # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from modules.graph.core import contact, seating          # noqa: E402

IMG_W, IMG_H = 1920, 1080
# armatura_electro по оси H: бокс 42x37.8, талия ниже центра рамки на 8.3916
WAIST_Y = 200.0
BOX_CY = WAIST_Y - 0.222 * 37.8
VALVE_BBOX = [279.0, BOX_CY - 18.9, 321.0, BOX_CY + 18.9]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _canvas():
    """Кусок серверного холста: коннектор — клапан — бокс на линии талии."""
    nodes = [
        {"id": "c", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [WAIST_Y, 200.0],
         "bbox": None, "segmentation": None, "degree": 1},
        {"id": "v", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [WAIST_Y, 300.0], "bbox": list(VALVE_BBOX),
         "segmentation": None, "degree": 2},
        {"id": "b", "type": "equipment", "class_id": 99,
         "class_name": "unknow", "centroid": [WAIST_Y, 400.0],
         "bbox": [380.0, WAIST_Y - 20.0, 420.0, WAIST_Y + 20.0],
         "segmentation": None, "degree": 1},
    ]
    links = [
        {"id": "e1", "source": "c", "target": "v",
         "source_point": [WAIST_Y, 200.0], "target_point": [WAIST_Y, 279.0],
         "waypoints": []},
        {"id": "e2", "source": "v", "target": "b",
         "source_point": [WAIST_Y, 321.0], "target_point": [WAIST_Y, 380.0],
         "waypoints": []},
    ]
    # холст собран сервером по модели — с меткой (Р1, блок 3)
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W],
                      "canvas_transform": {"attach_model": "waist-1"}},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def _valve_port(node):
    rect = seating._anchor_rect(node)
    return contact.face_refs(node, rect)["L"]


def test_canvas_fixture_is_what_the_server_builds():
    """Guard: вход — свежий выход серверной посадки (пересадка ничего не
    двигает), иначе тесты ниже проверяли бы не то."""
    g = copy.deepcopy(_canvas())
    seating.reseat_all_endpoints(g, model=True, slots=True)
    assert g["links"] == _canvas()["links"]
    assert _valve_port(g["nodes"][1]) == pytest.approx(WAIST_Y)


def test_open_does_not_move_a_canvas_built_by_the_server(tmp_path):
    """«Открыл холст, ничего не тронул» — ни один конец не сдвинулся, файл
    не переписан (посадка при открытии — та же модель, что у сервера)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(_canvas()), encoding="utf-8")

    assert not _reseat_canvas_endpoints(p), "холст переписан при открытии"
    assert json.loads(p.read_text(encoding="utf-8"))["links"] \
        == _canvas()["links"]


def test_open_keeps_slots_of_several_pipes_on_one_face(tmp_path):
    """Три трубы в одну грань (репро 51b339ab: стрелка направления и
    арматура) стоят слотами вокруг опорной точки — открытие их НЕ сводит
    в опорную точку одну на другую."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = _canvas()
    g["links"] = [
        {"id": f"m{i}", "source": "b", "target": "v",
         "source_point": [WAIST_Y, 380.0], "target_point": [WAIST_Y, 321.0],
         "waypoints": []} for i in range(3)]
    g["nodes"][1]["degree"] = 3
    g["nodes"][2]["degree"] = 3
    seating.reseat_all_endpoints(g, model=True, slots=True)

    ys = sorted(e["target_point"][0] for e in g["links"])
    assert len(set(ys)) == 3, f"слоты не развели трубы: {ys}"
    # Вариант 2 (решение Максима 30.09): веер 3 x 9.45 вокруг порта 200.0
    # упёрся бы в нижний угол (210.51) — нижняя труба ровно в талии, две
    # другие с тем же шагом выше (К7, блок 2: 185.61/195.06/204.51 — мимо).
    assert _valve_port(g["nodes"][1]) == pytest.approx(WAIST_Y)
    assert ys == pytest.approx([WAIST_Y - 18.9, WAIST_Y - 9.45, WAIST_Y],
                               abs=1e-6), "крайняя труба веера не в талии"

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    _reseat_canvas_endpoints(p)
    got = sorted(e["target_point"][0]
                 for e in json.loads(p.read_text(encoding="utf-8"))["links"])
    assert got == pytest.approx(ys), "открытие схлопнуло слоты"


def _editor(qapp, tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = AdvancedGraphEditor()
    ed._canvas_mode = True          # так делает вкладка: флаг ДО load_data
    assert ed.load_data(str(ip), str(gp))
    return ed


def test_drag_valve_keeps_both_pipes_in_the_waist(qapp, tmp_path):
    """Оператор тянет клапан — обе трубы остаются в выверенном порту его
    новой рамки (а не в середине грани)."""
    ed = _editor(qapp, tmp_path, _canvas())
    ed.start_drag_node("v")
    ed.drag_node_to(330.0, 240.0)
    ed.end_drag_node()

    v = ed.nodes["v"]
    port = _valve_port(v)
    rect = seating._anchor_rect(v)
    by_id = {e["id"]: e for e in ed.edges_data}
    assert by_id["e1"]["target_point"][0] == pytest.approx(port, abs=0.5)
    assert by_id["e1"]["target_point"][1] == pytest.approx(rect[0], abs=0.5)
    assert by_id["e2"]["source_point"][0] == pytest.approx(port, abs=0.5)
    assert by_id["e2"]["source_point"][1] == pytest.approx(rect[2], abs=0.5)
    # талия ниже центра рамки — конец НЕ в середине грани
    assert port - (rect[1] + rect[3]) / 2 == pytest.approx(0.222 * 37.8)


def test_skin_orientation_and_reverse_match_the_export(qapp, tmp_path):
    """Ориентация скина — та же, что эмитит выгрузка; вертикальная арматура
    рисуется развёрнутой (привод слева, как в САПФИР)."""
    g = _canvas()
    g["nodes"][1]["_axis"] = "V"
    ed = _editor(qapp, tmp_path, g)

    assert ed._node_orientation("v") == "VERTICAL_REVERSE"
    base = ed._skin_pixmap_for("armatura_electro")
    assert base is not None and not base.isNull()
    rev = ed._oriented_skin_pixmap(base, "VERTICAL_REVERSE")
    assert rev.toImage() == base.transformed(QTransform().rotate(180)).toImage()
    assert ed._oriented_skin_pixmap(base, "VERTICAL").toImage() \
        == base.toImage(), "VERTICAL — база как есть"


def test_horizontal_valve_skin_is_rotated_not_reversed(qapp, tmp_path):
    ed = _editor(qapp, tmp_path, _canvas())
    assert ed._node_orientation("v") == "HORIZONTAL"
    base = ed._skin_pixmap_for("armatura_electro")
    got = ed._oriented_skin_pixmap(base, "HORIZONTAL")
    assert got.toImage() == base.transformed(QTransform().rotate(-90)).toImage()


def _old_canvas_42x38():
    """Старый холст: armatura_ruchn в рамке 42x38 без _axis (сборка до
    FIXED_SIZES по аспекту) и коннектор под ним. Конец сохранён старым
    каноном — на letterbox-следе графики y=260.5 (поля по 8.5 px). Метка
    модели есть (Р1): проверяется одна рамка сервера и редактора (Р2); холст
    без метки открывается по правилам 238fe6b (test_attach_mark_editor)."""
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W],
                      "canvas_transform": {"attach_model": "waist-1"}},
            "nodes": [
                {"id": "s", "type": "equipment", "class_id": 1,
                 "class_name": "armatura_ruchn", "centroid": [250.0, 821.0],
                 "bbox": [800.0, 231.0, 842.0, 269.0],
                 "segmentation": None, "degree": 1},
                {"id": "p", "type": "connector", "class_id": -1,
                 "class_name": "connector", "centroid": [420.0, 821.0],
                 "bbox": None, "segmentation": None, "degree": 1}],
            "links": [{"id": "e", "source": "s", "target": "p",
                       "source_point": [260.5, 821.0],
                       "target_point": [420.0, 821.0], "waypoints": []}],
            "text_blocks": [], "bindings": []}


def test_server_reseat_agrees_with_editor_on_old_skin_frame(qapp, tmp_path):
    """Д1, Р2: сторож == судья на старой рамке скина ПОСЛЕ жестов оператора.

    Открытие -> оператор тянет коннектор-партнёр в сторону -> «Оптимизировать
    ребро». Редактор сажает конец скина на нижнюю грань РАМКИ y=269; серверная
    посадка того же состояния — туда же. До Р2 сервер уводил его на letterbox
    y=260.5: расхождение 8.5 px, и следующая пересборка тащила трубу внутрь
    рамки."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(_old_canvas_42x38()), encoding="utf-8")
    _reseat_canvas_endpoints(p)
    ed = _editor(qapp, tmp_path, json.loads(p.read_text(encoding="utf-8")))

    ed.start_drag_node("p")
    ed.drag_node_to(830.0, 470.0)
    ed.end_drag_node()
    assert ed.optimize_edge("s", "p")
    assert ed.edges_data[0]["source_point"] == [269.0, 821.0]    # [y, x]

    st = {"graph": {"image_size": [IMG_H, IMG_W]},
          "nodes": copy.deepcopy(list(ed.nodes.values())),
          "links": copy.deepcopy(ed.edges_data)}
    seating.reseat_all_endpoints(st, model=True, slots=True)
    assert st["links"][0]["source_point"] == [269.0, 821.0], \
        "серверная посадка увела конец с рамки (letterbox 260.5?)"


# regulator_electro (CTRL_VLV_ELEC) по оси H на рабочей рамке 42x37.8: талия
# ниже центра на 0.314·37.8 = 11.8692 px (Р6; по старой таблице было 5.67 —
# промах 6.2 px). Литерал — не из таблицы.
REG_OFF = 11.8692
REG_BBOX = [279.0, WAIST_Y - REG_OFF - 18.9, 321.0, WAIST_Y - REG_OFF + 18.9]


def _regulator_canvas():
    """Тот же кусок холста с regulator_electro, концы — серверной посадкой."""
    g = _canvas()
    v = g["nodes"][1]
    v["class_name"], v["class_id"], v["bbox"] = \
        "regulator_electro", 4, list(REG_BBOX)
    seating.reseat_all_endpoints(g, model=True, slots=True)
    return g


def test_measured_waist_survives_operator_gestures(qapp, tmp_path):
    """Д1, Р6 (T7-1 блока 2): открытие -> оператор тянет клапан -> Ctrl+Z ->
    тянет коннектор-партнёр. Оба конца в выверенном порту (талия на
    11.8692 px ниже центра рамки, не середина и не старые 5.67), на
    осевых гранях рамки; серверная посадка того же состояния не двигает
    ни одного конца (сторож == судья, одна рамка и одна таблица)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = _regulator_canvas()
    assert [e["target_point"] for e in g["links"] if e["id"] == "e1"] \
        == [[WAIST_Y, 279.0]], "сервер посадил мимо талии — вход не тот"
    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    assert not _reseat_canvas_endpoints(p), "открытие сдвинуло холст"
    ed = _editor(qapp, tmp_path, json.loads(p.read_text(encoding="utf-8")))

    ed.start_drag_node("v")
    ed.drag_node_to(330.0, 240.0)
    ed.end_drag_node()
    ed.undo()
    assert ed.nodes["v"]["bbox"] == pytest.approx(REG_BBOX)
    ed.start_drag_node("c")
    ed.drag_node_to(200.0, 230.0)
    ed.end_drag_node()
    assert ed.nodes["c"]["centroid"] == pytest.approx([230.0, 200.0]),         "перенос партнёра не состоялся — тест пуст"

    v = ed.nodes["v"]
    rect = seating._anchor_rect(v)
    port = _valve_port(v)
    assert port - (rect[1] + rect[3]) / 2 == pytest.approx(REG_OFF, abs=0.01)
    by_id = {e["id"]: e for e in ed.edges_data}
    assert by_id["e1"]["target_point"] == pytest.approx([port, rect[0]],
                                                        abs=0.01)
    assert by_id["e2"]["source_point"] == pytest.approx([port, rect[2]],
                                                        abs=0.01)

    st = {"graph": {"image_size": [IMG_H, IMG_W]},
          "nodes": copy.deepcopy(list(ed.nodes.values())),
          "links": copy.deepcopy(ed.edges_data)}
    seating.reseat_all_endpoints(st, model=True, slots=True)
    moved = [(e["id"], k) for e, o in zip(st["links"], ed.edges_data)
             for k in ("source_point", "target_point") if e[k] != o[k]]
    assert moved == [], f"серверная посадка сдвинула концы: {moved}"


# ───────────── осевые входы клапана степени 1–2 (Р5, блок 5) ─────────────

def _ends(ed, eid):
    e = next(e for e in ed.edges_data if e["id"] == eid)
    return e["source_point"], e["target_point"]


def _drag(ed, nid, x, y):
    ed.start_drag_node(nid)
    ed.drag_node_to(x, y)
    ed.end_drag_node()


def test_drag_valve_under_connector_keeps_axial_entry(qapp, tmp_path):
    """Д1, S1: оператор тянет клапан степени 2 под коннектор — c оказывается
    сверху. Конец e1 у клапана — в талии свободной осевой грани L (колено за
    клапаном), не на верхней грани. Поле `degree` узла нарочно 3: степень
    считается по рёбрам. До блока 5 — T (210.0, 272.7)."""
    g = _canvas()
    g["nodes"][1]["degree"] = 3
    ed = _editor(qapp, tmp_path, g)
    _drag(ed, "v", 210.0, 300.0)

    v = ed.nodes["v"]
    rect = seating._anchor_rect(v)
    assert v["centroid"] == pytest.approx([300.0, 210.0]), "перенос пуст"
    assert rect[0] == pytest.approx(189.0)
    assert _ends(ed, "e1")[1] == pytest.approx([300.0, 189.0], abs=0.5), \
        "труба сверху вошла не в осевую талию"
    assert _ends(ed, "e2")[0] == pytest.approx([300.0, 231.0], abs=0.5)


def _conn_d(x, y):
    return {"id": "d", "type": "connector", "class_id": -1,
            "class_name": "connector", "centroid": [y, x], "bbox": None,
            "segmentation": None, "degree": 0}


def test_axial_rule_after_foreign_add_and_undo(qapp, tmp_path):
    """Д1, S2: прогон ПОСЛЕ чужого действия. Оператор добавил третью трубу
    (v–d, d снизу): степень 3 — правила нет, перенос клапана под c кладёт
    e1 на верхнюю грань. Ctrl+Z (перенос и ребро) — снова степень 2, тот же
    перенос — e1 в талии осевой грани L. Степень — по рёбрам `edges_data`,
    не по полю `degree` и не из снимка до отмены."""
    g = _canvas()
    g["nodes"].append(_conn_d(210.0, 420.0))
    ed = _editor(qapp, tmp_path, g)
    depth = ed.undo_mgr.stack_depth
    assert ed.add_edge("v", "d")
    _drag(ed, "v", 210.0, 300.0)
    rect = seating._anchor_rect(ed.nodes["v"])
    tp = _ends(ed, "e1")[1]
    assert tp[0] == pytest.approx(rect[1], abs=0.5), \
        f"степень 3 — вход сверху, а конец {tp}"

    ed.undo()
    ed.undo()
    assert ed.undo_mgr.stack_depth == depth, "отмена не вернула степень 2"
    assert len(ed.edges_data) == 2
    _drag(ed, "v", 210.0, 300.0)
    assert _ends(ed, "e1")[1] == pytest.approx([300.0, 189.0], abs=0.5), \
        "после отмены правило не вернулось"


def test_multi_edges_count_by_id_not_by_pair(qapp, tmp_path):
    """Д1, S2 (вариант): две трубы v–b к одному unknow и e1 — степень 3 по
    рёбрам (кратные — разные рёбра), правила нет: перенос под c кладёт e1
    на верхнюю грань. Подсчёт по паре узлов дал бы 2 и увёл e1 в талию."""
    g = _canvas()
    e3 = copy.deepcopy(g["links"][1])
    e3["id"] = "e3"
    g["links"].append(e3)
    ed = _editor(qapp, tmp_path, g)
    assert len(ed.edges_data) == 3, "кратное ребро не загрузилось"
    _drag(ed, "v", 210.0, 300.0)
    rect = seating._anchor_rect(ed.nodes["v"])
    tp = _ends(ed, "e1")[1]
    assert tp[0] == pytest.approx(rect[1], abs=0.5), \
        f"степень 3 — вход сверху, а конец {tp}"


def _open(tmp_path, g):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    changed = _reseat_canvas_endpoints(p)
    return changed, json.loads(p.read_text(encoding="utf-8"))


def _knee_canvas():
    """Серверный холст: c сверху-слева от клапана степени 2, труба e1 —
    коленом в талию левой осевой грани (стаб 29 px), e2 — прямая в R."""
    g = _canvas()
    g["nodes"][0]["centroid"] = [100.0, 250.0]
    g["links"][0].update(source_point=[100.0, 250.0],
                         waypoints=[[WAIST_Y, 250.0]],
                         target_point=[WAIST_Y, 279.0])
    return g


def test_open_marked_canvas_is_fixed_point(tmp_path):
    """Д1, S3: холст с меткой, собранный сервером с коленом у клапана
    степени 2, открывается без единого сдвига (посадка открытия — та же
    модель с осевым правилом, что финальная посадка раскладки)."""
    g = _knee_canvas()
    st = copy.deepcopy(g)
    seating.reseat_all_endpoints(st, model=True, slots=True, axial=True)
    assert st["links"] == g["links"], "фикстура — не выход сервера"

    changed, got = _open(tmp_path, g)
    assert not changed, "холст переписан при открытии"
    assert got["links"] == g["links"]


def _side_entry_canvas(mark):
    """c прямо над клапаном степени 2, труба e1 без waypoints входит в
    верхнюю грань (так сажали до блока 5). mark=None — старый холст:
    концы — его каноном 238fe6b (letterbox), чтобы открытие было no-op."""
    from modules.graph.core import pretransform

    g = _canvas()
    g["nodes"][0]["centroid"] = [100.0, 300.0]
    g["links"][0].update(source_point=[100.0, 300.0],
                         target_point=[VALVE_BBOX[1], 300.0])
    if mark is None:
        del g["graph"]["canvas_transform"]["attach_model"]
        assert contact.apply_attach_mode(g)
        pretransform.seat_edge_endpoints(g, legacy=True)
        contact.strip_attach_mode(g["nodes"])
    return g


def test_open_marked_canvas_keeps_straight_side_entry(tmp_path):
    """Холст с меткой и ПРЯМЫМ боковым входом у клапана степени 2 (c прямо
    над клапаном, сохранён до блока 5): роутера при открытии нет, осевое
    правило сделало бы трубу косой — гейт «не хуже входа» оставляет вход
    (крючок в талию построят раскладка или жест). Находка red-team блока 5:
    без гейта стенд edit_bench @model рос на 34 косых/вдоль своего."""
    g = _side_entry_canvas("waist-1")
    tp = g["links"][0]["target_point"]
    assert tp[1] == 300.0 and tp[0] < WAIST_Y - 10,         f"фикстура: вход не сверху {tp}"
    changed, got = _open(tmp_path, g)
    assert not changed
    assert got["links"] == g["links"]


def test_open_marked_canvas_moves_knee_end_to_waist(tmp_path):
    """Тот же клапан, c сверху-слева, колено сохранено (waypoint на линии
    талии), а конец — на верхней грани (холст до блока 5): в талии
    свободной грани L стаб к колену горизонтален — открытие сажает по
    правилу Р5 (судья портов миграций — с фильтром граней плана, иначе
    «остаться в порту» вернул бы конец на верхнюю грань)."""
    g = _side_entry_canvas("waist-1")
    g["nodes"][0]["centroid"] = [100.0, 250.0]
    g["links"][0].update(source_point=[100.0, 250.0],
                         waypoints=[[WAIST_Y, 250.0]])
    changed, got = _open(tmp_path, g)
    assert changed
    by_id = {e["id"]: e for e in got["links"]}
    assert by_id["e1"]["target_point"] == [WAIST_Y, 279.0]
    assert by_id["e2"]["source_point"] == [WAIST_Y, 321.0]


def test_open_unmarked_canvas_keeps_side_entry(tmp_path):
    """Д1, S4: старый холст без метки — посадка 238fe6b, без осевого
    правила: боковой вход степени 2 без waypoints не двигается (правило
    превратило бы его при открытии в косую)."""
    g = _side_entry_canvas(None)
    tp = g["links"][0]["target_point"]
    assert tp[1] == 300.0 and tp[0] < WAIST_Y - 10, \
        f"фикстура: вход не сверху {tp}"
    changed, got = _open(tmp_path, g)
    assert not changed
    assert got["links"] == g["links"]


def test_optimize_edge_counts_its_own_edge_once(qapp, tmp_path):
    """«Оптимизировать ребро» считает посадку на probe-копии ребра: копия
    несёт id и заменяет своё ребро — степень клапана 2, а не 3, и труба от
    c почти над клапаном садится в талию осевой грани L, а не на верхнюю."""
    g = _canvas()
    g["nodes"][0]["centroid"] = [100.0, 290.0]
    g["links"][0]["source_point"] = [100.0, 290.0]
    ed = _editor(qapp, tmp_path, g)
    assert ed.optimize_edge("c", "v")
    assert _ends(ed, "e1")[1] == pytest.approx([WAIST_Y, 279.0], abs=0.5)


# ───── крючок в осевую талию по пути мыши (Р5, сквозной red-team 02.10) ─────
# Повернуть серверный клапан редактор не может (Р3: ось после первого ребра
# фиксирована): поперечная труба входит в осевую талию КРЮЧКОМ. До правки
# путь мыши (`add_edge_with_waypoints`, клик-клик) роутера не звал, а
# `add_edge` после отказа лестницы оставлял косую через угол рамки
# ([[60, 300], [200, 321]]). Сценарий — обработчиками мыши, как у оператора.

def _single_valve_canvas(mark="waist-1"):
    """Клапан степени 1 (ось H, без `_axis_free` — серверный): e1 слева в
    талию; коннектор t прямо над клапаном, s — левее и выше."""
    g = _canvas()
    g["nodes"] = [n for n in g["nodes"] if n["id"] != "b"]
    g["links"] = [e for e in g["links"] if e["id"] != "e2"]
    g["nodes"][1]["degree"] = 1
    for cid, x, y in (("t", 300.0, 60.0), ("s", 150.0, 60.0)):
        g["nodes"].append({"id": cid, "type": "connector", "class_id": -1,
                           "class_name": "connector", "centroid": [y, x],
                           "bbox": None, "segmentation": None, "degree": 0})
    if mark is None:
        del g["graph"]["canvas_transform"]["attach_model"]
    return g


def _mouse_edge(ed, a_xy, b_xy, wps=()):
    """Ребро режимом «add_edge» обработчиком мыши: клик, изломы, клик."""
    ed.set_mode("add_edge")
    h = ed._mode_handlers["add_edge"]
    n0 = len(ed.edges_data)
    for x, y in (a_xy, *wps, b_xy):
        h.on_press(ed, x, y, None)
    assert len(ed.edges_data) == n0 + 1, "ребро не создано"
    return ed.edges_data[-1]


def _poly(e):
    return [e["source_point"], *(e.get("waypoints") or []), e["target_point"]]


def _assert_hook_into_waist(ed, e, face_x, n_operator=0):
    """Труба ортогональна, конец ровно в талии осевой грани, подход —
    горизонталь снаружи грани, достроено не больше 3 изломов; судья не
    видит ни прошивания, ни прижатия к рамкам (колено не липнет к своей
    грани)."""
    from modules.graph.core import edit_checks

    pts = _poly(e)
    mini = {"nodes": list(ed.nodes.values()), "links": [e]}
    assert edit_checks.through_box(mini) == [] \
        and edit_checks.along_border(mini) == [], f"судья: {pts}"
    assert ed._edge_is_ortho(e, tol=0.0), f"косая: {pts}"
    assert e["target_point"] == pytest.approx([WAIST_Y, face_x], abs=1e-9)
    assert _valve_port(ed.nodes["v"]) == pytest.approx(WAIST_Y)
    last = pts[-2]
    assert last[0] == pytest.approx(WAIST_Y, abs=1e-9) \
        and (last[1] - face_x) * (face_x - 300.0) > 0, \
        f"подход не перпендикулярен грани: {pts}"
    assert len(e["waypoints"]) - n_operator <= 3, f"изломов > 3: {pts}"


def test_mouse_click_click_hooks_into_axial_waist(qapp, tmp_path):
    """Д1: клик по t, клик по клапану — крючок в свободную талию R (L занята
    e1); Ctrl+Z / Ctrl+Y — ребро уходит и возвращается тем же крючком;
    повторное открытие сохранённого холста ничего не двигает."""
    ed = _editor(qapp, tmp_path, _single_valve_canvas())
    e = _mouse_edge(ed, (300.0, 60.0), (300.0, WAIST_Y))
    _assert_hook_into_waist(ed, e, VALVE_BBOX[2])
    poly = copy.deepcopy(_poly(e))
    ed.undo()
    assert len(ed.edges_data) == 1
    ed.redo()
    assert _poly(ed.edges_data[-1]) == poly

    p = tmp_path / "saved.json"
    assert ed.save_graph(str(p))
    changed, _got = _open(tmp_path, json.loads(p.read_text(encoding="utf-8")))
    assert not changed, "открытие сдвинуло крючок"


def test_mouse_operator_waypoint_kept_hook_builds_approach(qapp, tmp_path):
    """Д1: излом оператора (150, 120) сохраняется как есть, достраивается
    только подход от него к талии — в обход клапана сверху."""
    ed = _editor(qapp, tmp_path, _single_valve_canvas())
    e = _mouse_edge(ed, (150.0, 60.0), (300.0, WAIST_Y), wps=[(150.0, 120.0)])
    assert e["waypoints"][0] == [120.0, 150.0], "излом оператора сдвинут"
    _assert_hook_into_waist(ed, e, VALVE_BBOX[2], n_operator=1)


def test_add_edge_api_hooks_when_ladder_fails(qapp, tmp_path):
    """`add_edge` (не мышь): лестница отказывает (L через свой клапан,
    Z без осей за ним) — тот же крючок, а не косая с `_route_defect`."""
    ed = _editor(qapp, tmp_path, _single_valve_canvas())
    assert ed.add_edge("t", "v")
    e = ed.edges_data[-1]
    assert (e["source"], e["target"]) == ("t", "v")
    assert not e.get("_route_defect")
    _assert_hook_into_waist(ed, e, VALVE_BBOX[2])


def test_mouse_old_canvas_has_no_hook(qapp, tmp_path):
    """Старый холст без метки (Р1) — как 238fe6b: правила Р5 нет, труба
    прямая в верхнюю грань, крючка нет."""
    ed = _editor(qapp, tmp_path, _single_valve_canvas(mark=None))
    e = _mouse_edge(ed, (300.0, 60.0), (300.0, WAIST_Y))
    assert e["waypoints"] == []
    assert e["target_point"][1] == pytest.approx(300.0)
    assert e["target_point"][0] < WAIST_Y - 10, f"не сверху: {_poly(e)}"


def test_mouse_partner_behind_gets_three_bend_hook(qapp, tmp_path):
    """Д1: партнёр ПОЗАДИ клапана на высоте его рамки (левая талия занята
    e1, новой трубе — правая): Z прошёл бы сквозь свой клапан, поэтому
    крючок в 3 излома в обход рамки."""
    g = _single_valve_canvas()
    g["nodes"].append({"id": "s2", "type": "connector", "class_id": -1,
                       "class_name": "connector", "centroid": [195.0, 150.0],
                       "bbox": None, "segmentation": None, "degree": 0})
    ed = _editor(qapp, tmp_path, g)
    e = _mouse_edge(ed, (150.0, 195.0), (300.0, WAIST_Y))
    _assert_hook_into_waist(ed, e, VALVE_BBOX[2])
    assert len(e["waypoints"]) == 3, _poly(e)


def test_mouse_near_straight_approach_is_left_to_the_button(qapp, tmp_path):
    """Почти-прямая перед гранью (коннектор f в 0.6 px от линии талии,
    отклонение <= DIAG_TOL судьи) — не косая: крючок из неё сделал бы
    ступеньку 0.6 px; её спрямляет кнопка «Выровнять» («з», блок 6). Труба
    без изломов, конец в талии свободной грани R."""
    g = _single_valve_canvas()
    g["nodes"].append({"id": "f", "type": "connector", "class_id": -1,
                       "class_name": "connector",
                       "centroid": [WAIST_Y + 0.6, 420.0], "bbox": None,
                       "segmentation": None, "degree": 0})
    ed = _editor(qapp, tmp_path, g)
    e = _mouse_edge(ed, (420.0, WAIST_Y + 0.6), (300.0, WAIST_Y))
    assert e["waypoints"] == [], _poly(e)
    assert e["target_point"] == pytest.approx([WAIST_Y, VALVE_BBOX[2]],
                                              abs=1e-9)


# ───── крючок к партнёру-рамке (red-team правок 02.10, (1) и (2)) ─────
# L-кандидат крючка ставил излом на грань ПАРТНЁРА: короткий пробег вдоль
# неё (< ROUTE_HUG_OVERLAP_MIN) валидатор пропускал, открытие уводило конец
# бокса в угол (труба 17–22 px вдоль грани, along_own/corner 0 -> 1), а у
# клапана-партнёра крючок второго конца давал возвратный ход 7.5 px.

def _box_partner_canvas(d):
    """Клапан степени 1 (e1 слева в талию) и бокс unknow справа, центр на
    d px ниже линии талии."""
    g = _single_valve_canvas()
    g["nodes"].append({"id": "b", "type": "equipment", "class_id": 99,
                       "class_name": "unknow", "centroid": [WAIST_Y + d, 450.0],
                       "bbox": [430.0, WAIST_Y + d - 20.0,
                                470.0, WAIST_Y + d + 20.0],
                       "segmentation": None, "degree": 1})
    return g


def _valve_valve_canvas(d):
    """Второй клапан w степени 1 справа, талия на d px ниже талии v; у w
    своя труба e2 вправо в коннектор."""
    g = _single_valve_canvas()
    wy = WAIST_Y + d
    g["nodes"] += [
        {"id": "w", "type": "equipment", "class_id": 3,
         "class_name": "armatura_electro", "_axis": "H",
         "centroid": [wy, 500.0],
         "bbox": [479.0, BOX_CY + d - 18.9, 521.0, BOX_CY + d + 18.9],
         "segmentation": None, "degree": 1},
        {"id": "cR", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [wy, 650.0], "bbox": None,
         "segmentation": None, "degree": 1}]
    g["links"].append({"id": "e2", "source": "w", "target": "cR",
                       "source_point": [wy, 521.0], "target_point": [wy, 650.0],
                       "waypoints": []})
    return g


def _on_face(pts, bbox):
    """Сегментов, лежащих на грани рамки (в 0.5 px), ненулевой длины."""
    x1, y1, x2, y2 = bbox
    n = 0
    for (ay, ax), (by, bx) in zip(pts, pts[1:]):
        if abs(ax - bx) <= 0.5 and min(abs(ax - x1), abs(ax - x2)) <= 0.5 \
                and abs(ay - by) > 1e-6:
            n += 1
        elif abs(ay - by) <= 0.5 and min(abs(ay - y1), abs(ay - y2)) <= 0.5 \
                and abs(ax - bx) > 1e-6:
            n += 1
    return n


def _saved_and_reopened(ed, tmp_path):
    from modules.graph.core import edit_smooth as es

    p = tmp_path / "saved.json"
    assert ed.save_graph(str(p))
    changed, got = _open(tmp_path, json.loads(p.read_text(encoding="utf-8")))
    contact.apply_attach_mode(got)
    return changed, es.score(got)


@pytest.mark.parametrize("d, straight", [(2.5, True), (3.5, True),
                                         (-2.5, True), (5.0, False),
                                         (8.0, False), (-8.0, False)])
def test_mouse_hook_to_box_partner(qapp, tmp_path, d, straight):
    """Д1 (1): клик по боксу, клик по клапану. Почти-соосный бокс (открытие
    оставит прямую) сам встаёт концом на линию талии — прямая, как у deploy
    238fe6b; дальше — Z с подходом поперёк грани бокса. Ни одного сегмента
    по грани бокса; сохранить -> открыть: ничего не двигается, вдоль своей
    и угол — 0."""
    ed = _editor(qapp, tmp_path, _box_partner_canvas(d))
    e = _mouse_edge(ed, (450.0, WAIST_Y + d), (300.0, WAIST_Y))
    pts = _poly(e)
    assert ed._edge_is_ortho(e, tol=0.0), f"косая: {pts}"
    assert _on_face(pts, ed.nodes["b"]["bbox"]) == 0, f"по грани бокса: {pts}"
    assert e["target_point"] == pytest.approx([WAIST_Y, VALVE_BBOX[2]],
                                              abs=1e-9)
    if straight:
        assert e["waypoints"] == [] and \
            e["source_point"] == pytest.approx([WAIST_Y, 430.0]), pts
    else:
        assert len(e["waypoints"]) == 2, pts
    changed, s = _saved_and_reopened(ed, tmp_path)
    assert not changed, f"открытие сдвинуло трубу: {pts}"
    assert (s["along_own"], s["corner"], s["diag"]) == (0, 0, 0), s


@pytest.mark.parametrize("d", [2.5, 8.0])
def test_mouse_hook_valve_to_valve_has_no_backtrack(qapp, tmp_path, d):
    """Д1 (2): клик по клапану w, клик по клапану v — талии разнесены на d.
    Крючок конца w не ставит излом на грань v, поэтому крючку конца v нечего
    чинить: Z без возвратного хода, не больше 3 изломов, оба конца в
    талиях, открытие ничего не двигает."""
    ed = _editor(qapp, tmp_path, _valve_valve_canvas(d))
    e = _mouse_edge(ed, (500.0, WAIST_Y + d), (300.0, WAIST_Y))
    pts = _poly(e)
    assert ed._edge_is_ortho(e, tol=0.0), f"косая: {pts}"
    back = [b for a, b, c in zip(pts, pts[1:], pts[2:])
            if (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
            < -1e-9]
    assert not back, f"возвратный ход: {pts}"
    assert len(e["waypoints"]) <= 3, pts
    assert e["source_point"] == pytest.approx([WAIST_Y + d, 479.0], abs=1e-9)
    assert e["target_point"] == pytest.approx([WAIST_Y, VALVE_BBOX[2]],
                                              abs=1e-9)
    changed, s = _saved_and_reopened(ed, tmp_path)
    assert not changed, f"открытие сдвинуло трубу: {pts}"
    assert (s["along_own"], s["corner"], s["diag"]) == (0, 0, 0), s
