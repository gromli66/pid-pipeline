# -*- coding: utf-8 -*-
"""Устаревание холста (§3.6 плана): sha по проекции, а не по файлу.

Проверяются ровно те случаи, ради которых §3.6 написан. Главный из них —
первый: раньше метка считалась как sha всего файла `graph_validated.json`, а
OCR и привязка этот файл переписывают. Холст, собранный до OCR, объявлялся
устаревшим ГАРАНТИРОВАННО, и раскладку выбрасывало бы каждый раз.

Здесь нет ни Qt, ни БД, ни данных заказчика.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from modules.graph.core import canvas_state


def _validated():
    """Маленький валидированный граф: два узла и ребро между ними."""
    return {
        "graph": {"image_size": [1000, 2000]},
        "nodes": [
            {"id": "node_1", "type": "equipment", "class_name": "armatura_ruchn",
             "centroid": [100.0, 200.0], "bbox": [180.0, 90.0, 220.0, 110.0],
             "segmentation": [180.0, 90.0, 220.0, 90.0, 220.0, 110.0]},
            {"id": "node_2", "type": "connector", "centroid": [100.0, 400.0]},
        ],
        "links": [
            {"id": "edge_1", "source": "node_1", "target": "node_2",
             "source_point": [100.0, 220.0], "target_point": [100.0, 400.0],
             "waypoints": []},
        ],
    }


def _canvas_from(validated, *, layout_applied=True):
    """Холст, собранный из этого графа и помеченный (как это делает сборщик)."""
    canvas = deepcopy(validated)
    canvas_state.stamp(canvas, validated, layout_applied=layout_applied)
    return canvas


# ───────────── главный случай: OCR и привязка не устаревят холст ─────────────

def test_ocr_and_binding_do_not_stale_the_canvas():
    validated = _validated()
    canvas = _canvas_from(validated)

    # OCR слил text_blocks в граф, потом вкладка привязки перезалила его
    # целиком с ПЕРЕНУМЕРАЦИЕЙ id блоков и своими bindings
    validated["text_blocks"] = [
        {"id": "block_1", "bbox": [10.0, 10.0, 60.0, 24.0], "text": "V-1",
         "merged_into": None},
    ]
    validated["bindings"] = [{"block_id": "block_1", "target": "node_1"}]
    assert canvas_state.is_stale(canvas, validated)[0] is False

    validated["text_blocks"][0]["id"] = "block_7"
    validated["bindings"] = [{"block_id": "block_7", "target": "node_1"}]
    stale, reason = canvas_state.is_stale(canvas, validated)
    assert stale is False, f"привязка не должна устаревлять геометрию: {reason}"


# ───────────── геометрия: правки обязаны устаревать холст ─────────────

def test_segmentation_change_stales():
    validated = _validated()
    canvas = _canvas_from(validated)
    validated["nodes"][0]["segmentation"][0] = 181.0
    assert canvas_state.is_stale(canvas, validated)[0] is True


def test_source_point_change_stales():
    """Точка подключения — реальный вход: по ней свопится W/H словарного бокса."""
    validated = _validated()
    canvas = _canvas_from(validated)
    validated["links"][0]["source_point"] = [101.0, 220.0]
    assert canvas_state.is_stale(canvas, validated)[0] is True


def test_centroid_and_bbox_changes_stale():
    for field, value in (("centroid", [111.0, 200.0]),
                         ("bbox", [180.0, 90.0, 221.0, 110.0])):
        validated = _validated()
        canvas = _canvas_from(validated)
        validated["nodes"][0][field] = value
        assert canvas_state.is_stale(canvas, validated)[0] is True, field


def test_node_removed_stales():
    validated = _validated()
    canvas = _canvas_from(validated)
    validated["nodes"].pop()
    assert canvas_state.is_stale(canvas, validated)[0] is True


# ───────────── версия раскладки ─────────────

def test_layout_version_change_stales(monkeypatch):
    validated = _validated()
    canvas = _canvas_from(validated)
    assert canvas_state.is_stale(canvas, validated)[0] is False

    monkeypatch.setattr(canvas_state, "LAYOUT_CODE_VERSION", "2")
    stale, reason = canvas_state.is_stale(canvas, validated)
    assert stale is True and "версия" in reason


def test_fixed_sizes_change_stales(monkeypatch):
    """Правка таблицы габаритов обязана устаревать холст сама.

    Она меняет бокс у 91 % скин-узлов, и результат со старой таблицей не
    воспроизводится — на такую правку нельзя полагаться на ручное поднятие
    версии кода.
    """
    validated = _validated()
    canvas = _canvas_from(validated)
    table = dict(canvas_state.FIXED_SIZES)
    table["armatura_ruchn"] = (42, 22)
    monkeypatch.setattr(canvas_state, "FIXED_SIZES", table)
    assert canvas_state.is_stale(canvas, validated)[0] is True


def test_canvas_without_stamp_is_stale():
    validated = _validated()
    canvas = deepcopy(validated)
    assert canvas_state.is_stale(canvas, validated)[0] is True
    canvas.setdefault("graph", {})["canvas_transform"] = {"s": 0.5}
    assert canvas_state.is_stale(canvas, validated)[0] is True


# ───────────── фолбэк клиента отличим от раскладки ─────────────

def test_fallback_canvas_is_distinguishable_from_layout():
    validated = _validated()
    worker = _canvas_from(validated, layout_applied=True)
    client = _canvas_from(validated, layout_applied=False)

    assert canvas_state.has_layout(worker) is True
    assert canvas_state.has_layout(client) is False
    # и при этом фолбэк НЕ считается устаревшим — иначе его пересобирало бы по
    # кругу, теряя правки оператора при каждом открытии
    assert canvas_state.is_stale(client, validated)[0] is False


def test_operator_saved_is_off_until_server_marks_it():
    validated = _validated()
    canvas = _canvas_from(validated)
    assert canvas_state.read_state(canvas)["operator_saved"] is False
    assert canvas_state.mark_operator_saved(canvas) is True
    assert canvas_state.read_state(canvas)["operator_saved"] is True
    assert canvas_state.mark_operator_saved(canvas) is False   # уже стоял


# ───────────── два процесса считают одинаково ─────────────

def test_sha_survives_json_roundtrip(tmp_path):
    """Воркер считает в памяти, клиент — прочитав файл. Sha обязан совпасть."""
    validated = _validated()
    in_worker = canvas_state.graph_projection_sha(validated)

    path = tmp_path / "graph_validated.json"
    path.write_text(json.dumps(validated, ensure_ascii=False), encoding="utf-8")
    in_client = canvas_state.graph_projection_sha(
        json.loads(path.read_text(encoding="utf-8")))

    assert in_worker == in_client


def test_sha_ignores_list_order_and_int_float():
    """Канон не должен зависеть от порядка списков и от 1 против 1.0.

    Переписыватели графа не обязаны сохранять порядок узлов, а JSON-обход
    легко превращает целое в дробное. И то и другое меняло бы sha на ровном
    месте — то есть давало бы ложное «устарело».
    """
    a = _validated()
    b = deepcopy(a)
    b["nodes"].reverse()
    b["nodes"][-1]["centroid"] = [100, 200]      # были float
    assert (canvas_state.graph_projection_sha(a)
            == canvas_state.graph_projection_sha(b))


def test_module_imports_without_qt():
    """Воркеру он нужен, а PySide6 в worker-образе нет."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.split('.')[0] == 'PySide6':\n"
        "            return self\n"
        "    def load_module(self, name):\n"
        "        raise ImportError('PySide6 недоступен')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from modules.graph.core import canvas_state\n"
        "print(canvas_state.layout_version())\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(__import__("pathlib").Path(
                             __file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().startswith("1+fs")


# ───────────── текст живёт своим хешем ─────────────

def test_text_staleness_is_independent_of_geometry():
    validated = _validated()
    validated["text_blocks"] = [
        {"id": "block_1", "bbox": [10.0, 10.0, 60.0, 24.0], "text": "V-1",
         "merged_into": None},
    ]
    validated["bindings"] = []
    canvas = _canvas_from(validated)

    # текст ещё не импортирован
    assert canvas_state.text_is_stale(canvas, validated)[0] is True

    canvas_state.stamp(canvas, validated, layout_applied=True,
                       text_imported_sha=canvas_state.text_projection_sha(validated))
    assert canvas_state.text_is_stale(canvas, validated)[0] is False

    # текст поменяли -> текст устарел, а геометрия по-прежнему свежая
    validated["text_blocks"][0]["text"] = "V-2"
    assert canvas_state.text_is_stale(canvas, validated)[0] is True
    assert canvas_state.is_stale(canvas, validated)[0] is False


def test_binding_change_stales_text_only():
    validated = _validated()
    validated["text_blocks"] = [
        {"id": "block_1", "bbox": [10.0, 10.0, 60.0, 24.0], "text": "V-1",
         "merged_into": None},
    ]
    validated["bindings"] = []
    canvas = _canvas_from(validated)
    canvas_state.stamp(canvas, validated, layout_applied=True,
                       text_imported_sha=canvas_state.text_projection_sha(validated))

    validated["bindings"] = [{"block_id": "block_1", "target": "node_1"}]
    assert canvas_state.text_is_stale(canvas, validated)[0] is True
    assert canvas_state.is_stale(canvas, validated)[0] is False


@pytest.mark.parametrize("layout_applied", [True, False])
def test_stamp_writes_all_fields(layout_applied):
    validated = _validated()
    canvas = _canvas_from(validated, layout_applied=layout_applied)
    tr = canvas["graph"]["canvas_transform"]
    for key in ("source_sha", "layout_version", "layout_applied",
                "operator_saved", "text_imported_sha", "text_edited"):
        assert key in tr, key
    assert tr["layout_applied"] is layout_applied
