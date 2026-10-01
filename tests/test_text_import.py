# -*- coding: utf-8 -*-
"""Импорт подписей на холст и якоря §3.9.

Проверяется главное утверждение §3.9: блок с якорем сохраняет положение
ОТНОСИТЕЛЬНО СВОЕГО ЭЛЕМЕНТА, а не относительно листа. Абсолютная точка при
этом может уехать далеко — ровно настолько, насколько уехал элемент.

Данных заказчика здесь нет: геометрия синтетическая, подписи дописаны в тесте.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from modules.graph.core import text_import

FIXTURE = Path(__file__).parent / "fixtures" / "layout" / "synth_med.json"


def _validated():
    """Крошечный граф в координатах растра + одна подпись у узла."""
    return {
        "graph": {"image_size": [1000, 2000]},
        "nodes": [
            {"id": "node_1", "type": "equipment", "class_name": "armatura_ruchn",
             "centroid": [100.0, 200.0], "bbox": [180.0, 90.0, 220.0, 110.0]},
            {"id": "node_2", "type": "equipment", "class_name": "armatura_ruchn",
             "centroid": [500.0, 900.0], "bbox": [880.0, 490.0, 920.0, 510.0]},
        ],
        "links": [
            {"id": "edge_1", "source": "node_1", "target": "node_2",
             "source_point": [100.0, 220.0], "target_point": [500.0, 880.0],
             "waypoints": []},
        ],
        # подпись НАД узлом: справа от него идёт труба, и блок у трубы
        # честно получил бы якорь-ребро — здесь проверяется якорь-узел
        "text_blocks": [
            {"id": "block_1", "bbox": [180.0, 40.0, 250.0, 60.0],
             "text": "V-1", "merged_into": None},
        ],
        "bindings": [],
    }


def _moved_canvas(validated, dx=0.0, dy=0.0, node_id="node_1"):
    """Холст = pretransform от графа, затем узел уехал на (dx, dy)."""
    from modules.graph.core.canvas_input import to_canvas

    canvas, _tr = to_canvas(validated)
    for n in canvas["nodes"]:
        if n["id"] == node_id:
            c = n["centroid"]
            n["centroid"] = [c[0] + dy, c[1] + dx]
            bb = n["bbox"]
            n["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
    return canvas


def test_block_follows_its_node_not_the_sheet():
    """Узел уехал на 300 px — подпись обязана уехать с ним, а не остаться."""
    validated = _validated()
    canvas = _moved_canvas(validated, dx=300.0, dy=-120.0)

    from modules.graph.core.canvas_input import to_canvas
    pre, _tr = to_canvas(validated)
    before = pre["text_blocks"][0]["bbox"]

    stats = text_import.import_text(canvas, validated)
    after = canvas["text_blocks"][0]["bbox"]

    assert stats["anchored_node"] == 1, stats
    assert after[0] == pytest.approx(before[0] + 300.0, abs=1e-6)
    assert after[1] == pytest.approx(before[1] - 120.0, abs=1e-6)


def test_block_keeps_size():
    validated = _validated()
    canvas = _moved_canvas(validated, dx=50.0)
    from modules.graph.core.canvas_input import to_canvas
    pre, _tr = to_canvas(validated)
    w0 = pre["text_blocks"][0]["bbox"][2] - pre["text_blocks"][0]["bbox"][0]

    text_import.import_text(canvas, validated)
    bb = canvas["text_blocks"][0]["bbox"]
    assert bb[2] - bb[0] == pytest.approx(w0, abs=1e-6)


def test_far_block_has_no_anchor_and_stays():
    """Штамп и рамка ни к чему не относятся — им «как в оригинале» и правильно."""
    validated = _validated()
    validated["text_blocks"][0]["bbox"] = [1500.0, 900.0, 1700.0, 940.0]
    canvas = _moved_canvas(validated, dx=300.0)

    from modules.graph.core.canvas_input import to_canvas
    pre, _tr = to_canvas(validated)
    before = list(pre["text_blocks"][0]["bbox"])

    stats = text_import.import_text(canvas, validated)
    assert stats["no_anchor"] == 1
    assert canvas["text_blocks"][0]["bbox"] == before


def test_anchor_max_is_the_switch():
    """Тот же блок: с большим порогом якорь есть, с нулевым — нет."""
    validated = _validated()
    validated["text_blocks"][0]["bbox"] = [400.0, 92.0, 470.0, 108.0]
    for anchor_max, expect_anchor in ((1000.0, True), (0.5, False)):
        canvas = _moved_canvas(validated, dx=300.0)
        stats = text_import.import_text(canvas, validated,
                                        anchor_max=anchor_max)
        assert (stats["anchored_node"] + stats["anchored_edge"] == 1) is \
            expect_anchor, (anchor_max, stats)


def test_binding_without_side_is_synthesized_and_block_follows():
    """Привязки из вкладки привязки идут БЕЗ side, а без него производная
    позиция не работает ни для одной привязки корпуса."""
    validated = _validated()
    validated["bindings"] = [
        {"block_id": "block_1", "node_id": "node_1", "kind": "node"},
    ]
    canvas = _moved_canvas(validated, dx=400.0, dy=200.0)

    stats = text_import.import_text(canvas, validated)
    assert stats["bound"] == 1
    binding = canvas["bindings"][0]
    assert binding["side"] in ("top", "right", "left", "bottom")
    assert "gap" in binding

    # блок стоит у своей цели, а не там, где был на листе
    node = next(n for n in canvas["nodes"] if n["id"] == "node_1")
    nb = node["bbox"]
    bb = canvas["text_blocks"][0]["bbox"]
    assert bb[0] >= nb[0] - 200 and bb[2] <= nb[2] + 200


def test_existing_side_is_not_overwritten():
    validated = _validated()
    validated["bindings"] = [
        {"block_id": "block_1", "node_id": "node_1", "kind": "node",
         "side": "top", "gap": 11.0},
    ]
    canvas = _moved_canvas(validated)
    text_import.import_text(canvas, validated)
    assert canvas["bindings"][0]["side"] == "top"
    assert canvas["bindings"][0]["gap"] == 11.0


def test_merged_blocks_are_dropped():
    validated = _validated()
    validated["text_blocks"].append(
        {"id": "block_2", "bbox": [230.0, 92.0, 300.0, 108.0], "text": "V-1",
         "merged_into": "block_1"})
    canvas = _moved_canvas(validated)
    stats = text_import.import_text(canvas, validated)
    assert stats["blocks"] == 1
    assert [b["id"] for b in canvas["text_blocks"]] == ["block_1"]


def test_graph_without_text_gives_empty_lists():
    validated = _validated()
    validated["text_blocks"] = []
    canvas = _moved_canvas(validated)
    stats = text_import.import_text(canvas, validated)
    assert stats["blocks"] == 0
    assert canvas["text_blocks"] == [] and canvas["bindings"] == []


def test_validated_graph_is_not_mutated():
    validated = _validated()
    snapshot = deepcopy(validated)
    canvas = _moved_canvas(validated, dx=100.0)
    text_import.import_text(canvas, validated)
    assert validated == snapshot


def test_import_after_real_layout_keeps_labels_at_their_objects():
    """Сквозная проверка на синтетике: после настоящей раскладки подписи
    остаются у своих элементов, а не у случайных соседей."""
    pytest.importorskip("shapely")
    from modules.graph.core.canvas_input import to_canvas
    from modules.graph.core.layout import LayoutParams, layout

    validated = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # синтетика без текста — вешаем подпись рядом с каждым десятым узлом
    validated["text_blocks"] = []
    for i, n in enumerate(validated["nodes"]):
        if i % 10 or not n.get("bbox"):
            continue
        bb = n["bbox"]
        validated["text_blocks"].append({
            "id": f"block_{i}", "text": n["id"], "merged_into": None,
            "bbox": [bb[2] + 5.0, bb[1], bb[2] + 60.0, bb[1] + 18.0],
        })
    validated["bindings"] = []

    canvas, _tr = to_canvas(validated)
    canvas, _stats = layout(canvas, LayoutParams())
    pre, _tr2 = to_canvas(validated)

    stats = text_import.import_text(canvas, validated)
    assert stats["blocks"] == len(validated["text_blocks"])
    assert stats["anchored_node"] + stats["anchored_edge"] > 0

    # каждая подпись — у того же узла, рядом с которым её поставили
    byid = {n["id"]: n for n in canvas["nodes"]}
    off = 0
    for blk in canvas["text_blocks"]:
        node = byid.get(blk["text"])
        if node is None or not node.get("bbox"):
            continue
        bx = (blk["bbox"][0] + blk["bbox"][2]) / 2.0
        by = (blk["bbox"][1] + blk["bbox"][3]) / 2.0
        nb = node["bbox"]
        d = text_import._pt_rect(bx, by, tuple(nb))
        if d > text_import.ANCHOR_MAX:
            off += 1
    assert off == 0, f"{off} подписей уехали от своих узлов"


# ─────────── режим холста: старый холст (Р1) и кратные пары (Р7) ───────────
# Красная находка сквозного red-team 02.10: импорт на СТАРЫЙ холст строил
# до-раскладочную геометрию кодом модели — нормализация кратных рёбер сдвигала
# индекс ребра (ключ якоря), центроид клапана уезжал в талию, и подписи
# разлетались (51b339ab — 1411 px). Холст тут синтетический, растр 1920x1080 —
# масштаб 1, «раскладка» — параллельный перенос всего холста: каждая
# заякоренная подпись обязана уехать ровно на него, ни больше ни меньше.

_DX, _DY = 37.0, -21.0


def _conn(cid, x, y):
    return {"id": cid, "type": "connector", "class_name": "connector",
            "centroid": [y, x], "bbox": None}


def _paired_validated():
    """Растр 1920x1080: пара t1-t2 из двух настоящих труб (40 px — Р7
    рассечёт вторую), за ней клапан на трубе, чья линия по растру (y=604)
    не совпадает с детектированным центроидом (y=600) — модель двигает
    центроид в талию. Подписи: у трубы ПОСЛЕ пары и над клапаном."""
    return {
        "graph": {"image_size": [1080, 1920]},
        "nodes": [
            {"id": "t1", "type": "equipment", "class_name": "bak",
             "centroid": [320.0, 130.0], "bbox": [100.0, 280.0, 160.0, 360.0]},
            {"id": "t2", "type": "equipment", "class_name": "bak",
             "centroid": [320.0, 630.0], "bbox": [600.0, 280.0, 660.0, 360.0]},
            _conn("c3", 250.0, 604.0), _conn("c4", 550.0, 604.0),
            {"id": "v", "type": "equipment", "class_name": "armatura_electro",
             "centroid": [600.0, 400.0], "bbox": [380.0, 585.0, 420.0, 615.0]},
        ],
        "links": [
            {"id": "edge_1", "source": "t1", "target": "t2",
             "source_point": [300.0, 160.0], "target_point": [300.0, 600.0],
             "waypoints": []},
            {"id": "edge_2", "source": "t1", "target": "t2",
             "source_point": [340.0, 160.0], "target_point": [340.0, 600.0],
             "waypoints": []},
            {"id": "edge_3", "source": "c3", "target": "v",
             "source_point": [604.0, 250.0], "target_point": [604.0, 380.0],
             "waypoints": []},
            {"id": "edge_4", "source": "v", "target": "c4",
             "source_point": [604.0, 420.0], "target_point": [604.0, 550.0],
             "waypoints": []},
        ],
        "text_blocks": [
            {"id": "b_edge", "bbox": [285.0, 580.0, 315.0, 592.0],
             "text": "Ду50", "merged_into": None},
            {"id": "b_valve", "bbox": [385.0, 560.0, 415.0, 572.0],
             "text": "V-1", "merged_into": None},
        ],
        "bindings": [],
    }


def _shifted(canvas, dx, dy):
    """Холст после «раскладки»: всё перенесено на (dx, dy)."""
    for n in canvas["nodes"]:
        c = n["centroid"]
        n["centroid"] = [c[0] + dy, c[1] + dx]
        if n.get("bbox"):
            bb = n["bbox"]
            n["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
    for e in canvas["links"]:
        for k in ("source_point", "target_point"):
            e[k] = [e[k][0] + dy, e[k][1] + dx]
        e["waypoints"] = [[p[0] + dy, p[1] + dx]
                          for p in e.get("waypoints") or []]
    return canvas


def _assert_follow(canvas, pre, stats, dx, dy):
    assert stats["anchored_edge"] == 1 and stats["anchored_node"] == 1, stats
    before = {b["id"]: b["bbox"] for b in pre["text_blocks"]}
    for blk in canvas["text_blocks"]:
        want = [before[blk["id"]][0] + dx, before[blk["id"]][1] + dy,
                before[blk["id"]][2] + dx, before[blk["id"]][3] + dy]
        assert blk["bbox"] == pytest.approx(want, abs=1e-6), blk["id"]


def test_legacy_pre_is_deploy_geometry():
    """`to_canvas(legacy=True)` — геометрия 238fe6b: пара не рассечена
    (рёбра и их порядок — как во входе), центроид клапана — детекция, рамка
    вокруг него; ключ старого холста в выход не попадает."""
    from modules.graph.core import contact
    from modules.graph.core.canvas_input import to_canvas

    validated = _paired_validated()
    old, _tr = to_canvas(validated, legacy=True)
    new, _tr = to_canvas(validated)
    assert [e["id"] for e in old["links"]] == \
        ["edge_1", "edge_2", "edge_3", "edge_4"]
    assert len(new["links"]) == 5          # модель рассекла вторую трубу
    v_old = next(n for n in old["nodes"] if n["id"] == "v")
    v_new = next(n for n in new["nodes"] if n["id"] == "v")
    assert v_old["centroid"] == [600.0, 400.0]
    bb = v_old["bbox"]
    assert ((bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2) == \
        pytest.approx((600.0, 400.0), abs=1e-9)
    assert v_new["centroid"][0] == pytest.approx(604.0)    # талия на трубе
    assert not any(contact.LEGACY_KEY in n for n in old["nodes"])


def test_import_on_old_canvas_with_parallel_pair_follows_elements():
    """Старый холст (метки нет) с кратной парой во входе: подпись у трубы за
    парой и подпись над клапаном едут ровно на перенос холста. Ошибка ветки:
    индекс ребра съезжал на рассечённую половину пары (подпись прыгала на
    соседнюю трубу), якорь клапана — на сдвиг талии."""
    from modules.graph.core import contact
    from modules.graph.core.canvas_input import to_canvas

    validated = _paired_validated()
    canvas, _tr = to_canvas(validated, legacy=True)     # холст deploy
    assert contact.canvas_model(canvas) is None
    pre = deepcopy(canvas)
    _shifted(canvas, _DX, _DY)

    stats = text_import.import_text(canvas, validated)
    _assert_follow(canvas, pre, stats, _DX, _DY)


def test_import_on_marked_canvas_with_parallel_pair_follows_elements():
    """Новый холст (метка модели) с той же парой: до-раскладочная — модель,
    как у раскладки (пара рассечена, клапан на талии) — подписи едут ровно на
    перенос. Сторож обратной ошибки: старая геометрия на новом холсте."""
    from modules.graph.core import contact
    from modules.graph.core.canvas_input import to_canvas

    validated = _paired_validated()
    canvas, _tr = to_canvas(validated)
    canvas["graph"]["canvas_transform"]["attach_model"] = contact.ATTACH_MODEL
    pre = deepcopy(canvas)
    _shifted(canvas, _DX, _DY)

    stats = text_import.import_text(canvas, validated)
    _assert_follow(canvas, pre, stats, _DX, _DY)


def test_import_on_prior_mark_canvas_uses_its_table(monkeypatch):
    """(4) red-team правок 02.10: холст прежней метки после подъёма метки
    (новая запись реестра, новая текущая таблица — как `bumped` в
    test_attach_mark.py). До-раскладочная строится по таблице СВОЕЙ метки
    (`to_canvas(mark=)`): подписи едут ровно на перенос холста. Ошибка:
    `import_text` выбирал только legacy/модель — модель шла по текущей
    таблице, рамка клапана съезжала на разницу талий, и якорь подписи с
    ней (302171b3: 3 подписи на 1.06 px)."""
    from modules.graph.core import contact
    from modules.graph.core.canvas_input import to_canvas

    validated = _paired_validated()
    mark0 = contact.ATTACH_MODEL
    canvas, _tr = to_canvas(validated)
    canvas["graph"]["canvas_transform"]["attach_model"] = mark0
    pre = deepcopy(canvas)
    _shifted(canvas, _DX, _DY)

    new = dict(contact.WAIST_TABLES[mark0], ELECTRIC_VLV=(0.25, 0.25))
    monkeypatch.setattr(contact, "WAIST_TABLES",
                        dict(contact.WAIST_TABLES, **{"bump": new}))
    monkeypatch.setattr(contact, "ATTACH_MODEL", "bump")
    monkeypatch.setattr(contact, "SKIN_CONTACT_OFFSET", dict(new))

    def vbox(g):
        return next(n for n in g["nodes"] if n["id"] == "v")["bbox"]
    assert vbox(to_canvas(validated)[0]) != vbox(pre), \
        "фикстура: подъём метки обязан менять рамку клапана"
    own, _tr = to_canvas(validated, mark=mark0)
    assert vbox(own) == vbox(pre)
    assert not any(contact.TABLE_KEY in n for n in own["nodes"])

    stats = text_import.import_text(canvas, validated)
    _assert_follow(canvas, pre, stats, _DX, _DY)
    assert not any(contact.TABLE_KEY in n for n in canvas["nodes"])
