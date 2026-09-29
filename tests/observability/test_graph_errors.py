"""
Fault-тесты Волны 3 (graph): под-под-шаги COMPUTE графа в builder.build().

§1 / §8.6: graph — самая медленная авто-стадия (≈46с/GPU → на CPU кратно
дольше), поэтому COMPUTE дробим на под-под-шаги (Вариант A, как ensemble.py /
engine.py): load_masks / bridge_preprocess / prepare_tracing / trace_edges.
Границы видны в логах (движение фазы на CPU) + сбой типизируется с проставленным
step → доезжает до failed_step в /stages.

ИЗОЛЯЦИЯ (§9 #2 / урок §8.12, паттерн test_junction_errors): заглушки cv2 +
sibling-модулей graph ставим через ФИКСТУРУ ``gb`` с monkeypatch.setitem
(функц. скоуп → авто-восстановление), а НЕ на уровне модуля. Модуль-уровневый
``sys.modules.setdefault`` загрязнял сессию уже на этапе КОЛЛЕКЦИИ pytest:
MagicMock вместо реального ``modules.graph.core.direction_nodes`` ронял 13
тестов tests/test_direction_nodes.py (аудит 2026-07-09, C3). Свежий импорт
builder под заглушками + pop из кэша на teardown = ноль протечки. scipy
инжектим ТОЛЬКО на время build() через monkeypatch.setitem (builder импортит
его лениво внутри build()); numpy реальный; обёртка obs — настоящая (app на
PYTHONPATH).
"""

import importlib
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")

from app.core.errors import ArtifactMissingError, PipelineError
# настоящий модуль кратных рёбер (Р7) грузится ДО заглушек фикстуры: его
# зависимость direction_nodes фикстура подменяет MagicMock-ом
from modules.graph.core import parallel_edges as _real_parallel_edges

_H = _W = 8

_GRAPH_SIBLINGS = ("utils", "nodes", "tracing", "bridge_preprocessing",
                   "visualize", "export_json", "direction_nodes")


@pytest.fixture
def gb(monkeypatch):
    """Изолированный свежий импорт builder под заглушками (см. модульный
    docstring). setitem — авто-восстановление; builder удаляем из кэша сами."""
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())
    for _sib in _GRAPH_SIBLINGS:
        monkeypatch.setitem(sys.modules, f"modules.graph.core.{_sib}", MagicMock())

    sys.modules.pop("modules.graph.core.builder", None)
    mod = importlib.import_module("modules.graph.core.builder")
    yield mod
    sys.modules.pop("modules.graph.core.builder", None)


# --- 1. Листья ошибок, которыми задача build_graph типизирует raise'ы -----------

def test_error_leaves_used_by_graph_task():
    assert ArtifactMissingError("x").code == "artifact_missing"
    assert PipelineError("x").code == "pipeline_error"
    assert issubclass(ArtifactMissingError, PipelineError)


# --- helpers -------------------------------------------------------------------

def _zeros():
    return np.zeros((_H, _W), dtype=np.uint8)


def _fake_scipy(monkeypatch):
    """scipy.ndimage.{label, sum} → минимум для build(); инжект только на время теста."""
    scipy = types.ModuleType("scipy")
    ndimage = types.ModuleType("scipy.ndimage")
    ndimage.label = lambda arr, *a, **k: (np.zeros(np.asarray(arr).shape, dtype=int), 1)
    # buried_connectors (П4): суммы по меткам, семантика scipy.ndimage.sum
    ndimage.sum = lambda arr, labels=None, index=(), **k: np.array(
        [np.asarray(arr)[np.asarray(labels) == i].sum() for i in index])
    scipy.ndimage = ndimage
    monkeypatch.setitem(sys.modules, "scipy", scipy)
    monkeypatch.setitem(sys.modules, "scipy.ndimage", ndimage)


def _wire_happy(gb, monkeypatch):
    """Замокать вызовы build() на минимально валидные возвраты (one-component)."""
    _fake_scipy(monkeypatch)
    monkeypatch.setattr(gb, "load_binary_mask", lambda *a, **k: _zeros())
    monkeypatch.setattr(gb, "preprocess_bridges", lambda **k: {
        "valid_bridges_mask": _zeros(),
        "invalid_bridges_mask": _zeros(),
        "bridge_routing": {},
        "bridge_info": {},
        "updated_connections_mask": _zeros(),
    })
    monkeypatch.setattr(gb, "prepare_tracing_data",
                        lambda **k: (_zeros(), {}, {}))
    monkeypatch.setattr(gb, "trace_edges_v3", lambda **k: ([], []))
    monkeypatch.setattr(gb, "compute_edge_statistics", lambda e: None)
    monkeypatch.setattr(gb, "update_node_degrees", lambda *a, **k: None)
    monkeypatch.setattr(gb, "filter_isolated_connectors",
                        lambda n, e, **k: ([], [], {"removed_connectors": 0,
                                                    "removed_edges": 0}))


def _builder(gb):
    return gb.GraphBuilder(verbose=False, debug=False, node_dilation=0)


def _build(b, tmp_path):
    d = tmp_path / "seg"
    d.mkdir(parents=True, exist_ok=True)
    return b.build(
        equipment_mask_path=str(d / "eq.png"),
        connection_mask_path=str(d / "conn.png"),
        bridge_mask_path=str(d / "bridge.png"),
        skeleton_path=str(d / "skel.png"),
        original_image_path=None,
        coco_path=None,
        image_filename=None,
    )


# --- 2. happy path: 4 под-под-шага логируют start/end + duration ---------------

def test_build_substeps_log_start_end_duration(gb, monkeypatch, caplog, tmp_path):
    _wire_happy(gb, monkeypatch)
    b = _builder(gb)

    with caplog.at_level(logging.INFO):
        result = _build(b, tmp_path)

    assert result["nodes"] == [] and result["edges"] == []

    ends = [r for r in caplog.records if getattr(r, "event", None) == "end"]
    names = {getattr(r, "step", None) for r in ends}
    assert {"load_masks", "bridge_preprocess", "prepare_tracing", "trace_edges"} <= names
    # duration проставлен на конце каждого под-под-шага (DoD §4)
    assert all(isinstance(getattr(r, "duration_ms", None), int) for r in ends)


# --- 3. Сбой трассировки → PipelineError(step=trace_edges) ----------------------

def test_trace_edges_failure_typed_with_step(gb, monkeypatch, caplog, tmp_path):
    _wire_happy(gb, monkeypatch)
    monkeypatch.setattr(gb, "trace_edges_v3",
                        MagicMock(side_effect=RuntimeError("trace boom")))
    b = _builder(gb)

    with caplog.at_level(logging.INFO):
        with pytest.raises(PipelineError) as ei:
            _build(b, tmp_path)

    assert ei.value.step == "trace_edges"
    assert ei.value.code == "pipeline_error"
    assert isinstance(ei.value.cause, RuntimeError)
    # предыдущие фазы закрылись (на CPU видно движение до точки сбоя)
    events = {(getattr(r, "step", None), getattr(r, "event", None))
              for r in caplog.records}
    assert ("load_masks", "end") in events
    assert ("prepare_tracing", "end") in events
    assert ("trace_edges", "start") in events


# --- 4. Уже типизированный сбой проходит насквозь (без двойной обёртки) ----------

def test_pipeline_error_passthrough(gb, monkeypatch, tmp_path):
    _wire_happy(gb, monkeypatch)
    boom = PipelineError("domain-fail", step="trace_edges")
    monkeypatch.setattr(gb, "trace_edges_v3",
                        MagicMock(side_effect=boom))
    b = _builder(gb)

    with pytest.raises(PipelineError) as ei:
        _build(b, tmp_path)

    assert ei.value is boom
    assert ei.value.step == "trace_edges"


# --- 5. Кратные рёбра (Р7): builder нормализует после сирот, до степеней -------

def _two_pipes(cls):
    nodes = [{"id": n, "type": "equipment", "class_name": cls, "class_id": 0,
              "centroid": c, "bbox": None, "degree": 0}
             for n, c in (("node_1", [100, 0]), ("node_2", [100, 200]))]
    straight = [[100, x] for x in range(0, 201, 2)]
    if cls == "unknow":            # дубль: концы совпадают до 0 px
        other = [list(p) for p in straight]
    else:                          # вторая труба обходом, у node_2 разнос 60 px
        other = [[100 + i, 0] for i in range(60)] + [[160, x] for x in range(0, 201, 4)]
    edges = [{"id": f"edge_{i}", "from": "node_1", "to": "node_2",
              "source_point": list(p[0]), "target_point": list(p[-1]), "path": p}
             for i, p in ((0, straight), (1, other))]
    return nodes, edges


def _build_two_pipes(gb, monkeypatch, tmp_path, cls):
    _wire_happy(gb, monkeypatch)
    monkeypatch.setitem(sys.modules, "modules.graph.core.parallel_edges",
                        _real_parallel_edges)
    nodes, edges = _two_pipes(cls)
    monkeypatch.setattr(gb, "trace_edges_v3", lambda **k: (edges, nodes))
    monkeypatch.setattr(gb, "filter_isolated_connectors",
                        lambda n, e, **k: (n, e, {"removed_connectors": 0,
                                                  "removed_edges": 0}))
    return _build(_builder(gb), tmp_path)


def test_build_splits_second_pipe(gb, monkeypatch, tmp_path):
    result = _build_two_pipes(gb, monkeypatch, tmp_path, "nasos")
    ids = sorted(e["id"] for e in result["edges"])
    assert ids == ["edge_0", "edge_1", "edge_2"]
    assert [n["id"] for n in result["nodes"]][-1] == "parconn_1"
    ends = {(e["from"], e["to"]) for e in result["edges"]}
    assert ends == {("node_1", "node_2"), ("node_1", "parconn_1"), ("parconn_1", "node_2")}


def test_build_unknow_dup_split_not_deleted(gb, monkeypatch, tmp_path):
    result = _build_two_pipes(gb, monkeypatch, tmp_path, "unknow")
    assert len(result["edges"]) == 3
    assert any(n["id"] == "parconn_1" for n in result["nodes"])
