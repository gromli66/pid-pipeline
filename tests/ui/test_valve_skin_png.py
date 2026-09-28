# -*- coding: utf-8 -*-
"""PNG-скин клапана в «Ручной правке» рисует талию там же, где САПФИР (блок 2, Р6).

Таблица талий `contact.SKIN_CONTACT_OFFSET` снята JavaFX-рендером на рабочем
размере; конец трубы сажается в талию по ней. PNG-скины редактора прежде были
скринами контрола 120×120, обрезанными по альфе, и при растяжении на рамку
рисовали талию в другом месте: regulator_electro на 6.3 px выше конца трубы,
klapan_obratn_seroprivod на 6.5 px ниже, regulator_ruchn на 3.5 px (замер
red-team блока 2). Теперь PNG десяти клапанов рабочего размера сняты тем же
рендером ровно по рамке (`tools/skin_waist_probe.py --png`), и редактор альфой
их не режет.

Нарисованная талия — вершина бабочки по заливке треугольников: строка (для V —
столбец), где зелёный треугольник ближе всего подходит к оси рамки. Сцена
рендерится ×8, шаг 0.125 px.

Координаты двойственны (CODING_GUIDE §6): centroid / *_point = [y, x];
bbox = [x1, y1, x2, y2].
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
np = pytest.importorskip("numpy")

from PySide6.QtCore import QRectF                        # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter       # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from modules.graph.core import contact                   # noqa: E402
from modules.graph.core.pretransform import FIXED_SIZES  # noqa: E402

IMG_W, IMG_H = 1920, 1080
K = 8
VALVES = sorted(c for c in FIXED_SIZES
                if contact.CLASS_NAME_TO_SKIN.get(c, ("",))[0] == "ValveControl")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _conn(nid, y, x):
    return {"id": nid, "type": "connector", "class_id": -1,
            "class_name": "connector", "centroid": [y, x], "bbox": None,
            "segmentation": None, "degree": 0}


@pytest.fixture
def make_editor(qapp, tmp_path):
    made = []

    def _make(nodes):
        from ui.editors.advanced_graph_editor import AdvancedGraphEditor

        img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        ip = tmp_path / f"raster{len(made)}.png"
        assert img.save(str(ip))
        g = {"directed": False, "multigraph": False,
             "graph": {"image_size": [IMG_H, IMG_W],
                       "canvas_transform": {"layout_applied": True}},
             "nodes": nodes, "links": [], "text_blocks": [], "bindings": []}
        gp = tmp_path / f"graph{len(made)}.json"
        gp.write_text(json.dumps(g), encoding="utf-8")
        ed = AdvancedGraphEditor()
        ed._canvas_mode = True
        assert ed.load_data(str(ip), str(gp))
        made.append(ed)
        return ed

    yield _make
    for ed in made:
        ed.set_mode("idle")
        ed.scene.clear()
        ed.setParent(None)
        ed.deleteLater()
    qapp.processEvents()


def _painted_waist(ed, nid, horizontal):
    """Нарисованная талия скина: координата поперёк трубы в px от края рамки."""
    bb = ed.nodes[nid]["bbox"]
    skin = ed._skin_items[nid]
    hidden = [it for it in ed.scene.items() if it not in skin and it.isVisible()]
    for it in hidden:
        it.setVisible(False)
    src = QRectF(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1])
    img = QImage(round(src.width() * K), round(src.height() * K),
                 QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    ed.scene.render(p, QRectF(0, 0, img.width(), img.height()), src)
    p.end()
    for it in hidden:
        it.setVisible(True)
    a = np.frombuffer(img.constBits(), np.uint8, count=img.sizeInBytes()).reshape(
        img.height(), img.bytesPerLine() // 4, 4)[:, :img.width()].astype(int)
    green = (a[..., 1] > 170) & (a[..., 2] < 170) & (a[..., 0] < 170)
    if not horizontal:
        green = green.T
    mid = green.shape[1] / 2
    found = []
    for right in (True, False):             # правый треугольник / левый
        dist = []
        for row in range(green.shape[0]):
            c = np.nonzero(green[row])[0]
            c = c[c >= mid] if right else c[c < mid]
            if len(c):
                dist.append((abs((c.min() if right else c.max() + 1) - mid), row))
        if dist:
            m = min(d for d, _ in dist)
            found.append((np.mean([r for d, r in dist if d <= m + 1]) + 0.5) / K)
    assert found, "на скине нет зелёной бабочки"
    return float(np.mean(found))


def test_valve_png_is_the_working_frame_and_not_cropped(make_editor):
    """PNG клапана = рабочая рамка ×10 в базе редактора (вертикаль: ширина —
    поперёк трубы), и редактор отдаёт его целиком, без обрезки по альфе.
    Прочие скины по-прежнему режутся по альфе."""
    ed = make_editor([])
    assert len(VALVES) == 10
    for cn in VALVES:
        w, h = FIXED_SIZES[cn]
        f = QImage(str(ed._skins_dir() / f"{cn}.png"))
        assert (f.width(), f.height()) == (round(h * 10), round(w * 10)), cn
        pm = ed._skin_pixmap_for(cn)
        assert (pm.width(), pm.height()) == (f.width(), f.height()), \
            f"{cn}: PNG клапана обрезан по альфе"
    nasos = QImage(str(ed._skins_dir() / "nasos.png"))
    pm = ed._skin_pixmap_for("nasos")
    assert pm.width() * pm.height() < nasos.width() * nasos.height(), \
        "поля скина насоса больше не режутся"


@pytest.mark.parametrize("cn", ["regulator_electro", "klapan_obratn_seroprivod",
                                "regulator_ruchn", "armatura_electro"])
def test_painted_waist_is_the_pipe_end_after_operator_draws_pipes(make_editor, cn):
    """Д1: оператор ставит клапан, доводит трубу вдоль оси (H), затем трубу
    сверху — второй клапан поворачивается (Р3, VERTICAL_REVERSE). В обоих
    нарисованная талия совпадает с концом трубы (≤ 0.35 px; было до 6.5)."""
    w, h = FIXED_SIZES[cn]
    waist_y = 300.0 + contact.SKIN_CONTACT_OFFSET[contact.CLASS_NAME_TO_SKIN[cn][1]][0] * h
    ed = make_editor([_conn("k", 150.0, 905.0), _conn("kh", waist_y, 300.0)])
    ed.set_show_skins(True)

    vh = ed.add_equipment_node(500.0, 300.0, 3, cn, width=w, height=h,
                               enter_resize=False)
    assert ed.nodes[vh]["centroid"][0] == pytest.approx(waist_y, abs=0.01)
    assert ed.add_edge(vh, "kh")
    e = ed.edges_data[-1]
    end = e["source_point"] if e["source"] == vh else e["target_point"]
    bb = ed.nodes[vh]["bbox"]
    assert ed._node_orientation(vh) == "HORIZONTAL"
    assert _painted_waist(ed, vh, True) == pytest.approx(end[0] - bb[1], abs=0.35)

    vv = ed.add_equipment_node(900.0, 300.0, 3, cn, width=w, height=h,
                               enter_resize=False)
    assert ed.add_edge(vv, "k")
    n = ed.nodes[vv]
    assert n["_axis"] == "V" and ed._node_orientation(vv) == "VERTICAL_REVERSE"
    e = ed.edges_data[-1]
    end = e["source_point"] if e["source"] == vv else e["target_point"]
    assert _painted_waist(ed, vv, False) == pytest.approx(
        end[1] - n["bbox"][0], abs=0.35)
