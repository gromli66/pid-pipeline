# -*- coding: utf-8 -*-
"""corpus.py — загрузчик корпуса графов (КД7, пункт 0.8 дороги рефакторинга).

Зачем. Корпус жил только в `storage/diagrams/` этой машины, и каждый стенд
искал его сам (`layout_bench.py:404`, `cmp_bitexact.py:66`, `corner_probe.py:77`,
`tests/test_canvas_pipeline_golden.py:28`). На чистом клоне и в CI все они молча
оставались без данных: корпусные проверки не падали, а исчезали из сбора.
Пункт 0.8 кладёт три самых мелких графа в git (`tests/fixtures/graph/`), а этот
модуль даёт общий вход к ним. На него переведены `layout_bench.py` и
корпусная цепочка `tests/test_canvas_pipeline_golden.py`; `cmp_bitexact.py` и
`corner_probe.py` пока ищут storage сами (их эталоны лежат в `_scratch/`,
которого в git тоже нет, — это отдельная работа, не пункт 0.8).

Порядок поиска: сначала фикстура из git (есть везде, не меняется), потом
локальный `storage/diagrams/<uid>/graph/graph_validated.json` (графов больше,
но только на машине разработки). Ключ везде — `uid8`, первые 8 символов uid,
как в таблицах стендов.

Данные фикстур — байт-в-байт копии `graph_validated.json` (происхождение и
sha256 — `tests/fixtures/graph/README.md`).

Использование:
    from tools import corpus
    corpus.graph_path("d74eb9f1")          # Path | None
    corpus.load_graph("d74eb9f1")          # dict (node-link)
    corpus.corpus_paths()                  # {uid8: Path}, фикстуры + storage
    corpus.corpus_paths(include_storage=False)   # только то, что есть в git
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "graph"
STORAGE_DIR = REPO / "storage" / "diagrams"


def fixture_paths() -> dict[str, Path]:
    """Корпус из git: {uid8: путь к json}."""
    if not FIXTURE_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(FIXTURE_DIR.glob("*.json"))}


def storage_paths() -> dict[str, Path]:
    """Корпус из локального storage: {uid8: путь к graph_validated.json}."""
    if not STORAGE_DIR.is_dir():
        return {}
    return {p.parts[-3][:8]: p
            for p in sorted(STORAGE_DIR.glob("*/graph/graph_validated.json"))}


def corpus_paths(include_storage: bool = True) -> dict[str, Path]:
    """Весь доступный корпус. Фикстура из git важнее одноимённого storage."""
    found = storage_paths() if include_storage else {}
    found.update(fixture_paths())
    return dict(sorted(found.items()))


def graph_path(uid8: str) -> Path | None:
    """Путь к графу по uid8; None — графа нет ни в git, ни в storage."""
    return corpus_paths().get(uid8)


def load_graph(uid8: str) -> dict:
    """Разобранный граф по uid8. KeyError, если графа нет."""
    path = graph_path(uid8)
    if path is None:
        raise KeyError(f"графа {uid8} нет ни в {FIXTURE_DIR}, ни в {STORAGE_DIR}")
    return json.loads(path.read_text(encoding="utf-8"))
