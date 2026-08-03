# -*- coding: utf-8 -*-
"""Целостность undo-стека: restore обязан отдавать модели ПРИВАТНЫЕ копии.

Репро аудита 2026-08-03: SnapshotCommand.undo/redo передают в
GraphDataModel.restore кортежи, живущие в undo-стеке (_before/_after).
Если restore присваивает их модели по ссылке, следующий жест мутирует
историю задним числом: undo x2 -> redo возвращает итог ЖЕСТА 2 вместо
итога жеста 1, а любая внекомандная мутация после undo портит запись стека.
Qt не нужен: GraphDataModel и UndoManager — чистый Python.
"""
import json

from ui.editors.graph_data import GraphDataModel
from ui.editors.undo_manager import SnapshotCommand, UndoManager


def _model(tmp_path):
    graph = {
        "directed": False, "multigraph": False,
        "graph": {"image_size": [600, 800]},
        "nodes": [{"id": "n1", "type": "connector", "centroid": [1.0, 1.0],
                   "bbox": None, "segmentation": None,
                   "class_id": -1, "class_name": "connector", "degree": 0}],
        "links": [], "text_blocks": [], "bindings": [],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    m = GraphDataModel()
    assert m.load(str(gp))
    return m


def _gesture(m, mgr, centroid):
    """Жест по боевому паттерну SnapshotCommand: execute -> мутация -> finalize."""
    cmd = SnapshotCommand(m, lambda: None)
    cmd.execute()
    m.nodes["n1"]["centroid"] = list(centroid)
    cmd.finalize()
    mgr.push_executed(cmd)


def test_restore_gives_model_private_copies(tmp_path):
    """Инвариант: после restore мутация модели не трогает сам снапшот."""
    m = _model(tmp_path)
    snap = m.snapshot()
    m.restore(snap)
    m.nodes["n1"]["centroid"][0] = 777.0
    assert snap[0]["n1"]["centroid"][0] != 777.0, \
        "restore отдал модели объекты снапшота по ссылке — история undo мутируема"


def test_redo_after_second_gesture_returns_first_gesture_state(tmp_path):
    """Пользовательское репро: жест1 -> undo -> redo -> жест2 -> undo x2 -> redo.

    Redo обязан вернуть итог жеста 1. При алиасе снапшота жест 2 писал бы
    прямо в cmd1._after, и redo прыгал бы в итог жеста 2, минуя жест 1.
    """
    m = _model(tmp_path)
    mgr = UndoManager()
    _gesture(m, mgr, (10.0, 10.0))        # жест 1
    mgr.undo()
    mgr.redo()                            # модель == итог жеста 1
    _gesture(m, mgr, (99.0, 99.0))        # жест 2
    mgr.undo()                            # откат жеста 2
    mgr.undo()                            # откат жеста 1
    mgr.redo()                            # повтор жеста 1
    assert m.nodes["n1"]["centroid"] == [10.0, 10.0], \
        "история undo мутирована жестом 2 (алиас снапшота в restore)"
