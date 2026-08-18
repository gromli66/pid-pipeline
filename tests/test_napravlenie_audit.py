# -*- coding: utf-8 -*-
"""Приёмка napravlenie (`tools/napravlenie_audit.py`, пункт ВН1б дороги).

Корпус в git не лежит, поэтому в CI стенд не запускается — здесь его логика
проверяется на синтетических артефактах той же формы. Фиксируются три класса
утверждений, каждый из которых уже соврал бы тихо:

1. **Степень считается по рёбрам, а не читается из поля.** Поле `degree` в
   графе после правки редактором протухает (`ui/editors/graph_data.py::save`
   его не пересчитывает) — на корпусе 2026-08-18 это 37 «сирот», у которых
   ребро есть. Стенд, поверивший полю, завысил бы дефект в полтора раза.
2. **Куча «восстановимо однозначно» против «оператору».** Граница — труба
   касается ОСЕВОЙ грани и скелет дотянулся; всё остальное уходит оператору
   (решение Максима: догаданная труба уезжает в FXML как настоящая топология).
3. **Вердикт `--check`.** Пустой корпус обязан давать «судить нечем» (код 2),
   а не зелёный ноль; попадание бокса в `node_mask` СВОЕЙ площадью — красный,
   а накрытие рамкой соседа — нет (иначе две трети боксов корпуса ложно красны).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import napravlenie_audit as audit  # noqa: E402


def _save_mask(path: Path, mask) -> None:
    """PNG пишем Pillow, а не cv2: имя `cv2` в наборе подменено заглушкой
    на уровне модуля (`tests/observability/*`, долг 0.3x) — под ней `imwrite`
    молча не пишет ничего. Тот же приём в `tests/test_pair_bench.py:195`."""
    Image = pytest.importorskip("PIL.Image")
    Image.fromarray(mask).save(path)


BOX = [100, 100, 140, 140]      # бокс стрелки: [x1, y1, x2, y2]
SHAPE = (300, 300)


# --------------------------------------------------------------------------
# Сборка синтетических артефактов
# --------------------------------------------------------------------------

def _mask(*rects):
    """Маска с горизонтальными/вертикальными полосами (x1, y1, x2, y2)."""
    mask = np.zeros(SHAPE, np.uint8)
    for x1, y1, x2, y2 in rects:
        mask[y1:y2, x1:x2] = 255
    return mask


def _pipe_left():
    """Труба подходит к левой грани бокса (и обрывается на ней)."""
    return _mask((0, 118, BOX[0] + 1, 122))


def _pipe_both():
    return _mask((0, 118, BOX[0] + 1, 122), (BOX[2] - 1, 118, 300, 122))


def _pipe_top():
    return _mask((118, 0, 122, BOX[1] + 1))


def _write_diagram(root: Path, uid: str, *, boxes=1, direction="right",
                   links=None, degree_field=0, node_mask=None,
                   pipe_mask=None, skeleton=None, editor=False,
                   neighbour=(200, 100, 40, 40)):
    """Диаграмма из одного бокса napravlenie (плюс соседний насос, [x, y, w, h])."""
    diagram = root / uid
    (diagram / "detection").mkdir(parents=True, exist_ok=True)
    (diagram / "segmentation").mkdir(parents=True, exist_ok=True)
    (diagram / "skeleton").mkdir(parents=True, exist_ok=True)
    (diagram / "graph").mkdir(parents=True, exist_ok=True)

    annotations = []
    for i in range(boxes):
        attrs = {"direction": direction, "direction_confidence": 0.9} if direction else {}
        annotations.append({"id": i, "category_id": 41, "attributes": attrs,
                            "bbox": [BOX[0], BOX[1], BOX[2] - BOX[0], BOX[3] - BOX[1]]})
    annotations.append({"id": 90, "category_id": 5, "attributes": {},
                        "bbox": list(neighbour)})                    # сосед-насос
    (diagram / "detection" / "coco_validated.json").write_text(json.dumps({
        "categories": [{"id": 41, "name": "napravlenie"}, {"id": 5, "name": "nasos"}],
        "annotations": annotations,
    }, ensure_ascii=False), encoding="utf-8")

    graph = {
        "graph": {"image_size": list(SHAPE), **({"_editor_commit": "abc1234"} if editor else {})},
        "nodes": [{"id": "node_1", "type": "equipment", "class_id": 40,
                   "class_name": "napravlenie", "direction_node": True,
                   "flow_axis": "h" if direction in ("left", "right") else "v",
                   "flow_direction": direction, "degree": degree_field,
                   "bbox": BOX, "centroid": [120, 120]},
                  {"id": "node_2", "type": "equipment", "class_id": 5,
                   "degree": 1, "bbox": [200, 100, 240, 140], "centroid": [120, 220]}],
        "links": links or [],
    }
    (diagram / "graph" / "graph.json").write_text(json.dumps(graph, ensure_ascii=False),
                                                  encoding="utf-8")
    _save_mask(diagram / "segmentation" / "pipe_mask.png",
               _pipe_left() if pipe_mask is None else pipe_mask)
    _save_mask(diagram / "segmentation" / "node_mask.png",
               np.zeros(SHAPE, np.uint8) if node_mask is None else node_mask)
    _save_mask(diagram / "skeleton" / "skeleton_final.png",
               _pipe_left() if skeleton is None else skeleton)
    return diagram


@pytest.fixture()
def storage(tmp_path):
    return tmp_path / "diagrams"


# --------------------------------------------------------------------------
# 1. Степень — по рёбрам, а не по полю
# --------------------------------------------------------------------------

def test_incidence_beats_stale_degree_field(storage):
    """Поле degree=0 при живом ребре — не сирота (граф после редактора)."""
    _write_diagram(storage, "aaaa0001", degree_field=0, editor=True,
                   links=[{"id": "edge_0", "source": "node_1", "target": "node_2"}])
    row = audit.audit_diagram(storage / "aaaa0001")
    assert row["degrees"] == {1: 1}
    assert row["orphans"] == []
    assert row["stale_degree_nodes"] == 1        # поле соврало ровно про один узел
    assert row["editor_saved"] is True


def test_orphan_is_counted_when_no_links(storage):
    """Поле degree=1 при отсутствии рёбер — сирота (обратная ошибка поля)."""
    _write_diagram(storage, "aaaa0002", degree_field=1, links=[])
    row = audit.audit_diagram(storage / "aaaa0002")
    assert row["degrees"] == {0: 1}
    assert [o["node_id"] for o in row["orphans"]] == ["node_1"]
    assert row["orphans"][0]["degree_field"] == 1


# --------------------------------------------------------------------------
# 2. Деление сирот на две кучи
# --------------------------------------------------------------------------

def test_bucket_one_axial_side(storage):
    _write_diagram(storage, "aaaa0003")
    row = audit.audit_diagram(storage / "aaaa0003")
    assert row["orphans"][0]["bucket"] == audit.BUCKET_AXIS_ONE
    assert row["orphans"][0]["axial_sides"] == ["L"]


def test_bucket_through_pipe(storage):
    _write_diagram(storage, "aaaa0004", pipe_mask=_pipe_both(), skeleton=_pipe_both())
    assert audit.audit_diagram(storage / "aaaa0004")["orphans"][0]["bucket"] \
        == audit.BUCKET_AXIS_BOTH


def test_bucket_no_pipe_at_all(storage):
    empty = np.zeros(SHAPE, np.uint8)
    _write_diagram(storage, "aaaa0005", pipe_mask=empty, skeleton=empty)
    assert audit.audit_diagram(storage / "aaaa0005")["orphans"][0]["bucket"] \
        == audit.BUCKET_NO_PIPE


def test_bucket_perpendicular_only(storage):
    """Труба у глухой грани: цеплять её бокс не имеет права (NAPRAVLENIE §3.4)."""
    _write_diagram(storage, "aaaa0006", direction="right",
                   pipe_mask=_pipe_top(), skeleton=_pipe_top())
    assert audit.audit_diagram(storage / "aaaa0006")["orphans"][0]["bucket"] \
        == audit.BUCKET_PERP_ONLY


def test_bucket_skeleton_did_not_reach(storage):
    """Труба у грани есть, скелет обрывается за порогом — гадать не будем."""
    far = _mask((0, 118, BOX[0] - audit.SKELETON_REACH_PX - 5, 122))
    _write_diagram(storage, "aaaa0007", skeleton=far)
    assert audit.audit_diagram(storage / "aaaa0007")["orphans"][0]["bucket"] \
        == audit.BUCKET_SKELETON


def test_vertical_axis_uses_top_bottom(storage):
    """Ось берётся из flow_direction: у стрелки «вверх» осевые грани — T/B."""
    _write_diagram(storage, "aaaa0008", direction="up",
                   pipe_mask=_pipe_top(), skeleton=_pipe_top())
    orphan = audit.audit_diagram(storage / "aaaa0008")["orphans"][0]
    assert orphan["bucket"] == audit.BUCKET_AXIS_ONE
    assert orphan["axial_sides"] == ["T"]


# --------------------------------------------------------------------------
# 3. node_mask: своя площадь против накрытия соседом
# --------------------------------------------------------------------------

def test_neighbour_box_is_not_a_violation(storage):
    """Рамка соседнего насоса накрывает стрелку — это не «бокс стал узлом»."""
    covered = _mask((BOX[0] - 5, BOX[1] - 5, BOX[0] + 45, BOX[1] + 45))
    _write_diagram(storage, "aaaa0009", node_mask=covered,
                   neighbour=(BOX[0] - 5, BOX[1] - 5, 50, 50))
    row = audit.audit_diagram(storage / "aaaa0009")
    assert row["node_mask_overlap"] == 1     # заливка есть
    assert row["node_mask_hits"] == 0        # но она чужая
    assert audit.verdict([row])[0] == 0


def test_own_area_in_node_mask_is_a_violation(storage):
    """Залит кусок бокса, которого сосед не накрывает — старый дефект ВН1."""
    painted = _mask((BOX[0] + 30, BOX[1] + 30, BOX[2], BOX[3]))
    _write_diagram(storage, "aaaa0010", node_mask=painted)
    row = audit.audit_diagram(storage / "aaaa0010")
    assert row["node_mask_hits"] == 1
    code, lines = audit.verdict([row])
    assert code == 1
    assert any("node_mask" in line for line in lines)


# --------------------------------------------------------------------------
# 4. Вердикт --check
# --------------------------------------------------------------------------

def test_missing_direction_fails_check(storage):
    _write_diagram(storage, "aaaa0011", direction=None)
    code, lines = audit.verdict([audit.audit_diagram(storage / "aaaa0011")])
    assert code == 1
    assert any("направление проставлено 0 из 1" in line for line in lines)


def test_clean_run_passes_check(storage, capsys):
    _write_diagram(storage, "aaaa0012", degree_field=1,
                   links=[{"id": "edge_0", "source": "node_1", "target": "node_2"}])
    assert audit.main(["--storage", str(storage), "--check"]) == 0
    assert "нарушений критериев приёмки: 0" in capsys.readouterr().out


def test_empty_storage_is_not_a_green_check(storage, capsys):
    storage.mkdir(parents=True)
    assert audit.main(["--storage", str(storage), "--check"]) == 2
    assert "судить нечем" in capsys.readouterr().out


def test_report_counts_restorable_and_operator(storage, capsys):
    _write_diagram(storage, "aaaa0013")                                  # восстановимо
    empty = np.zeros(SHAPE, np.uint8)
    _write_diagram(storage, "aaaa0014", pipe_mask=empty, skeleton=empty)  # оператору
    assert audit.main(["--storage", str(storage), "--orphans"]) == 0
    out = capsys.readouterr().out
    assert "сирот (нет ни одного ребра): 2" in out
    assert "восстановимо однозначно 1, оператору 1" in out
