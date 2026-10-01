# -*- coding: utf-8 -*-
"""Кнопка «Выровнять» -> сохранить -> открыть ещё раз (гейт блока 6, §142.2).

Р-б (решение Максима 01.10): открытие вкладки пересаживает концы своим
каноном (`base_graph_tab._reseat_canvas_endpoints`), и ход кнопки, который
гейт принял, открытие переделывало — стаб вдоль грани входа уходил в угол или
на соседнюю грань, прямая вне MID_SNAP от середины грани — в косую. Теперь на
холсте с меткой модели проба, прошедшая гейт, судится после того же канона и
принимается такой (`edit_smooth._Canon`): «сохранить -> открыть» — неподвижная
точка для всего, что сделала кнопка. Старый холст без метки — как 238fe6b,
канона в кнопке нет (ADR-009).

Р-а: ход не подводит формы концов трубы ближе пола раскладки (6 px) — только
на холсте с меткой модели; старый холст — как до ddd84ec (решение Максима 01.10).

Сценарии — живой редактор в режиме холста, чужое действие оператора
(протяжка узла) перед кнопкой. Сдвиги открытия считает тот же счёт, что
колонка `reopen` стенда (`tools/smooth_bench.reopen_moves`): концы, которые
канон двигает в сохранённом холсте, кроме тех, что он двигает так же и без
кнопки. Координаты двойственны (CODING_GUIDE §6): centroid/точки = [y, x].
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

from pathlib import Path                            # noqa: E402

from modules.graph.core import contact               # noqa: E402
from modules.graph.core import edit_smooth as es     # noqa: E402
from tools import corpus                             # noqa: E402
from tools import smooth_bench as sb                 # noqa: E402

RAW = (Path(__file__).resolve().parents[2] / "tools" / "bench"
       / "smooth_corpus" / "raw")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _editor(tmp_path, graph):
    from ui.editors.advanced_graph_editor import AdvancedGraphEditor
    img = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    ip = tmp_path / "raster.png"
    assert img.save(str(ip))
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    ed = AdvancedGraphEditor()
    ed._canvas_mode = True
    assert ed.load_data(str(ip), str(gp))
    return ed


def _drag(ed, nid, dx, dy):
    """Чужое действие оператора: протяжка узла на (dx, dy)."""
    y, x = ed.nodes[nid]["centroid"]
    ed.set_mode("drag_node")
    ed.start_drag_node(nid)
    ed.drag_node_to(x + dx, y + dy)
    ed.end_drag_node()
    ed.set_mode("idle")


def _button_save(ed, tmp_path):
    """-> (холст до кнопки, сохранённый после кнопки) — тексты json."""
    pre = json.dumps(ed.model.graph_data, ensure_ascii=False)
    ed.smooth_canvas()
    p = tmp_path / "saved.json"
    assert ed.save_graph(str(p))
    return pre, p.read_text(encoding="utf-8")


def _089(model):
    g = json.loads((RAW / "089feca2_canvas.json").read_text(encoding="utf-8"))
    return corpus.as_model(g) if model else g


def test_button_save_reopen_is_a_fixed_point_after_operator_drag(
        qapp, tmp_path, monkeypatch):
    """089feca2@model (корпус стенда): оператор протягивает node_30 на 10 px
    вправо, жмёт кнопку, сохраняет — открытие не сдвигает ни одного конца,
    который тронула кнопка. Без канона в судье того же хода (`_Canon.settle`
    выключен — кнопка до правки) открытие сдвигает концы: фикстура ловит
    ловушку, а не проходит мимо неё."""
    def run(tag):
        d = tmp_path / tag
        d.mkdir()
        ed = _editor(d, _089(True))
        _drag(ed, "node_30", 10.0, 0.0)
        pre, saved = _button_save(ed, d)
        return sb.reopen_moves(pre, saved, d)

    assert run("canon") == 0
    monkeypatch.setattr(es._Canon, "settle", lambda self, graph, base: None)
    assert run("bare") > 0, "фикстура: без канона открытие обязано двигать"


def test_reopen_after_button_again_moves_nothing(qapp, tmp_path):
    """Тот же лист без протяжки: после кнопки и сохранения канон открытия —
    тождество на всех концах, которые он не двигает и во входе кнопки, а
    второе открытие (над уже открытым файлом) не двигает ничего."""
    from ui.tabs.base_graph_tab import _reseat_canvas_endpoints
    ed = _editor(tmp_path, _089(True))
    pre, saved = _button_save(ed, tmp_path)
    assert sb.reopen_moves(pre, saved, tmp_path) == 0
    p = tmp_path / "opened.json"
    p.write_text(saved, encoding="utf-8")
    _reseat_canvas_endpoints(p)
    once = p.read_text(encoding="utf-8")
    assert _reseat_canvas_endpoints(p) is False
    assert p.read_text(encoding="utf-8") == once


@pytest.mark.parametrize("model", [True, False], ids=["модель", "старый"])
def test_canon_reaches_the_engine_only_on_model_canvas(qapp, tmp_path,
                                                       monkeypatch, model):
    """Канон открытия редактор отдаёт движку только на холсте с меткой
    модели (`_waist_model`); старый холст — кнопка как в 238fe6b, без
    канона (ADR-009)."""
    seen = {}
    real = es.smooth

    def spy(graph, **kw):
        seen["canon_fn"] = kw.get("canon_fn")
        return real(graph, **kw)

    monkeypatch.setattr(es, "smooth", spy)
    ed = _editor(tmp_path, _089(model))
    assert (contact.canvas_model(ed.model.graph_data) is not None) is model
    ed.smooth_canvas()
    from ui.tabs.base_graph_tab import reseat_canvas
    assert seen["canon_fn"] is (reseat_canvas if model else None)


def _gap_canvas(dx, model=True):
    """Ступенька 10 px между коннекторами, у каждого — бокс по диагонали в
    dx px (`tests/test_edit_smooth.py::_canvas_gap`), холст с меткой модели
    (model=False — старый холст без метки)."""
    def conn(nid, x, y):
        return {"id": nid, "type": "connector", "class_id": -1,
                "class_name": "connector", "centroid": [y, x], "bbox": None,
                "segmentation": None}

    def box(nid, cx, cy):
        return {"id": nid, "type": "equipment", "class_id": 99,
                "class_name": "unknow", "centroid": [cy, cx],
                "bbox": [cx - 20.0, cy - 20.0, cx + 20.0, cy + 20.0],
                "segmentation": None}

    def edge(eid, s, t, pts):
        yx = [[p[1], p[0]] for p in pts]
        return {"id": eid, "source": s, "target": t, "source_point": yx[0],
                "target_point": yx[-1], "waypoints": yx[1:-1]}

    nodes = [conn("c1", 100.0, 140.0), conn("c2", 400.0, 150.0),
             box("b1", 80.0 - dx, 175.0), box("b2", 420.0 + dx, 116.0)]
    links = [edge("e1", "c1", "c2", [(100.0, 140.0), (200.0, 140.0),
                                    (200.0, 150.0), (400.0, 150.0)]),
             edge("e2", "c1", "b1", [(100.0, 140.0), (100.0, 175.0),
                                    (100.0 - dx, 175.0)]),
             edge("e3", "c2", "b2", [(400.0, 150.0), (400.0, 116.0),
                                    (400.0 + dx, 116.0)])]
    meta = {"image_size": [1080, 1920]}
    if model:
        meta["canvas_transform"] = {"attach_model": contact.ATTACH_MODEL}
    return {"directed": False, "multigraph": False, "graph": meta,
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def test_button_does_not_bring_forms_below_floor(qapp, tmp_path):
    """Р-а в живом редакторе: ступеньку закрывает только сдвиг коннектора к
    боксу на hypot(3, 5) = 5.83 < 6 px — кнопка его не делает, зазоров форм
    меньше пола после кнопки 0, как до неё; сохранить -> открыть не двигает
    ничего из сделанного кнопкой. В 9 px от бокса тот же ход проходит."""
    ed = _editor(tmp_path, _gap_canvas(3.0))
    assert es.count_gaps(ed.model.graph_data) == 0
    pre, saved = _button_save(ed, tmp_path)
    after = json.loads(saved)
    assert es.count_gaps(after) == 0
    assert {n["id"]: n["centroid"] for n in after["nodes"]} == \
        {n["id"]: n["centroid"] for n in json.loads(pre)["nodes"]}
    assert sb.reopen_moves(pre, saved, tmp_path) == 0

    d = tmp_path / "free"
    d.mkdir()
    ed = _editor(d, _gap_canvas(9.0))
    pre, saved = _button_save(ed, d)
    after = json.loads(saved)
    assert es.count_steps(after) == 0 and es.count_gaps(after) == 0


@pytest.mark.parametrize("model", [True, False], ids=["модель", "старый"])
def test_gap_floor_follows_the_canvas_mark(qapp, tmp_path, model):
    """Р-а — только на холсте с меткой модели (решение Максима 01.10).
    Перед кнопкой оператор протягивает бокс и отменяет протяжку (Ctrl+Z).
    Холст модели: ступеньку закрывает только сдвиг коннектора к боксу на
    5.83 < 6 px — кнопка его не делает. Старый холст без метки — кнопка
    как до ddd84ec: ступенька снята этим сдвигом, зазор форм меньше пола
    появился."""
    ed = _editor(tmp_path, _gap_canvas(3.0, model=model))
    assert ed._waist_model() is model
    _drag(ed, "b2", 30.0, 0.0)
    ed.undo_mgr.undo()
    pre, saved = _button_save(ed, tmp_path)
    after = json.loads(saved)
    assert es.count_steps(after) == (1 if model else 0)
    assert es.count_gaps(after) == (0 if model else 1)
