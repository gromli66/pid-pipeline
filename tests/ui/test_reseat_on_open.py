# -*- coding: utf-8 -*-
"""§8.3.1 EDITOR_AFTER_LAYOUT_PLAN: посадка концов рёбер чинится при открытии
холста (страховка от смеси контрактов посадки до Э1).

Канон — `seating`: конец у коннектора = центроид, жёстко. На каноничном
холсте починка — no-op (файл не перезаписывается).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")


def _canvas(tmp_path, target_point):
    g = {
        "directed": False,
        "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [100.0, 100.0], "bbox": [80, 80, 120, 120]},
            {"id": "b", "type": "connector", "class_name": "connector",
             "centroid": [100.0, 300.0], "bbox": None},
        ],
        "links": [{"id": "e1", "source": "a", "target": "b",
                   "source_point": [100.0, 120.0],
                   "target_point": target_point, "waypoints": []}],
        "text_blocks": [],
        "bindings": [],
    }
    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    return p


def test_off_canon_connector_end_is_reseated_to_centroid(tmp_path):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])   # конец в 4 px от центроида
    assert _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    assert g["links"][0]["target_point"] == [100.0, 300.0]
    # Каноничный конец у оборудования не тронут.
    assert g["links"][0]["source_point"] == [100.0, 120.0]


def test_canonical_canvas_is_noop(tmp_path):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])
    assert _reseat_canvas_endpoints(p)       # первая починка
    before = p.read_text(encoding="utf-8")
    assert not _reseat_canvas_endpoints(p)   # идемпотентно
    assert p.read_text(encoding="utf-8") == before


def test_broken_file_is_skipped(tmp_path):
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = tmp_path / "graph_canvas.json"
    p.write_text("{оборванный", encoding="utf-8")
    assert not _reseat_canvas_endpoints(p)


def test_manual_route_migrates_to_anchor(tmp_path):
    """ПЕРЕОБЪЯВЛЕН 2026-08-02 (решение заказчика: жест «оставить как
    нарисовал» не нужен). Заморозка маршрута отменена, но точки оператора
    не выбрасываются: при открытии они ПЕРЕВОДЯТСЯ В ЯКОРЯ (порты с
    владельцем-ребром). Итог: геометрия концов та же, пометка снята,
    маршрут снова участвует в пересчёте, второй прогон — no-op."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])
    g = json.loads(p.read_text(encoding="utf-8"))
    g["links"][0]["_manual_route"] = True
    p.write_text(json.dumps(g), encoding="utf-8")

    assert _reseat_canvas_endpoints(p)        # миграция: заморозка -> якорь
    g2 = json.loads(p.read_text(encoding="utf-8"))
    e = g2["links"][0]
    assert e["source_point"] == [100.0, 120.0], \
        "конец на БОКСЕ обязан уцелеть — его держит якорь"
    assert e["target_point"] == [100.0, 300.0], \
        "конец на КОННЕКТОРЕ приходит к центроиду (канон), якорить нечего"
    assert "_manual_route" not in e, "отменённая пометка обязана уйти"
    anchors = [pt for n in g2["nodes"] for pt in (n.get("_ports") or [])]
    assert anchors and all(a.get("edge") for a in anchors), \
        f"точка оператора не закреплена якорем: {anchors}"
    assert not _reseat_canvas_endpoints(p), "миграция обязана быть идемпотентной"


def _stub_canvas(tmp_path, extra_nodes=()):
    """Конец на правой грани бокса ВНЕ порта, перпендикулярный стаб к wp:
    sp [115, 120] -> wp [115, 200] -> wp [50, 200] -> коннектор [50, 300]."""
    g = {
        "directed": False,
        "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [100.0, 100.0], "bbox": [80.0, 80.0, 120.0, 120.0]},
            {"id": "b", "type": "connector", "class_name": "connector",
             "centroid": [50.0, 300.0], "bbox": None},
        ] + list(extra_nodes),
        "links": [{"id": "e1", "source": "a", "target": "b",
                   "source_point": [115.0, 120.0],
                   "target_point": [50.0, 300.0],
                   "waypoints": [[115.0, 200.0], [50.0, 200.0]]}],
        "text_blocks": [],
        "bindings": [],
    }
    p = tmp_path / "graph_canvas.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    return p


def test_wp_end_with_perpendicular_stub_migrates_to_port(tmp_path):
    """Этап A: конец С waypoints и перпендикулярным стабом мигрирует на порт
    ВМЕСТЕ с осевым сдвигом смежного waypoint — стаб остаётся
    перпендикулярным грани, ортогональность маршрута цела."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _stub_canvas(tmp_path)
    assert _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    e = g["links"][0]
    assert e["source_point"] == [100.0, 120.0], \
        "конец обязан мигрировать в порт (центр правой грани)"
    assert e["waypoints"][0] == [100.0, 200.0], \
        "смежный waypoint обязан сдвинуться вдоль грани на ту же величину"
    assert e["waypoints"][1] == [50.0, 200.0], "дальний waypoint тронут"
    assert e["target_point"] == [50.0, 300.0]


def test_wp_end_migration_blocked_by_foreign_box(tmp_path):
    """Сдвинутый стаб прошил бы чужую рамку — конец НЕ мигрирует."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    wall = {"id": "w", "type": "equipment", "class_name": "unknow",
            "centroid": [100.0, 160.0], "bbox": [140.0, 90.0, 180.0, 110.0]}
    p = _stub_canvas(tmp_path, extra_nodes=[wall])
    # файл может переписаться легитимно (подпись _auto_route у зигзага,
    # 2026-08-01) — судим ГЕОМЕТРИЮ: конец и колени не сдвинулись
    _reseat_canvas_endpoints(p)
    g = json.loads(p.read_text(encoding="utf-8"))
    e = g["links"][0]
    assert e["source_point"] == [115.0, 120.0]
    assert e["waypoints"] == [[115.0, 200.0], [50.0, 200.0]]


def test_manual_route_preserved_among_reseated_edges(tmp_path):
    """Смешанный холст: обычное ребро чинится, бывшее ручное — расфиксируется.

    ПЕРЕОБЪЯВЛЕН 2026-08-02: заморозка маршрута отменена заказчиком. Конец на
    БОКСЕ у такого ребра сохраняется якорем, а конец на КОННЕКТОРЕ приходит к
    центроиду: у коннектора канонический конец и есть центроид, и 296 вместо
    300 держалось только заморозкой (судья считал такой конец conn_off)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])
    g = json.loads(p.read_text(encoding="utf-8"))
    g["links"][0]["_manual_route"] = True
    g["nodes"].append({"id": "c", "type": "connector",
                       "class_name": "connector",
                       "centroid": [300.0, 300.0], "bbox": None})
    g["links"].append({"id": "e2", "source": "a", "target": "c",
                       "source_point": [120.0, 113.0],
                       "target_point": [300.0, 296.0], "waypoints": []})
    p.write_text(json.dumps(g), encoding="utf-8")

    assert _reseat_canvas_endpoints(p)
    g2 = json.loads(p.read_text(encoding="utf-8"))
    # Бывшее ручное: конец на боксе удержан якорем, конец на коннекторе —
    # приведён к канону (центроид), пометка снята.
    assert g2["links"][0]["source_point"] == [100.0, 120.0]
    assert g2["links"][0]["target_point"] == [100.0, 300.0]
    assert "_manual_route" not in g2["links"][0]
    # Обычное — конец у коннектора пересажен в центроид.
    assert g2["links"][1]["target_point"] == [300.0, 300.0]


def test_manual_ray_end_is_materialized_into_data(tmp_path):
    """Э1 «экран == данные»: конец _manual_route ВНУТРИ контура — то, что
    отрисовочная доводка показывала лучом из центроида, — при открытии
    один раз пишется в данные (конец на контур, waypoints оператора целы)."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = {
        "directed": False,
        "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "p", "type": "equipment", "class_name": "unknow",
             "centroid": [100.0, 100.0], "bbox": [80, 80, 120, 120],
             "segmentation": [80, 80, 120, 80, 120, 120, 80, 120]},
            {"id": "b", "type": "connector", "class_name": "connector",
             "centroid": [100.0, 300.0], "bbox": None},
        ],
        "links": [{"id": "e1", "source": "p", "target": "b",
                   "source_point": [100.0, 110.0],
                   "target_point": [100.0, 300.0],
                   "waypoints": [[100.0, 200.0]],
                   "_manual_route": True}],
        "text_blocks": [],
        "bindings": [],
    }
    path = tmp_path / "graph_canvas.json"
    path.write_text(json.dumps(g), encoding="utf-8")

    assert _reseat_canvas_endpoints(path)
    g2 = json.loads(path.read_text(encoding="utf-8"))
    e = g2["links"][0]
    assert e["source_point"] == [100.0, 120.0]      # на контуре, по лучу к wp
    assert e["waypoints"] == [[100.0, 200.0]]       # маршрут оператора цел
    assert e["target_point"] == [100.0, 300.0]

    before = path.read_text(encoding="utf-8")
    assert not _reseat_canvas_endpoints(path)        # идемпотентно
    assert path.read_text(encoding="utf-8") == before


def test_skin_end_lifted_to_bbox_on_open(tmp_path):
    """«Символ тянется на рамку» (2026-08-01): конец на letterbox-грани
    серверного канона при открытии поднимается на рамку bbox вдоль стаба;
    идемпотентно."""
    from modules.graph.core.pretransform import (_CLASS_SKIN, _SKIN_ASPECT,
                                                 FIXED_SIZES,
                                                 _skin_content_rect)
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    cls = next(c for c in sorted(FIXED_SIZES)
               if _SKIN_ASPECT.get(_CLASS_SKIN.get(c, "")))
    node = {"id": "s", "type": "equipment", "class_name": cls,
            "centroid": [21.5, 140.0], "bbox": [0.0, 0.0, 280.0, 43.0]}
    cr = _skin_content_rect(node)
    assert cr is not None and abs(cr[0] - 0.0) > 1.0, \
        "нужен класс с letterbox-полями по X для этой фикстуры"

    g = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [node,
                  {"id": "b", "type": "connector", "class_name": "connector",
                   "centroid": [21.5, -100.0], "bbox": None}],
        "links": [{"id": "e1", "source": "s", "target": "b",
                   "source_point": [21.5, cr[0]],
                   "target_point": [21.5, -100.0], "waypoints": []}],
        "text_blocks": [], "bindings": [],
    }
    path = tmp_path / "graph_canvas.json"
    path.write_text(json.dumps(g), encoding="utf-8")

    assert _reseat_canvas_endpoints(path)
    g2 = json.loads(path.read_text(encoding="utf-8"))
    e = g2["links"][0]
    assert e["source_point"] == [21.5, 0.0]      # рамка bbox, тангенс цел
    assert not _reseat_canvas_endpoints(path)    # идемпотентно


def test_unsigned_zigzag_gets_auto_flag_on_open(tmp_path):
    """«Зигзаг прибит гвоздями» (2026-08-01): маршрут без подписи (сервер
    старых эпох не ставил _auto_route) при открытии подписывается как
    авто — drag снова ведёт его; _manual_route не трогается."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    g = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "a", "type": "equipment", "class_name": "unknow",
             "centroid": [100.0, 100.0], "bbox": [80, 80, 120, 120]},
            {"id": "b", "type": "connector", "class_name": "connector",
             "centroid": [200.0, 300.0], "bbox": None},
        ],
        "links": [
            {"id": "e1", "source": "a", "target": "b",
             "source_point": [100.0, 120.0], "target_point": [200.0, 300.0],
             "waypoints": [[100.0, 300.0]]},
            {"id": "e2", "source": "a", "target": "b",
             "source_point": [100.0, 120.0], "target_point": [200.0, 300.0],
             "waypoints": [[200.0, 120.0]], "_manual_route": True},
        ],
        "text_blocks": [], "bindings": [],
    }
    path = tmp_path / "graph_canvas.json"
    path.write_text(json.dumps(g), encoding="utf-8")

    assert _reseat_canvas_endpoints(path)
    g2 = json.loads(path.read_text(encoding="utf-8"))
    e1 = next(e for e in g2["links"] if e["id"] == "e1")
    e2 = next(e for e in g2["links"] if e["id"] == "e2")
    assert e1.get("_auto_route") is True      # безфлаговый подписан
    # ПЕРЕОБЪЯВЛЕНО 2026-08-02: заморозка отменена, поэтому бывшее «ручное»
    # ребро в ЭТОМ ЖЕ проходе расфиксируется и тоже получает подпись —
    # иначе прогон не идемпотентен (второе открытие подписало бы его).
    assert "_manual_route" not in e2
    assert e2.get("_auto_route") is True
    assert not _reseat_canvas_endpoints(path)  # идемпотентно
