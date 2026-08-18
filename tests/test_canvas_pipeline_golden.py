"""T10 (П6) — golden-цепочка graph_validated → pretransform → холст → FXML.

Реальный источник несовместимости json — не настройки оформления (они по
устройству никуда не пишутся, это стережёт T5), а холст: он живёт в 1920x1080,
необратим и пересобирается с нуля при устаревании. Набор ключей узлов/рёбер
нигде не сверяется — закрываем тестом.

Что проверяется:
  • рёбра лежат под ключом `links` (node-link формат networkx), ключа `edges`
    нет; число рёбер считаем как len(links) — graph.num_edges врёт в 3 графах
    корпуса из 8;
  • ключи узлов и рёбер после pretransform — из ожидаемого множества;
  • pretransform ЛЕГАЛЬНО меняет число узлов/рёбер (в корпусе 402/418 → 400/416),
    поэтому равенство не требуется;
  • граф холста идемпотентен (повторный pretransform его не трогает);
  • из холста собирается валидный FXML.

Источник корпуса — загрузчик `tools/corpus.py` (КД7, пункт 0.8): три графа
лежат в git и проходят везде, остальные добавляются из локального `storage/`
этой машины. Синтетический прогон работает и без корпуса вовсе.
"""
import json
import xml.etree.ElementTree as ET

import pytest

from tools.corpus import corpus_paths

CORPUS = corpus_paths()

CANVAS_W, CANVAS_H = 1920, 1080

# Ключи, которые допустимы у узла/ребра холста. Собрано по корпусу (8 графов)
# + тем, что добавляет pretransform. Новый ключ — осознанное решение, а не
# случайная утечка: тест обязан упасть и заставить обновить список.
NODE_KEYS = {
    "id", "type", "centroid", "bbox", "segmentation", "area", "degree",
    "class_id", "class_name", "yolo_idx", "ann_id", "ann_idx", "manual",
    "direction", "direction_node", "flow_axis", "flow_direction",
    "pass_through", "kks_full", "skin_info", "_axis",
}
EDGE_KEYS = {
    "id", "source", "target", "source_point", "target_point", "waypoints",
    "length", "straight_line_distance", "is_terminal", "manual",
    "render_color", "render_width", "color", "dashed", "diameter_text",
    "diameter_value", "connection_type", "_src_side", "_tgt_side",
    "_manual_route", "_auto_route",
}
NODE_REQUIRED = {"id", "type", "centroid"}
EDGE_REQUIRED = {"source", "target", "source_point", "target_point"}


def _synthetic_graph() -> dict:
    """Мини-граф в координатах растра 3516x4968 (как у корпуса)."""
    nodes = [
        {"id": "n1", "type": "equipment", "centroid": [1000.0, 1200.0],
         "bbox": [1140.0, 940.0, 1260.0, 1060.0], "segmentation": None,
         "area": 14400, "degree": 1, "class_id": 1, "class_name": "nasos",
         "yolo_idx": 0},
        {"id": "n2", "type": "connector", "centroid": [1000.0, 1600.0],
         "bbox": None, "segmentation": None, "area": 225, "degree": 2,
         "class_id": -1, "class_name": "connector", "yolo_idx": None},
        {"id": "n3", "type": "equipment", "centroid": [1400.0, 1600.0],
         "bbox": [1540.0, 1340.0, 1660.0, 1460.0], "segmentation": None,
         "area": 14400, "degree": 1, "class_id": 2, "class_name": "zadvizhka",
         "yolo_idx": 1},
    ]
    links = [
        {"id": "e1", "source": "n1", "target": "n2",
         "source_point": [1000.0, 1260.0], "target_point": [1000.0, 1585.0],
         "waypoints": [], "length": 325.0, "straight_line_distance": 325.0,
         "is_terminal": False},
        {"id": "e2", "source": "n2", "target": "n3",
         "source_point": [1015.0, 1600.0], "target_point": [1340.0, 1600.0],
         "waypoints": [], "length": 325.0, "straight_line_distance": 325.0,
         "is_terminal": False},
    ]
    return {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [3516, 4968], "num_nodes": 3,
                  # намеренно врущие метаданные: считать надо len(links)
                  "num_edges": 99},
        "nodes": nodes, "links": links, "text_blocks": [], "bindings": [],
    }


def _check_canvas_graph(canvas: dict, source: dict):
    assert "links" in canvas, "рёбра обязаны лежать под ключом links"
    assert "edges" not in canvas, "ключа edges в node-link формате быть не должно"

    size = canvas["graph"]["image_size"]
    assert [int(size[0]), int(size[1])] == [CANVAS_H, CANVAS_W]
    assert canvas["graph"].get("canvas_transform"), \
        "без canvas_transform холст не развернуть обратно в оригинал"

    n_src, n_dst = len(source["nodes"]), len(canvas["nodes"])
    e_src, e_dst = len(source["links"]), len(canvas["links"])
    # pretransform легально выкидывает узлы (в корпусе 402/418 → 400/416)
    assert 0 < n_dst <= n_src
    assert 0 < e_dst <= e_src

    ids = {n["id"] for n in canvas["nodes"]}
    for n in canvas["nodes"]:
        extra = set(n) - NODE_KEYS
        assert not extra, f"неожиданные ключи узла: {sorted(extra)}"
        assert NODE_REQUIRED <= set(n), f"узел без обязательных ключей: {n.get('id')}"
    for e in canvas["links"]:
        extra = set(e) - EDGE_KEYS
        assert not extra, f"неожиданные ключи ребра: {sorted(extra)}"
        assert EDGE_REQUIRED <= set(e), f"ребро без обязательных ключей: {e.get('id')}"
        assert e["source"] in ids and e["target"] in ids, "ребро в никуда"


def _check_fxml(xml: str):
    assert xml and xml.lstrip().startswith("<"), "FXML пустой"
    root = ET.fromstring(xml)          # падает на невалидном XML
    assert len(list(root.iter())) > 1


def test_t10_synthetic_chain():
    """Цепочка целиком на синтетике — работает без корпуса (в т.ч. в CI).

    FXML из холста собирает 1:1-сериализатор canvas_to_fxml (2026-08-03) —
    тот же, что зовёт task_generate_fxml для canvas-пути.
    """
    from modules.graph.core.pretransform import pretransform
    from modules.canvas_to_fxml import generate_canvas_fxml

    src = _synthetic_graph()
    canvas, transform, _stats = pretransform(src)
    _check_canvas_graph(canvas, src)
    assert transform["canvas"] == [CANVAS_W, CANVAS_H]
    _check_fxml(generate_canvas_fxml(canvas))


def test_legacy_raster_fxml_still_works():
    """Не-canvas путь (validated в px растра, старый generate_fxml) остаётся
    боевым для графов без холста — smoke, чтобы у него был хоть один тест."""
    from modules.graph_to_fxml import generate_fxml

    _check_fxml(generate_fxml(_synthetic_graph()))


def test_t10_num_edges_metadata_is_not_trusted():
    """graph.num_edges врёт в 3 графах корпуса из 8 — считаем len(links)."""
    from modules.graph.core.pretransform import pretransform

    src = _synthetic_graph()
    assert src["graph"]["num_edges"] != len(src["links"])
    canvas, _t, _s = pretransform(src)
    assert len(canvas["links"]) <= len(src["links"])


def test_t10_pretransform_is_idempotent():
    """Повторный прогон графа холста ничего не меняет (пере-открытие
    сохранённого graph_canvas идёт этим же путём)."""
    from modules.graph.core.pretransform import pretransform

    canvas, _t, _s = pretransform(_synthetic_graph())
    again, _t2, stats2 = pretransform(canvas)
    assert stats2.get("skipped") is True
    assert again["nodes"] == canvas["nodes"]
    assert again["links"] == canvas["links"]


@pytest.mark.parametrize("path", CORPUS.values(), ids=list(CORPUS))
def test_t10_corpus_chain(path):
    """Та же цепочка на реальных графах корпуса."""
    from modules.graph.core.pretransform import pretransform
    from modules.canvas_to_fxml import generate_canvas_fxml

    src = json.loads(path.read_text(encoding="utf-8"))
    canvas, _transform, _stats = pretransform(src)
    _check_canvas_graph(canvas, src)
    _check_fxml(generate_canvas_fxml(canvas))
