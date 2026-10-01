# -*- coding: utf-8 -*-
"""Колонки блока 6 в `smooth_bench` (шаг 2 проекта блока 6).

`overlap` (наслоений узлов) и `offaxis` (неосевых входов клапана) — ДЕФЕКТЫ:
судятся `--check` и попадают в эталон входа. Значения берёт стенд из
`edit_smooth.score` (через `.get`: эталон входа снимался ДО правок движка, и
там их не было; с шага 4 движок их считает). Ход «осевая талия» считается
в `moves` вместе с прочими. Qt и libavoid не нужны.
"""
from __future__ import annotations

from modules.graph.core import edit_smooth as es
from tools import smooth_bench as sb


def _graph() -> dict:
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [1080, 1920]},
            "nodes": [{"id": "a", "type": "connector", "class_name": "connector",
                       "centroid": [100.0, 100.0], "bbox": None},
                      {"id": "b", "type": "connector", "class_name": "connector",
                       "centroid": [100.0, 300.0], "bbox": None}],
            "links": [{"id": "e", "source": "a", "target": "b",
                       "source_point": [100.0, 100.0],
                       "target_point": [100.0, 300.0], "waypoints": []}],
            "text_blocks": [], "bindings": []}


def test_new_columns_are_defects_and_remedy_is_counted():
    cols = dict(sb.DEFECT_COLS)
    assert cols["overlap"] == "наслоений узлов"
    assert cols["offaxis"] == "неосевых входов клапана"
    assert "осевая талия" in sb.REMEDIES


def test_new_columns_come_from_engine_score(monkeypatch):
    m = sb.canvas_metrics(_graph())
    assert (m["overlap"], m["offaxis"]) == (0, 0)     # рамок и клапанов нет
    real = es.score
    monkeypatch.setattr(es, "score",
                        lambda g: dict(real(g), overlap=3, offaxis=2))
    m = sb.canvas_metrics(_graph())
    assert (m["overlap"], m["offaxis"]) == (3, 2)


def test_new_columns_reach_the_row_and_moves():
    m = sb.canvas_metrics(_graph())
    stats = {k: 0 for k in sb.REMEDIES}
    stats.update({"осевая талия": 2, "узел": 1, "отклонено": 0, "раунды": 1})
    row = {"after": dict(m, overlap=1), "stats": stats, "ms": 1.0,
           "rollback_bad": 0, "reseat_calls": 0, "reseat_blind": 0}
    cols = sb._columns(row)
    assert (cols["overlap"], cols["offaxis"]) == (1, 0)
    assert cols["moves"] == 3


def test_hooks_column_is_reference_not_defect(monkeypatch):
    """`hooks` (крючков, решение Максима 01.10) — справочная колонка: из
    `edit_smooth.score` в строку прогона, но не дефект — `--check` её рост
    не судит."""
    assert "hooks" in dict(sb.REF_COLS)
    assert "hooks" not in dict(sb.DEFECT_COLS)
    real = es.score
    monkeypatch.setattr(es, "score", lambda g: dict(real(g), hooks=4))
    m = sb.canvas_metrics(_graph())
    assert m["hooks"] == 4
    stats = {k: 0 for k in sb.REMEDIES}
    stats.update({"отклонено": 0, "раунды": 1})
    row = {"after": m, "stats": stats, "ms": 1.0, "rollback_bad": 0,
           "reseat_calls": 0, "reseat_blind": 0}
    cols = sb._columns(row)
    assert cols["hooks"] == 4
    one = {k: {"med": v, "lo": v, "hi": v} for k, v in cols.items()}
    grown = dict(one, hooks={"med": 9, "lo": 9, "hi": 9})
    assert sb.growth({"x": grown}, {"x": one}, {}) == {}
    worse = dict(one, steps={"med": cols["steps"] + 1, "lo": 0, "hi": 0})
    assert sb.growth({"x": worse}, {"x": one}, {}) \
        == {"x": [("steps", cols["steps"], cols["steps"] + 1)]}


def _jog_canvas(y: float) -> dict:
    """Бокс и коннектор, прямая из грани R бокса на высоте y (середина грани —
    501.985; red-team блока 6, e21): на середине канон открытия её не трогает,
    в 6.1 px от середины (> MID_SNAP) сажает конец в середину — косая."""
    from modules.graph.core import contact
    box = {"id": "A", "type": "equipment", "class_id": 99,
           "class_name": "unknow", "centroid": [501.99, 853.64],
           "bbox": [821.7, 483.86, 885.89, 520.11], "segmentation": None}
    c = {"id": "c", "type": "connector", "class_id": -1,
         "class_name": "connector", "centroid": [y, 977.79], "bbox": None,
         "segmentation": None}
    e = {"id": "e", "source": "A", "target": "c", "source_point": [y, 885.89],
         "target_point": [y, 977.79], "waypoints": []}
    return {"graph": {"image_size": [1080, 1920], "canvas_transform": {
        "attach_model": contact.ATTACH_MODEL}},
        "nodes": [box, c], "links": [e]}


def test_reopen_column_counts_only_moves_made_by_the_button(tmp_path):
    """`reopen` (гейт блока 6, §142.2): канон открытия вкладки над холстом,
    сохранённым после кнопки. Кнопка увела прямую с середины грани — открытие
    её пересаживает: 1. Вход уже такой и кнопка его не трогала — сдвиг
    открытия не дело кнопки: 0. Канон без витрин: виджеты не создаются."""
    import json
    assert dict(sb.DEFECT_COLS)["reopen"] == "сдвигов повторного открытия"
    mid, off = json.dumps(_jog_canvas(501.99)), json.dumps(_jog_canvas(495.89))
    assert sb.reopen_moves(mid, off, tmp_path) == 1
    assert sb.reopen_moves(off, off, tmp_path) == 0
    assert sb.reopen_moves(mid, mid, tmp_path) == 0
    stats = {k: 0 for k in sb.REMEDIES}
    stats.update({"отклонено": 0, "раунды": 1})
    row = {"after": sb.canvas_metrics(_graph()), "stats": stats, "ms": 1.0,
           "rollback_bad": 0, "reseat_calls": 0, "reseat_blind": 0,
           "reopen": 3}
    assert sb._columns(row)["reopen"] == 3
