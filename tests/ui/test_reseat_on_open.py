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


def test_manual_route_endpoints_survive(tmp_path):
    """Ручная посадка (_manual_route) неприкосновенна — T-D.3 плана."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints

    p = _canvas(tmp_path, [100.0, 296.0])
    g = json.loads(p.read_text(encoding="utf-8"))
    g["links"][0]["_manual_route"] = True
    p.write_text(json.dumps(g), encoding="utf-8")

    assert not _reseat_canvas_endpoints(p)   # чинить нечего: ребро ручное
    g2 = json.loads(p.read_text(encoding="utf-8"))
    assert g2["links"][0]["target_point"] == [100.0, 296.0]


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
    assert not _reseat_canvas_endpoints(p), \
        "миграция сквозь чужую рамку обязана быть отброшена (файл не переписан)"
    g = json.loads(p.read_text(encoding="utf-8"))
    e = g["links"][0]
    assert e["source_point"] == [115.0, 120.0]
    assert e["waypoints"] == [[115.0, 200.0], [50.0, 200.0]]


def test_manual_route_preserved_among_reseated_edges(tmp_path):
    """Смешанный холст: обычное ребро чинится, ручное — не тронуто."""
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
    # Ручное ребро — байт-в-байт как было.
    assert g2["links"][0]["target_point"] == [100.0, 296.0]
    assert g2["links"][0]["source_point"] == [100.0, 120.0]
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
