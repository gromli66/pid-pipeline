# -*- coding: utf-8 -*-
"""Стенд диффов «до/после оператора» (ДН4, `tools/pair_bench.py`, пункт 0.11).

Данные корпуса в git не лежат, поэтому в CI стенд не запускается — здесь его
арифметика проверяется на синтетических артефактах той же формы. Фиксируются
два класса утверждений:

1. **Что считается правкой.** Сдвиг бокса, переделанная рамка текста, сдвинутая
   точка перекрёстка — правки; шум округления и перенос всего листа целиком —
   нет. Ошибка здесь тихая: таблица останется правдоподобной, но соврёт.
2. **Вердикт `--check`.** Пустой замер и пустой эталон обязаны давать «судить
   нечем» (код 2), а не зелёный ноль — тот же класс дефекта, что блокер 0.3.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import pair_bench  # noqa: E402


# --------------------------------------------------------------------------
# Сборка синтетического storage
# --------------------------------------------------------------------------

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _yolo(rows) -> str:
    return "".join("%d %.6f %.6f %.6f %.6f\n" % r for r in rows)


def _graph(nodes, links):
    return {"nodes": nodes, "links": links}


def _node(nid, y, x, w=10.0, h=10.0):
    return {"id": nid, "type": "equipment", "centroid": [y, x],
            "bbox": [x - w / 2, y - h / 2, x + w / 2, y + h / 2]}


def _edge(eid, src, tgt, sp=(0.0, 0.0), tp=(1.0, 1.0), **extra):
    edge = {"id": eid, "source": src, "target": tgt,
            "source_point": list(sp), "target_point": list(tp)}
    edge.update(extra)
    return edge


def _points(items):
    return {"junctions": [{"x": x, "y": y} for x, y in items], "bridges": []}


@pytest.fixture()
def storage(tmp_path):
    """Одна диаграмма со всеми парами, кроме масок (те собираются отдельно)."""
    diagram = tmp_path / "aaaabbbb-0000-0000-0000-000000000000"
    _write(diagram / "detection" / "yolo_predicted.txt",
           _yolo([(1, 0.100000, 0.100000, 0.020000, 0.020000),     # не тронут
                  (2, 0.300000, 0.300000, 0.020000, 0.020000),     # сдвинут
                  (3, 0.500000, 0.500000, 0.020000, 0.020000),     # снят
                  (4, 0.700000, 0.700000, 0.020000, 0.020000)]))   # смена класса
    _write(diagram / "detection" / "yolo_validated.txt",
           _yolo([(1, 0.100002, 0.100001, 0.020000, 0.020000),     # шум округления
                  (2, 0.305000, 0.300000, 0.020000, 0.020000),
                  (5, 0.700000, 0.700000, 0.020000, 0.020000),
                  (9, 0.900000, 0.900000, 0.020000, 0.020000)]))   # дорисован
    _write(diagram / "junction" / "points.json",
           _points([(100, 100), (200, 200), (300, 300)]))
    _write(diagram / "junction" / "points_validated.json",
           _points([(100, 100), (203, 200), (400, 400)]))
    _write(diagram / "graph" / "graph.json",
           _graph([_node("n1", 100.0, 100.0)], []))
    _write(diagram / "graph" / "graph_validated.json",
           _graph([_node("n1", 140.0, 100.0), _node("n2", 500.0, 500.0)],
                  [_edge("e1", "n1", "n2")]))
    _write(diagram / "graph" / "graph_canvas.json", {
        "nodes": [_node("n1", 900.0, 900.0),          # раскладка увезла узел
                  _node("n2", 700.0, 700.0),
                  dict(_node("n3", 10.0, 10.0), manual=True)],
        "links": [_edge("e1", "n1", "n2"),
                  _edge("e9", "n1", "n3", manual=True)],
        "graph": {"canvas_transform": {"operator_saved": True}},
    })
    _write(diagram / "contours" / "contours_auto.json",
           {"nodes": [{"ann_id": 1}, {"ann_id": 2}, {"ann_id": 3}]})
    _write(diagram / "contours" / "contours_validated.json", {"nodes": [
        {"ann_id": 1, "status": "approved", "was_edited": False,
         "polygon_auto": [0, 0, 1, 1], "polygon_validated": [0, 0, 1, 1]},
        {"ann_id": 2, "status": "approved", "was_edited": True,
         "polygon_auto": [0, 0, 1, 1], "polygon_validated": [0, 0, 2, 2]},
    ]})
    _write(diagram / "ocr" / "ocr_result.json", {"target": [
        {"bbox": [0, 0, 100, 20], "text": "БЛОК-1"},        # принят как есть
        {"bbox": [0, 100, 100, 120], "text": "орифер"},     # рамку растянули
        {"bbox": [0, 200, 100, 220], "text": "мусор"},      # снят
    ]})
    _write(diagram / "ocr_binding" / "ocr_binding.json", {
        "version": 2,
        "bindings": [{"ocr_block_idx": 0, "node_id": "n1"}],
        "edited_blocks": [
            {"bbox": [0, 0, 100, 20], "text": "БЛОК-1"},
            {"bbox": [0, 95, 160, 125], "text": "калорифер 1Б"},
            {"bbox": [500, 500, 600, 520], "text": "дорисован"},
        ],
    })
    return tmp_path


def _row(storage_dir, stage):
    return pair_bench.collect(storage_dir)["rows"]["aaaabbbb"][stage]


# --------------------------------------------------------------------------
# Что считается правкой
# --------------------------------------------------------------------------

def test_detection_counts_edits_and_ignores_rounding_noise(storage):
    row = _row(storage, "detection")

    assert (row["before"], row["after"]) == (4, 4)
    assert (row["added"], row["removed"]) == (1, 1)
    assert row["moved"] == 1          # шумный бокс сюда НЕ попал
    assert row["retyped"] == 1


def test_junction_move_add_and_remove(storage):
    row = _row(storage, "junction")

    assert (row["added"], row["removed"], row["moved"]) == (1, 1, 1)
    assert row["origin_shift"] == 0


def test_whole_sheet_shift_is_reported_instead_of_fake_edits(tmp_path):
    """Перенос листа целиком — не правки оператора, а разные обрезки."""
    diagram = tmp_path / "shift0001-0000-0000-0000-000000000000"
    before = [(100, 100), (200, 200), (300, 300), (400, 400)]
    _write(diagram / "junction" / "points.json", _points(before))
    _write(diagram / "junction" / "points_validated.json",
           _points([(x + 26, y - 21) for x, y in before]))

    row = pair_bench.collect(tmp_path)["rows"]["shift000"]["junction"]

    assert row["origin_shift"] == 33          # hypot(26, 21)
    assert (row["added"], row["removed"]) == (4, 4)


def test_graph_pair_uses_dn2_classifier(storage):
    row = _row(storage, "graph")

    assert (row["node_moved"], row["node_added"], row["edge_added"]) == (1, 1, 1)
    assert (row["before_nodes"], row["after_nodes"]) == (1, 2)


def test_layout_counts_composition_not_coordinates(storage):
    """У раскладки координаты разные по построению — переезд узла не правка."""
    row = _row(storage, "layout")

    assert (row["node_added"], row["node_removed"]) == (1, 0)
    assert (row["edge_added"], row["edge_removed"]) == (1, 0)
    assert (row["manual_nodes"], row["manual_edges"]) == (1, 1)
    assert row["operator_saved"] == 1
    assert "node_moved" not in row


def test_contours_edited_is_read_from_the_validated_file(storage):
    row = _row(storage, "contours")

    assert (row["auto"], row["validated"]) == (3, 2)
    assert row["edited"] == 1
    assert row["not_reviewed"] == 1


def test_ocr_stretched_frame_is_an_edit_not_add_plus_remove(storage):
    row = _row(storage, "ocr")

    assert (row["before"], row["after"]) == (3, 3)
    assert row["box_edited"] == 1
    assert row["text_fixed"] == 1
    assert (row["added"], row["removed"]) == (1, 1)   # дорисованный и снятый
    assert row["bindings"] == 1


def test_mask_pair_compares_binary_masks_of_different_png_modes(tmp_path):
    """Воркер пишет маску в сером, редактор возвращает RGBA — это одна маска."""
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")

    diagram = tmp_path / "maskaaaa-0000-0000-0000-000000000000"
    seg = diagram / "segmentation"
    seg.mkdir(parents=True)
    before = np.zeros((10, 10), dtype=np.uint8)
    before[0:2, :] = 255                       # 20 пикселей трубы
    Image.fromarray(before).save(seg / "pipe_mask.png")

    after = np.zeros((10, 10, 4), dtype=np.uint8)
    after[..., 3] = 255
    after[1:4, :, :3] = 255                    # строку стёрли, две дорисовали
    Image.fromarray(after, mode="RGBA").save(seg / "pipe_mask_validated.png")

    row = pair_bench.collect(tmp_path)["rows"]["maskaaaa"]["segmentation"]

    assert (row["px_before"], row["px_after"]) == (20, 30)
    assert (row["px_added"], row["px_removed"]) == (20, 10)
    assert row["changed_ppm"] == 300_000       # 30 из 100 пикселей
    assert row["shape_mismatch"] == 0


def test_regenerated_input_marks_the_pair_as_out_of_order(storage):
    """«До» новее «после» — стадию перезапустили, правками это читать нельзя."""
    graph_dir = storage / "aaaabbbb-0000-0000-0000-000000000000" / "graph"
    later = os.path.getmtime(graph_dir / "graph_validated.json") + 600
    os.utime(graph_dir / "graph.json", (later, later))

    assert pair_bench.collect(storage)["stale"]["aaaabbbb"] == ["graph"]


# --------------------------------------------------------------------------
# Вердикт --check
# --------------------------------------------------------------------------

def _rows(added=0):
    return {"u1": {"detection": {"before": 10, "after": 10, "added": added,
                                 "removed": 0, "moved": 0, "retyped": 0}}}


def test_check_is_green_when_nothing_grew():
    code, lines = pair_bench.verdict(_rows(2), _rows(2))

    assert code == 0
    assert "рост правок: 0" in lines[-1]


def test_check_fails_when_operator_had_to_edit_more():
    code, lines = pair_bench.verdict(_rows(3), _rows(2))

    assert code == 1
    assert any("ХУЖЕ" in line and "added 2 -> 3" in line for line in lines)


def test_paying_the_debt_is_printed_but_does_not_fail():
    code, lines = pair_bench.verdict(_rows(1), _rows(2))

    assert code == 0
    assert any("лучше" in line for line in lines)


def test_disappeared_pair_is_a_failure_not_a_skip():
    code, lines = pair_bench.verdict({"u1": {}}, _rows(2))

    assert code == 1
    assert any("ПРОПАЛА" in line for line in lines)


def test_empty_measurement_cannot_be_green():
    """Чистый клон: данных корпуса нет — судить нечем, а не «всё хорошо»."""
    assert pair_bench.verdict({}, _rows(2))[0] == 2


def test_empty_baseline_cannot_be_green():
    assert pair_bench.verdict(_rows(2), {})[0] == 2


def test_no_common_uids_cannot_be_green():
    assert pair_bench.verdict({"other": {}}, _rows(2))[0] == 2


def test_unknown_uid_is_skipped_not_judged():
    rows = dict(_rows(2))
    rows["new_uid"] = {"detection": {"added": 999}}

    code, lines = pair_bench.verdict(rows, _rows(2))

    assert code == 0
    assert any("новое" in line and "new_uid" in line for line in lines)


def test_missing_diagram_is_reported_and_skipped():
    code, lines = pair_bench.verdict({"u1": {}, }, {"u1": {}, "u2": _rows()["u1"]})

    assert code == 0
    assert any("нет данных u2" in line for line in lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_report_and_json_dump(storage, tmp_path, capsys):
    out = tmp_path / "pairs.json"

    code = pair_bench.main(["--storage", str(storage), "--json", str(out)])

    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["aaaabbbb"]["graph"]
    assert "объём правок" in capsys.readouterr().out


def test_cli_refuses_to_judge_a_subset_of_pairs(storage):
    with pytest.raises(SystemExit):
        pair_bench.main(["--storage", str(storage), "--stage", "graph", "--check"])


def test_cli_does_not_freeze_an_empty_baseline(tmp_path, capsys):
    code = pair_bench.main(["--storage", str(tmp_path / "нет"), "--write-baseline"])

    assert code == 2
    assert "эталон не тронут" in capsys.readouterr().out
