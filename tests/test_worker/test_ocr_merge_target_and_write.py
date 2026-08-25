# -*- coding: utf-8 -*-
"""Блок 5 «точечных болей» (2026-08-25), мелочи слияния OCR в граф.

Две клетки, обе про ТИХУЮ порчу — то есть про случаи, где провал выглядит
успехом и не виден ни оператору, ни логу:

1. **Куда сливать.** Выбор графа шёл `ORDER BY <bool>` БЕЗ `.desc()`, а это
   возрастание: `False` (`graph.json`) первым. При обеих строках — а они и
   лежат обе после сборки и валидации — текст уезжал в СЫРОЙ граф сборки,
   мимо того, который правил оператор и который читает «Привязка подписей».
   Молча: `n_merged` при этом честно ненулевой, стадия зелёная.
   ⚠ Внутри `task_run_ocr` эта ошибка непроверяема: там подделка сессии,
   `order_by` у неё — заглушка. Поэтому решение вынесено в `pick_graph_artifact`
   и судится НАСТОЯЩИМ SQL на sqlite: направление сортировки — наше решение,
   а не свойство библиотеки.

2. **Чем писать.** Слияние открывало сам граф на `"w"`, то есть УСЕКАЛО его
   в ноль ещё до того, как в него что-то ляжет. Падение посреди `json.dump`
   (диск кончился, воркер убит по таймауту) оставляло оператора без графа
   вовсе — и функция при этом возвращала `0`, законный в норме.

Числа абсолютные, пороги заперты с двух сторон: и «выбран validated», и
«именно он, а не любой из двух»; и «граф цел после падения», и «после
успешной записи текст в нём ЕСТЬ».
"""
import json
import uuid

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Artifact, ArtifactType
from app.db.base import Base
from app.services.ocr_graph_merge import merge_ocr_result_into_graph
from worker.tasks.ocr import pick_graph_artifact

UID = uuid.UUID("e5000000-1111-2222-3333-444455556666")

GRAPH = {"nodes": [{"id": "node_1"}], "links": []}
OCR = {"target": [{"bbox": [10, 10, 40, 20], "text": "10LBA10", "confidence": 0.9}],
       "secondary": []}


@pytest.fixture
def db():
    """Настоящий SQL: направление сортировки подделкой не проверить."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Artifact.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _row(kind, path):
    return Artifact(diagram_uid=UID, artifact_type=kind, file_path=path,
                    file_size=1, mime_type="application/json")


# ── 1. куда сливать ──────────────────────────────────────────────────────

@pytest.mark.parametrize("order", ["json_first", "validated_first"],
                         ids=["json_first", "validated_first"])
def test_merge_target_is_the_operators_graph(db, order):
    """Выбирается `graph_validated` — при ЛЮБОМ порядке вставки строк.

    Два порядка вставки, потому что без `.desc()` тест с одним из них остался
    бы зелёным случайно: sqlite отдал бы «первый по rowid», и совпадение
    выглядело бы как работающая сортировка.
    """
    rows = [_row(ArtifactType.GRAPH_JSON, "graph/graph.json"),
            _row(ArtifactType.GRAPH_VALIDATED, "graph/graph_validated.json")]
    if order == "validated_first":
        rows.reverse()
    db.add_all(rows)
    db.commit()

    picked = pick_graph_artifact(db, UID)

    assert picked is not None, "граф не найден вовсе"
    assert picked.artifact_type is ArtifactType.GRAPH_VALIDATED, (
        f"текст уехал бы в {picked.artifact_type} — мимо правок оператора")


def test_merge_target_falls_back_to_the_raw_graph(db):
    """Порог заперт с другой стороны: без validated берётся сырой.

    Иначе «предпочитать validated» лечилось бы «брать только validated», и
    параллельный OCR (он идёт ВМЕСТЕ со сборкой) остался бы без цели вовсе.
    """
    db.add(_row(ArtifactType.GRAPH_JSON, "graph/graph.json"))
    db.commit()

    picked = pick_graph_artifact(db, UID)

    assert picked is not None
    assert picked.artifact_type is ArtifactType.GRAPH_JSON


def test_merge_target_is_none_when_there_is_no_graph(db):
    """Ни одного графа — `None`, а не исключение: OCR идёт параллельно сборке."""
    assert pick_graph_artifact(db, UID) is None


# ── 2. чем писать ────────────────────────────────────────────────────────

def _prepare(tmp_path):
    graph_path = tmp_path / "graph_validated.json"
    graph_path.write_text(json.dumps(GRAPH), encoding="utf-8")
    ocr_path = tmp_path / "ocr_result.json"
    ocr_path.write_text(json.dumps(OCR), encoding="utf-8")
    return graph_path, ocr_path


def test_merge_writes_the_text_into_the_graph(tmp_path):
    """Успешный путь: блоки в графе есть — иначе клетка ниже слепа."""
    graph_path, ocr_path = _prepare(tmp_path)

    n = merge_ocr_result_into_graph(graph_path, ocr_path)

    assert n == 1, "слияние не перенесло блок"
    written = json.loads(graph_path.read_text(encoding="utf-8"))
    assert written["text_blocks"], "текстовых блоков в графе нет"
    assert written["nodes"] == GRAPH["nodes"], "узлы потерялись при записи"
    assert not list(tmp_path.glob("*.tmp")), "временный файл остался лежать"


def test_a_crash_mid_write_leaves_the_graph_intact(tmp_path, monkeypatch):
    """Падение посреди записи НЕ оставляет оператора без графа.

    Инъекция бьёт ровно в `json.dump` — то место, между открытием файла и
    концом записи, где старый код уже успел усечь граф в ноль.
    """
    graph_path, ocr_path = _prepare(tmp_path)
    before = graph_path.read_text(encoding="utf-8")

    import app.services.ocr_graph_merge as merge_mod
    real_dump = merge_mod.json.dump

    def _boom(obj, fp, **kwargs):
        fp.write('{"nodes": [')       # частичная запись, как при обрыве
        raise OSError("на диске нет места")

    monkeypatch.setattr(merge_mod.json, "dump", _boom)
    try:
        n = merge_ocr_result_into_graph(graph_path, ocr_path)
    finally:
        monkeypatch.setattr(merge_mod.json, "dump", real_dump)

    assert n == 0, "провал записи объявлен успехом"
    assert graph_path.read_text(encoding="utf-8") == before, (
        "граф оператора усечён падением слияния")
    assert not list(tmp_path.glob("*.tmp")), "мусорный временный файл остался"
