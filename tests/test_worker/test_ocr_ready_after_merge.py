# -*- coding: utf-8 -*-
"""Блок 1 болей (pains-1, 2026-08-25), пункт 1.6: «готово» — только после слияния.

Боль. Клиент судит о готовности распознавания по СТРОКЕ В БД: `get_ocr_status`
отдаёт `has_ocr_result` по наличию `Artifact(OCR_RESULT)` (`app/api/ocr.py`), а
воркер до правки завершал стадию и коммитил артефакт РАНЬШЕ, чем сливал
результат в граф: файл -> артефакт -> `complete_stage` -> `commit` -> и только
потом merge. В это окно оператор уже видел зелёный этап и шёл в «Привязку
подписей» — по графу БЕЗ текстовых блоков. Хуже: падение слияния глоталось
`except Exception` с `warning` в лог, то есть выглядело УСПЕХОМ (родня A7 из
ROADMAP: «провал OCR-merge неотличим от успеха»).

Решение №8 редтима (2026-08-25): merge переставлен ДО `complete_stage`, и его
падение становится честной ошибкой этапа — стадия `failed`, `error_stage='ocr'`,
артефакт не закоммичен, то есть клиент «готово» не покажет и повтор возможен.

⚠ Граница проверяемого: здесь судится ЗАДАЧА — что она не глотает исключение и не
объявляет «готово» до слияния. Сама `merge_ocr_result_into_graph` свои отказы ввода-вывода
гасит и возвращает `0` (`app/services/ocr_graph_merge.py:59,65,85`), а ноль законен и в
норме, поэтому тихий провал ЗАПИСИ графа этим набором не ловится и ловиться не может —
это A7 из ROADMAP, корень в контракте функции (хозяин — дорога).

Утверждается ПОРЯДОК наблюдаемых действий, а не текст лога: журнал `calls`
пишут подменённые `complete_stage`/`merge`/`commit`, поэтому тест краснеет ровно
на перестановке, а не на переформулировке сообщения.

Изоляция (канон `docs/TESTING.md §6`): тяжёлые импорты задачи (`torch`,
`modules.ocr.pipeline_clean`) подменяются в `sys.modules` фикстурой, БД —
поддельная сессия, файлы — `tmp_path`. Сам `task_run_ocr` при этом настоящий:
проверяется его код, а не представление о нём.
"""
import json
import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

pytest.importorskip("celery")

from app.models import Artifact, ArtifactType, Diagram      # noqa: E402
from app.models.diagram import DiagramStatus               # noqa: E402

UID = "e1c0a5aa-1111-2222-3333-444455556666"


class FakeQuery:
    """Ответ зависит от ТОГО, ЧТО спросили, а не от порядка вопросов.

    Различитель — `order_by`: из трёх запросов `Artifact` его зовёт только
    поиск графа (`GRAPH_VALIDATED` вперёд `GRAPH_JSON`), а идемпотентность и
    поиск прошлого результата OCR обходятся без сортировки. Позиционная
    очередь ответов сломалась бы от самой перестановки, которую тест и судит.
    """

    def __init__(self, answer_plain, answer_ordered=None):
        self._plain = answer_plain
        self._ordered = answer_ordered
        self._is_ordered = False

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        self._is_ordered = True
        return self

    def first(self):
        return self._ordered if self._is_ordered else self._plain


class FakeDB:
    """Сессия с журналом: по нему видно, что и когда задача записала."""

    def __init__(self, diagram, graph_artifact, calls):
        self.diagram = diagram
        self.graph_artifact = graph_artifact
        self.calls = calls
        self.added = []
        self.committed = []

    def query(self, model):
        if model is Diagram:
            return FakeQuery(self.diagram)
        # Прошлого результата OCR нет (задача идёт впервые); граф — есть.
        return FakeQuery(None, self.graph_artifact)

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        pass

    def flush(self):
        pass

    def commit(self):
        self.calls.append("commit")
        self.committed.extend(self.added)

    def rollback(self):
        self.calls.append("rollback")
        self.added.clear()

    def close(self):
        pass


class FakeStage:
    id = 42
    status = None


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Задача настоящая; окружение, БД и тяжёлые модули — поддельные."""
    calls = []

    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    original = tmp_path / UID / "original"
    original.mkdir(parents=True)
    (original / "image.png").write_bytes(b"PNG")

    torch_stub = MagicMock()
    torch_stub.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", torch_stub)

    pipeline = MagicMock()
    pipeline.run_ocr_pipeline_clean.return_value = {
        "target": [{"text": "PT-101", "bbox": [1, 2, 3, 4]}],
        "secondary": [],
    }
    monkeypatch.setitem(sys.modules, "modules.ocr.pipeline_clean", pipeline)

    import worker.tasks.ocr as task_module

    diagram = Diagram()
    diagram.uid = uuid.UUID(UID)
    diagram.project_code = "thermohydraulics"
    diagram.status = DiagramStatus.VALIDATED_GRAPH
    diagram.error_stage = None
    diagram.error_message = None

    graph_art = Artifact()
    graph_art.diagram_uid = uuid.UUID(UID)
    graph_art.artifact_type = ArtifactType.GRAPH_VALIDATED
    graph_art.file_path = f"{UID}/graph/graph_validated.json"
    graph_path = tmp_path / UID / "graph"
    graph_path.mkdir(parents=True)
    (graph_path / "graph_validated.json").write_text(
        json.dumps({"nodes": [], "edges": []}), encoding="utf-8")

    db = FakeDB(diagram, graph_art, calls)

    monkeypatch.setattr(task_module, "SessionLocal", lambda: db, raising=False)
    import app.db.session as session_module
    monkeypatch.setattr(session_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(task_module, "check_deleted", lambda *a, **kw: False)
    monkeypatch.setattr(task_module, "start_stage", lambda *a, **kw: FakeStage())
    monkeypatch.setattr(task_module, "make_step_reporter", lambda *a, **kw: None)

    def _complete(stage, payload=None):
        calls.append("complete_stage")

    def _fail(stage, message, trace=None, exc=None):
        calls.append("fail_stage")

    def _set_error(db_, uid, message, stage_value):
        calls.append(f"set_diagram_error:{stage_value}")

    monkeypatch.setattr(task_module, "complete_stage", _complete)
    monkeypatch.setattr(task_module, "fail_stage", _fail)
    monkeypatch.setattr(task_module, "set_diagram_error", _set_error)
    monkeypatch.setattr(task_module, "persist_failed_attempt",
                        lambda *a, **kw: calls.append("persist_failed_attempt"))

    import app.services.project_loader as project_loader

    class _Loader:
        def load(self, code):
            cfg = MagicMock()
            cfg.ocr.enabled = True
            return cfg

    monkeypatch.setattr(project_loader, "get_project_loader", lambda: _Loader())

    import app.services.ocr_graph_merge as merge_module

    def set_merge(fn):
        monkeypatch.setattr(merge_module, "merge_ocr_result_into_graph", fn)

    return calls, db, task_module, set_merge, tmp_path


def _ok_merge(calls):
    def _merge(graph_path, ocr_path):
        calls.append("merge")
        return 1
    return _merge


def test_stage_completes_only_after_the_merge(bench):
    """Порядок: слияние -> завершение стадии -> коммит.

    До правки первым шло `complete_stage`, и «готово» наступало раньше, чем
    текст доезжал до графа.
    """
    calls, db, task_module, set_merge, _tmp = bench
    set_merge(_ok_merge(calls))

    task_module.task_run_ocr.apply(args=[UID], throw=True)

    assert "merge" in calls, calls
    assert calls.index("merge") < calls.index("complete_stage"), calls
    assert calls.index("complete_stage") < calls.index("commit"), calls
    assert [type(a) for a in db.committed] == [Artifact], db.committed


def test_a_failed_merge_is_an_honest_error(bench):
    """Падение слияния — ошибка этапа, а не тихий успех.

    Утверждается РАЗНИЦА с зелёным путём: тот же прогон, но слияние падает —
    стадия не завершается, артефакт не доезжает до БД, `error_stage` назван.
    До правки здесь были `complete_stage` + `commit` + `warning` в лог.
    """
    calls, db, task_module, set_merge, _tmp = bench

    def _boom(graph_path, ocr_path):
        calls.append("merge")
        raise ValueError("граф не читается")

    set_merge(_boom)

    result = task_module.task_run_ocr.apply(args=[UID], retries=1, throw=False)

    assert result.failed(), result.result
    assert "complete_stage" not in calls, calls
    assert "fail_stage" in calls, calls
    assert "set_diagram_error:ocr" in calls, calls
    assert db.committed == [], db.committed


def test_the_ocr_file_is_written_even_if_the_merge_fails(bench, tmp_path):
    """Порог с другой стороны: работа распознавания не выбрасывается.

    Файл `ocr_result.json` остаётся на диске — повтор задачи не считает
    заново то, что уже посчитано глазами модели; в БД его при этом нет,
    поэтому клиент «готово» не покажет (`has_ocr_result` смотрит строку).
    """
    calls, db, task_module, set_merge, storage = bench

    def _boom(graph_path, ocr_path):
        raise ValueError("граф не читается")

    set_merge(_boom)

    task_module.task_run_ocr.apply(args=[UID], retries=1, throw=False)

    assert (storage / UID / "ocr" / "ocr_result.json").exists()
    assert db.committed == []
