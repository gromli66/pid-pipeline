# -*- coding: utf-8 -*-
"""T-B (Э0) — регрессионная рамка «один контракт посадки на всех путях».

Одна фикстура прогоняется через ВСЕ реализации посадки концов рёбер
(§1 плана docs/planning/EDITOR_AFTER_LAYOUT_PLAN.md), концы сравниваются с
каноном `modules/graph/core/seating.py`. Известные расхождения — в явном
ALLOWLIST, и для них тест УТВЕРЖДАЕТ расхождение: когда Э1 приведёт
реализацию к канону, тест УПАДЁТ и заставит сжечь запись. Цель экспериментов
Э1 — пустой ALLOWLIST.

Реализации в матрице:
  1. seating.reseat_edge                  — канон (строка-эталон, всегда 0);
  2. AdvancedGraphEditor._recalculate_edge — полный пересчёт ребра
     (L-route/Цикл стороны/optimize; drag после Э3a идёт через
     _reseat_moved_end — тот делегирует в те же функции канона, его
     семантику «только ближний конец» держат тесты T-C);
  3. AdvancedGraphEditor.optimize_edge     — «оптимизация» ребра;
  4. autofix_chains.auto_fix_graph         — «автовыравнивание» (двигает узлы;
     канон для него считается на ЕГО ЖЕ выходном состоянии узлов);
  5. BaseGraphEditor.get_connection_point  — загрузка/прочее;
  6. graph_to_fxml.get_line_endpoints      — экспорт FXML (try-import: модуль
     чужой, при неимпортируемости — skip с пометкой);
  7. AdvancedGraphEditor.add_edge          — СОЗДАНИЕ нового ребра (хвост Э1:
     рёбер в графе ещё нет, инструмент сажает концы с нуля);
  8. SimpleGraphEditor.add_edge           — то же во вкладке «Проверка схемы»,
     где с 2026-08-25 посадка идёт диспетчером «Контуров»
     (`graph_geometry.dispatch_connect`), а не каноном: строгая ось важнее
     центра. Ряд добавлен ВМЕСТЕ со сменой контракта — без него сторож T-B
     слепнет ровно там, где контракт и поменялся.

Почему фикстура рукописная, а не подмножество tests/fixtures/layout/synth_med.json
(предпочтение §4 T-B): в synth_med НИ ОДНОГО узла с segmentation (0 из 208) и
ни одного equipment вне FIXED_SIZES — обязательные случаи «конец у полигона» и
«конец у обычного bbox-узла» из него не собрать ни подмножеством, ни фильтром.
Компактная фикстура в координатах холста даёт по одной изолированной паре
узлов на случай (свой компонент связности — авто-fix не тянет чужие цепочки)
и точную ручную проверку геометрии каждого конца.

Соглашения координат (CODING_GUIDE §6): centroid/source_point/target_point =
[y, x]; bbox/segmentation = [x, y]. Все сравнения в тесте — в (x, y) после
явного свопа.

Фикстура ЗАПЕЧЕНА каноном при сборке (seating.reseat_all_endpoints): это
модель реальности — свежий выход раскладки канонический by construction,
а инструменты редактора касаются его первыми (механика жалобы (4), §1 плана).

Известный баг «H раньше V» (seating.py, чинится Э2) фикстуры не задевает:
вертикальные связи построены так, что cy коннектора НЕ попадает в Y-диапазон
рамки соседа (420 vs 239.5..260.5), H-ветка честно отваливается. Красный тест
на сам баг — T-A.4 (tests/test_seating.py), не здесь.
"""
import json
import math
import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from modules.graph.core import seating              # noqa: E402
from modules.graph.core.pretransform import (       # noqa: E402
    FIXED_SIZES,
    _skin_content_rect,
)

# Допуск канона (== tol метрики seat_violations, §3.2 плана)
TOL = 0.5

# ── Матрица: случай -> (ребро, измеряемый конец) ─────────────────────────
# Каждый случай — конец ребра у узла соответствующего сорта.
CASES = {
    "connector": ("e_conn", "source"),      # конец у коннектора (точка)
    "skin": ("e_skin", "source"),           # FIXED_SIZES-скин без segmentation
    "skin_poly": ("e_skinpoly", "source"),  # FIXED_SIZES-скин С segmentation
    "polygon": ("e_poly", "source"),        # полигон без скина
    "bbox": ("e_bbox", "source"),           # обычный bbox-узел
}

IMPLS = (
    "seating",
    "recalculate_edge",
    "optimize_edge",
    "auto_fix_graph",
    "get_connection_point",
    "fxml_endpoints",
    "add_edge",
    "simple_add_edge",
)

# ── ALLOWLIST известных расхождений ──────────────────────────────────────
# ключ: (реализация, случай) -> причина. px — замер на этой фикстуре.
# Для записей отсюда тест УТВЕРЖДАЕТ dist > TOL: починка реализации
# уронит тест и заставит удалить запись.
#
# Э1 (2026-07-31): все 12 записей СОЖЖЕНЫ — редакторские инструменты
# (_recalculate_edge, optimize_edge, auto_fix_graph, get_connection_point)
# переведены на канон seating.reseat_edge/node_anchor, замер по каждой
# записи стал 0.00px. Добавлять сюда только НОВЫЕ известные расхождения.
ALLOWLIST = {
    # fxml_endpoints: расхождений НЕТ — на каноничном входе get_line_endpoints
    # воспроизводит канон (equipment: сохранённые sp/tp; connector: центроид ==
    # канон). Его особенность «коннектору принудительно центроид, сохранённый
    # sp игнорируется» видима только на НЕканоничном входе — это фиксирует T-F.
    #
    # А->В (Э2b/Э2c, решение заказчика 2026-07-31, рисунок трёх вариантов):
    # конец РАМОЧНОГО узла в редакторе всегда жёстко в ПОРТУ (середина/слот)
    # — канонный slack (скольжение по грани к оси партнёра) отменён для
    # редакторских инструментов. Канон seating НЕ меняется (сервер,
    # бит-эталон): расхождение здесь — задокументированная разница
    # контрактов «канон оси» vs «порт движка», а не баг. Замер фикстуры:
    # порт (320, 250) vs канон (320, 270) = 20.00px.
    ("recalculate_edge", "bbox"):
        "А->В: порт (середина грани) вместо канонного слака, 20.00px",
    ("optimize_edge", "bbox"):
        "А->В: порт (середина грани) вместо канонного слака, 20.00px",
    #
    # «Символ тянется на рамку» (2026-08-01, репро graph_edited_3edge):
    # редактор сажает скины на РАМКУ bbox (скин рисуется растянутым, как
    # контрол FXML), серверный канон — letterbox _skin_content_rect.
    # Замер фикстуры: 8.50px (половина letterbox-поля).
    ("recalculate_edge", "skin"): "скин на рамке bbox vs letterbox, 8.50px",
    ("optimize_edge", "skin"): "скин на рамке bbox vs letterbox, 8.50px",
    ("add_edge", "skin"): "скин на рамке bbox vs letterbox, 8.50px",
    ("recalculate_edge", "skin_poly"):
        "скин на рамке bbox vs letterbox, 8.50px",
    ("optimize_edge", "skin_poly"):
        "скин на рамке bbox vs letterbox, 8.50px",
    ("add_edge", "skin_poly"): "скин на рамке bbox vs letterbox, 8.50px",
    #
    # 2026-08-04 (репро «конец в середине арматуры» во вкладке «Проверка
    # схемы»/SimpleGraphTab): предварительный канон get_connection_point
    # тоже выталкивает скиновый конец на рамку (_lift_to_seat_rect) — в
    # простом редакторе он единственная посадка, движковой доводки там нет.
    ("get_connection_point", "skin"):
        "скин на рамке bbox vs letterbox, 8.50px",
    ("get_connection_point", "skin_poly"):
        "скин на рамке bbox vs letterbox, 8.50px",
    #
    # 2026-08-25 (блок 4 «точечных болей», решение Максима «да, как в
    # Контурах»): «Проверка схемы» сажает новое ребро диспетчером
    # `graph_geometry.dispatch_connect` — строгая ось важнее центра. Канон
    # seating не меняется (сервер, бит-эталон). Два расхождения, оба —
    # заявленный контракт вкладки, а не баг:
    ("simple_add_edge", "skin"): "скин на рамке bbox vs letterbox, 8.50px",
    # skin_poly расходится ИНАЧЕ, чем у Advanced (там тоже 8.50): ветвление
    # диспетчера смотрит СЫРОЙ segmentation, поэтому у скина С контуром конец
    # садится на КОНТУР, а не на рамку. Канон в этом случае предпочитает скин.
    ("simple_add_edge", "skin_poly"):
        "конец на контуре (сырой seg-чек диспетчера) vs letterbox скина, 4.50px",
}


# ── Фикстура: 5 изолированных пар «узел случая + коннектор-партнёр» ──────

def _node(nid, ntype, cy, cx, bbox=None, seg=None, cls=None):
    return {
        "id": nid, "type": ntype,
        "centroid": [float(cy), float(cx)],
        "bbox": [float(v) for v in bbox] if bbox else None,
        "segmentation": [float(v) for v in seg] if seg else None,
        "class_id": -1 if ntype == "connector" else 1,
        "class_name": cls or ("connector" if ntype == "connector" else "unknow"),
        "degree": 1,
    }


def _edge(eid, src, tgt):
    # sp/tp — заглушки (центроиды не важны): запекаются каноном при сборке
    return {"id": eid, "source": src, "target": tgt,
            "source_point": [0.0, 0.0], "target_point": [0.0, 0.0],
            "waypoints": []}


def _build_fixture():
    """Граф холста 1920x1080; концы рёбер запечены каноном seating."""
    nodes = [
        # A: обычный bbox-узел; партнёр на 20px ниже оси центра (внутри створа)
        _node("box", "equipment", 250, 260, bbox=(200, 200, 320, 300)),
        _node("p_box", "connector", 270, 450),
        # B: коннектор; партнёр — bbox-блок строго на оси
        _node("conn_t", "connector", 520, 300),
        _node("eq2", "equipment", 520, 560, bbox=(500, 480, 620, 560)),
        # C: FIXED_SIZES-скин (armatura_ruchn, aspect 0.5: бокс 42x38, графика
        # 42x21 -> letterbox по 8.5px сверху/снизу); партнёр СНИЗУ — только
        # вертикальный подход видит разницу bbox vs content-rect
        _node("skin1", "equipment", 250, 821, bbox=(800, 231, 842, 269),
              cls="armatura_ruchn"),
        _node("p_skin", "connector", 420, 821),
        # D: тот же скин, но С segmentation (ромб ВНУТРИ bbox, нижняя вершина
        # y=265: между content-rect 260.5 и bbox 269 — различает три ответа)
        _node("skin2", "equipment", 250, 1121, bbox=(1100, 231, 1142, 269),
              seg=(1121, 235, 1136, 250, 1121, 265, 1106, 250),
              cls="armatura_ruchn"),
        _node("p_skin2", "connector", 420, 1121),
        # E: полигон без скина (ромб = bbox по вершинам); партнёр на 25px выше
        # оси — канонный луч вдоль lock-оси даёт точку на наклонной грани
        _node("poly1", "equipment", 250, 1460, bbox=(1400, 190, 1520, 310),
              seg=(1460, 190, 1520, 250, 1460, 310, 1400, 250)),
        _node("p_poly", "connector", 225, 1700),
    ]
    links = [
        _edge("e_bbox", "box", "p_box"),
        _edge("e_conn", "conn_t", "eq2"),
        _edge("e_skin", "skin1", "p_skin"),
        _edge("e_skinpoly", "skin2", "p_skin2"),
        _edge("e_poly", "poly1", "p_poly"),
    ]
    g = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": nodes, "links": links,
        "text_blocks": [], "bindings": [],
    }
    seating.reseat_all_endpoints(g)
    return g


# ── Утилиты сравнения ────────────────────────────────────────────────────

def _end_xy(e, role):
    p = e["source_point"] if role == "source" else e["target_point"]
    return (float(p[1]), float(p[0]))         # [y, x] -> (x, y)


def _canon_ends(state):
    """Канонные концы для ДАННОГО состояния узлов/маршрутов (пересадка копии)."""
    c = deepcopy(state)
    seating.reseat_all_endpoints(c)
    return {e["id"]: e for e in c["links"]}


def _diverge(state):
    """{случай: (px, measured_xy, canon_xy)} — расхождение концов с каноном."""
    canon = _canon_ends(state)
    got = {e["id"]: e for e in state["links"]}
    out = {}
    for case, (eid, role) in CASES.items():
        m = _end_xy(got[eid], role)
        c = _end_xy(canon[eid], role)
        out[case] = (math.hypot(m[0] - c[0], m[1] - c[1]), m, c)
    return out


def _state_from_editor(ed):
    return {"graph": {"image_size": [1080, 1920]},
            "nodes": deepcopy(list(ed.nodes.values())),
            "links": deepcopy(ed.edges_data)}


# ── Харнесс редактора (offscreen; образец tests/ui/test_connection_point.py) ──

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _editor(tmp_dir, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_dir / "raster.png"
    img.save(str(ip))
    gp = tmp_dir / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = AdvancedGraphEditor()
    # Как в бою: вкладка ставит режим холста ДО load_data (base_graph_tab:449);
    # в нём CONNECTOR_MARKER_RADIUS = 4.0 (_VIS_CANVAS), не legacy 8.
    ed._canvas_mode = True
    assert ed.load_data(str(ip), str(gp))
    return ed


def _simple_editor(tmp_dir, graph):
    """«Проверка схемы» — тот же харнесс, но БЕЗ режима холста.

    Вкладка растровая: CONNECTOR_MARKER_RADIUS = 8 (legacy), CLICK_THRESHOLD = 20.
    """
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    img = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_dir / "raster.png"
    img.save(str(ip))
    gp = tmp_dir / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")

    ed = SimpleGraphEditor()
    assert ed.load_data(str(ip), str(gp))
    return ed


# ── Прогон всех реализаций (один раз на модуль) ──────────────────────────

@pytest.fixture(scope="module")
def results(qapp, tmp_path_factory):
    fixture = _build_fixture()
    byid = {n["id"]: n for n in fixture["nodes"]}
    baked = {e["id"]: e for e in fixture["links"]}
    out = {}

    # 1. Канон: пересадка уже канонического графа обязана быть no-op
    st = deepcopy(fixture)
    seating.reseat_all_endpoints(st)
    out["seating"] = _diverge(st)

    # 2. _recalculate_edge (полный пересчёт ребра) — мутирует рёбра модели
    ed = _editor(tmp_path_factory.mktemp("recalc"), fixture)
    for e in list(ed.edges_data):
        ed._recalculate_edge(e)
    out["recalculate_edge"] = _diverge(_state_from_editor(ed))

    # 3. optimize_edge — через undo-команду, как по кнопке
    ed = _editor(tmp_path_factory.mktemp("optimize"), fixture)
    for e in fixture["links"]:
        assert ed.optimize_edge(e["source"], e["target"])
    out["optimize_edge"] = _diverge(_state_from_editor(ed))

    # 4. auto_fix_graph — чистая функция, двигает узлы; канон считается
    #    на ЕГО ЖЕ выходных позициях узлов (сравнение честное: «конец
    #    соответствует канону для итоговой геометрии»)
    nodes = {n["id"]: deepcopy(n) for n in fixture["nodes"]}
    edges = deepcopy(fixture["links"])
    from ui.editors.autofix_chains import auto_fix_graph
    auto_fix_graph(nodes, edges)
    out["auto_fix_graph"] = _diverge({
        "graph": {"image_size": [1080, 1920]},
        "nodes": list(nodes.values()), "links": edges})

    # 5. get_connection_point — не мутирует; вызов как на загрузке:
    #    точка на узле случая в сторону ЦЕНТРОИДА партнёра
    ed = _editor(tmp_path_factory.mktemp("connpoint"), fixture)
    gcp = {}
    for case, (eid, role) in CASES.items():
        e = baked[eid]
        node_id = e["source"] if role == "source" else e["target"]
        other = byid[e["target"] if role == "source" else e["source"]]
        ocx, ocy = float(other["centroid"][1]), float(other["centroid"][0])
        m = ed.get_connection_point(node_id, ocx, ocy)
        c = _end_xy(e, role)
        gcp[case] = (math.hypot(m[0] - c[0], m[1] - c[1]), tuple(m), c)
    out["get_connection_point"] = gcp

    # 6. FXML get_line_endpoints — чужой модуль, может не импортироваться
    try:
        from modules.graph_to_fxml import get_line_endpoints
    except Exception as exc:                                 # noqa: BLE001
        out["fxml_endpoints"] = (
            "skip", f"modules.graph_to_fxml неимпортируем: {exc!r}")
    else:
        fx = {}
        for case, (eid, role) in CASES.items():
            e = baked[eid]
            ends = get_line_endpoints(e, byid)
            assert ends is not None
            m = ends[0] if role == "source" else ends[1]
            c = _end_xy(e, role)
            fx[case] = (math.hypot(m[0] - c[0], m[1] - c[1]), tuple(m), c)
        out["fxml_endpoints"] = fx

    # 7. add_edge — создание нового ребра: старт БЕЗ рёбер (инструмент
    #    отказывается добавлять существующее), пары — те же, что в фикстуре.
    #    Созданным рёбрам возвращаются канонические id — для _diverge.
    empty = deepcopy(fixture)
    empty["links"] = []
    ed = _editor(tmp_path_factory.mktemp("addedge"), empty)
    for e in fixture["links"]:
        assert ed.add_edge(e["source"], e["target"])
    st = _state_from_editor(ed)
    eid_by_pair = {(e["source"], e["target"]): e["id"] for e in fixture["links"]}
    for e in st["links"]:
        e["id"] = eid_by_pair[(e["source"], e["target"])]
    out["add_edge"] = _diverge(st)

    # 8. SimpleGraphEditor.add_edge — «Проверка схемы»: посадка диспетчером
    #    «Контуров» (2026-08-25). Расхождения с каноном тут ОЖИДАЕМЫ и лежат
    #    в ALLOWLIST — это и есть новый контракт вкладки.
    ed = _simple_editor(tmp_path_factory.mktemp("simple_addedge"), empty)
    for e in fixture["links"]:
        assert ed.add_edge(e["source"], e["target"])
    st = _state_from_editor(ed)
    for e in st["links"]:
        e["id"] = eid_by_pair[(e["source"], e["target"])]
    out["simple_add_edge"] = _diverge(st)

    return out


# ── Предусловия фикстуры (ломаются — матрица теряет смысл, чинить их) ────

def test_fixture_preconditions():
    g = _build_fixture()
    byid = {n["id"]: n for n in g["nodes"]}
    baked = {e["id"]: e for e in g["links"]}

    # сорта узлов действительно те, что заявлены в CASES
    assert "unknow" not in FIXED_SIZES
    assert "armatura_ruchn" in FIXED_SIZES

    # letterbox скина существует (иначе случаи skin/skin_poly вырождаются):
    # content-rect уже bbox по вертикали более чем на straight_tol
    cr = _skin_content_rect(byid["skin1"])
    assert cr is not None, "нет tools/skin_geometry.json или маппинга скина"
    bb = byid["skin1"]["bbox"]
    assert cr[1] - bb[1] > 3.0 and bb[3] - cr[3] > 3.0

    # запечённый канон — ручная геометрия (страховка от дрейфа фикстуры)
    assert baked["e_bbox"]["source_point"] == pytest.approx([270.0, 320.0])
    assert baked["e_conn"]["source_point"] == pytest.approx([520.0, 300.0])
    assert baked["e_skin"]["source_point"] == pytest.approx([260.5, 821.0])
    assert baked["e_skinpoly"]["source_point"] == pytest.approx([260.5, 1121.0])
    assert baked["e_poly"]["source_point"] == pytest.approx([225.0, 1495.0])


def test_simple_add_edge_divergences_are_the_declared_ones(results):
    """Новый контракт «Проверки схемы» заперт ЧИСЛАМИ, а не фактом расхождения.

    ⚠ Зачем отдельный тест: ALLOWLIST утверждает лишь `dist > TOL`, и на этом
    он слеп к подмене пути. Замерено зондом 2026-08-25 — возврат `add_edge`
    на канонную посадку оставил всю матрицу ЗЕЛЁНОЙ: у канона скин тоже
    расходится на 8.50px, и «расхождение есть» выполняется в обоих мирах.
    Отличает миры только величина у `skin_poly`: диспетчер смотрит СЫРОЙ
    segmentation и сажает конец на КОНТУР (4.50px), канон предпочитает скин
    и уводит на letterbox (8.50px).

    Числа абсолютные: тест, вычисляющий ожидание из проверяемого пути,
    остался бы зелёным при любом его поведении.
    """
    r = results["simple_add_edge"]
    assert r["skin"][0] == pytest.approx(8.50, abs=0.01), \
        "скиновый конец ушёл не на рамку bbox"
    assert r["skin_poly"][0] == pytest.approx(4.50, abs=0.01), \
        ("у скина С контуром конец обязан сесть на КОНТУР (сырой seg-чек "
         "диспетчера); 8.50px здесь означает возврат на канон")
    for case in ("connector", "polygon", "bbox"):
        assert r[case][0] <= TOL, \
            f"{case}: новый путь разошёлся с каноном вне заявленного"


def test_allowlist_keys_are_in_matrix():
    """Мертвые записи ALLOWLIST запрещены — каждая обязана быть в матрице."""
    for impl, case in ALLOWLIST:
        assert impl in IMPLS, f"неизвестная реализация в ALLOWLIST: {impl}"
        assert case in CASES, f"неизвестный случай в ALLOWLIST: {case}"


# ── Сама матрица ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("impl", IMPLS)
def test_seat_contract_matrix(results, impl, case):
    r = results[impl]
    if isinstance(r, tuple) and r[0] == "skip":
        pytest.skip(r[1])
    dist, measured, canon = r[case]
    key = (impl, case)
    if key in ALLOWLIST:
        assert dist > TOL, (
            f"{key}: расхождение с каноном исчезло ({dist:.2f}px <= {TOL}) — "
            f"похоже, Э1 привёл реализацию к канону. Сожгите запись ALLOWLIST: "
            f"{ALLOWLIST[key]}")
    else:
        assert dist <= TOL, (
            f"{key}: конец {measured} против канона {canon} — "
            f"{dist:.2f}px > {TOL}. Новое расхождение вне ALLOWLIST.")
