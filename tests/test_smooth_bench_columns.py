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
