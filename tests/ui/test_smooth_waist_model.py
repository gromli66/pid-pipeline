# -*- coding: utf-8 -*-
"""Блок 6, «Авто-выравнивание» (Р4), связка в редакторе: подписи едут за
сдвинутым узлом, шаг отмены — один, строка статуса называет осевые ходы.

Решение заказчика §8.3: привязанная подпись — якорь узла и едет за ним на
ту же дельту, непривязанная стоит (как у протяжки, `_shift_bound_blocks`).
Движок `edit_smooth` двигает узлы на месте и про подписи не знает, поэтому
дельту центроидов до/после кнопки отдаёт подписям `smooth_canvas`.

Инварианты — про ДАННЫЕ (класс «UI-жесты»): ноль утверждений про режим
редактора. Координаты двойственны (CODING_GUIDE §6): centroid = [y, x],
bbox = [x1, y1, x2, y2].
"""
import copy
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 800, 600


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _graph():
    """Ступенька 10 px между двумя рамками — её закрывает сдвиг узла
    (коннектора нет, рамочный конец не скользит). У каждой рамки
    привязанная подпись (привязка без side — следование даёт только сам
    сдвиг), плюс непривязанная."""
    nodes = [
        {"id": "a", "type": "equipment", "centroid": [140.0, 100.0],
         "bbox": [85.0, 125.0, 115.0, 155.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
        {"id": "b", "type": "equipment", "centroid": [150.0, 620.0],
         "bbox": [600.0, 130.0, 640.0, 170.0], "segmentation": None,
         "class_id": 99, "class_name": "unknow", "degree": 1},
    ]
    links = [{"id": "e1", "source": "a", "target": "b",
              "source_point": [140.0, 115.0], "target_point": [150.0, 600.0],
              "waypoints": [[140.0, 200.0], [150.0, 200.0]]}]
    blocks = [
        {"id": "tb_a", "bbox": [80.0, 100.0, 120.0, 118.0], "text": "A1",
         "source": "manual"},
        {"id": "tb_b", "bbox": [600.0, 176.0, 640.0, 194.0], "text": "B1",
         "source": "manual"},
        {"id": "tb_free", "bbox": [300.0, 400.0, 340.0, 418.0],
         "text": "прочее", "source": "manual"},
    ]
    bindings = [{"block_id": "tb_a", "node_id": "a", "kind": "node",
                 "text": "A1"},
                {"block_id": "tb_b", "node_id": "b", "kind": "node",
                 "text": "B1"}]
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": blocks,
            "bindings": bindings}


@pytest.fixture
def ed(qapp, tmp_path):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_graph()), encoding="utf-8")
    editor = AdvancedGraphEditor()
    editor._canvas_mode = True
    assert editor.load_data(str(ip), str(gp))
    return editor


def _state(ed):
    """Холст как ДАННЫЕ: узлы, рёбра, текст-блоки."""
    g = ed.model.graph_data
    return copy.deepcopy((g.get("nodes"), g.get("links"),
                          [ed.model.find_text_block(b)["bbox"]
                           for b in ("tb_a", "tb_b", "tb_free")]))


def test_smooth_bound_labels_ride_with_shifted_node(ed):
    """Сдвиг узла кнопкой — его привязанная подпись едет на ту же дельту,
    подпись несдвинутого узла и непривязанная стоят; один Ctrl+Z
    возвращает всё побайтово, Ctrl+Y — состояние после кнопки."""
    c0 = {n["id"]: list(n["centroid"]) for n in ed.model.graph_data["nodes"]}
    tb0 = {b: list(ed.model.find_text_block(b)["bbox"])
           for b in ("tb_a", "tb_b", "tb_free")}
    s0 = _state(ed)
    msgs = []
    ed.status_callback = msgs.append

    st = ed.smooth_canvas()

    assert st["узел"] == 1, st
    moved = 0
    for nid, tb in (("a", "tb_a"), ("b", "tb_b")):
        c1 = ed.model.graph_data["nodes"][0 if nid == "a" else 1]["centroid"]
        dy, dx = c1[0] - c0[nid][0], c1[1] - c0[nid][1]
        moved += bool(dx or dy)
        want = [tb0[tb][0] + dx, tb0[tb][1] + dy,
                tb0[tb][2] + dx, tb0[tb][3] + dy]
        assert ed.model.find_text_block(tb)["bbox"] == \
            pytest.approx(want, abs=0.01), (nid, dx, dy)
    assert moved == 1, "фикстура: кнопка обязана сдвинуть ровно один узел"
    assert ed.model.find_text_block("tb_free")["bbox"] == tb0["tb_free"]
    assert any("осевых 0" in m for m in msgs), msgs

    s1 = _state(ed)
    assert ed.undo_mgr.undo() == "Сглаживание"
    assert _state(ed) == s0, "один Ctrl+Z обязан вернуть узлы и подписи"
    ed.undo_mgr.redo()
    assert _state(ed) == s1, "Ctrl+Y обязан вернуть состояние после кнопки"
