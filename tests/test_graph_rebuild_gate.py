# -*- coding: utf-8 -*-
"""Пересборка графа против фазы B (блок 5 «точечных болей», 2026-08-25).

Зачем файл. Сборка графа заново («Сборка схемы» из уже готовой схемы, повтор
после ошибки) переписывает `graph.json`/`graph_validated.json` целиком, и `id`
узлов между сборками НЕ выживают — они порядковые `node_N` от порядка компонент
маски (`modules/graph/core/nodes.py`). Пока она идёт, любая работа фазы B
пишется в граф, которого через минуту не будет: подтверждение контуров
переставляет статус на `contours_validated` и ставит раскладку по
переписываемому графу, привязка вешает KKS на узлы ПРОШЛОГО поколения.

Решение Максима (2026-08-25): **на время сборки фаза B блокируется**. Форма
гейта — WHITELIST, а не «BUILDING и раньше»: чёрный список пропускает `ERROR`,
в который упавшая сборка и уходит (замечание редтима). `ERROR` разбирается
отдельно по `error_stage` (решение №3): упавшая сборка — 400, упавший OCR фазу B
не запирает.

Класс правки — [сма], значит `PROTOCOL §3`: **первая редакция этого файла снята
с НЕТРОНУТОГО кода и зелена на нём**. Таблица `ACCEPTS` ниже — литералы,
прочитанные в коде, а не вычисленные из него; правка гейтов меняет её ЯВНО, и по
диффу этого файла видно, какие именно клетки закрылись.

Что судится. Настоящие корутины десяти эндпоинтов — все, кто при пересборке
пишет или жжёт очередь:

    POST /api/contours/{uid}/extract        — extract_contours
    PUT  /api/contours/{uid}/validated      — upload_contours_validated
    POST /api/contours/{uid}/auto-accept    — auto_accept_contours
    POST /api/contours/{uid}/complete       — complete_contour_validation
    PUT  /api/contours/{uid}/training       — upload_contours_training
    PUT  /api/ocr/{uid}/result              — update_ocr_result
    POST /api/ocr/{uid}/validation/save     — save_ocr_validation
    POST /api/ocr/{uid}/recognize           — recognize_ocr_boxes
    POST /api/ocr/{uid}/binding/apply       — apply_ocr_binding
    POST /api/ocr/{uid}/start               — start_ocr

`AsyncSession` подделана, хранилище уведено в `tmp_path`, раскладка и брокер
замоканы. Гейты НАЛИЧИЯ (нет артефакта → 404) пройдены заранее: судится ровно
гейт СТАТУСА.
"""
import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException

import app.api.contours as contours_api
import app.services.storage as storage_mod
from app.api.contours import (
    auto_accept_contours,
    complete_contour_validation,
    extract_contours,
    upload_contours_training,
    upload_contours_validated,
)
from app.api.ocr import (
    apply_ocr_binding,
    recognize_ocr_boxes,
    save_ocr_binding,
    save_ocr_validation,
    start_ocr,
    update_ocr_result,
)
from app.api.validation import save_validated_graph
from app.api.build_gate import GRAPH_READY_STATUSES, REBUILD_REFUSAL
from app.models import Artifact, ArtifactType, Diagram, DiagramStatus

UID = uuid.UUID("c5000000-1111-2222-3333-444455556666")

STATUS_COUNT = 31

#: Этап, которым воркер подписывает упавшую сборку графа
#: (`worker/tasks/graph.py`, заперто `tests/test_graph_build_status_gate.py`).
GRAPH_BUILD_STAGE = "building_graph"

ENDPOINTS = (
    "contours_extract",
    "contours_validated_put",
    "contours_auto_accept",
    "contours_complete",
    "contours_training_put",
    "ocr_result_put",
    "ocr_validation_save",
    "ocr_recognize",
    "ocr_binding_apply",
    "ocr_start",
    # Возврат ревизии связки 3+5: этих двух в решётке НЕ БЫЛО, и расхождение
    # `apply` c `save` при `ERROR` попало ровно в зазор между двумя наборами —
    # блок 5 их не судил, а решётка pains-3 не знает про гейт пересборки.
    "graph_save",
    "binding_save",
)

ALL_STATUSES = {s.value for s in DiagramStatus}

# ── гейт статуса: какие статусы эндпоинт пускает ─────────────────────────
#
# Литералы, снятые ЧТЕНИЕМ кода. Всё, чего в множестве нет, — 400.
#
# `GRAPH_READY` — общий белый список восьми эндпоинтов: граф уже собран,
# фазе B есть с чем работать. Всё, что раньше `BUILT`, — фаза A и сама сборка.
# `ERROR` в решётке судится с ПУСТЫМ `error_stage`; упавшая сборка — отдельным
# тестом ниже.
GRAPH_READY = {"built", "validating_graph", "validated_graph",
               "extracting_contours", "contours_extracted",
               "contours_validated", "ocr_processing", "ocr_completed",
               "ocr_bound", "generating_fxml", "completed", "error"}

ACCEPTS = {
    "contours_extract": GRAPH_READY,
    "contours_validated_put": GRAPH_READY,
    "contours_auto_accept": GRAPH_READY,
    "contours_complete": GRAPH_READY,
    "contours_training_put": GRAPH_READY,
    "ocr_result_put": GRAPH_READY,
    "ocr_validation_save": GRAPH_READY,
    "ocr_recognize": GRAPH_READY,
    # Список взят у соседа по вкладке — `/binding/save` (`_BINDING_SAVE_STATUSES`):
    # разъедься они, оператор получил бы «сохранить можно, применить нельзя».
    # Отличие от `GRAPH_READY` ровно в двух клетках: до валидации графа
    # применять нечего.
    "ocr_binding_apply": GRAPH_READY - {"built", "validating_graph", "error"},
    # Единственный, у кого свой список был и до блока 5. Убран `building_graph`:
    # эндпоинт первым же делом СНОСИТ `OCR_RESULT` (`app/api/ocr.py`), а сборка
    # графа в этот момент ждёт текстовые блоки, чтобы слить их в свежий граф.
    "ocr_start": {"validated_junctions", "built",
                  "validating_graph", "validated_graph", "extracting_contours",
                  "contours_extracted", "contours_validated", "ocr_completed",
                  "ocr_bound", "generating_fxml", "completed", "error"},
    # Собственные списки pains-3 + гейт пересборки перед ними. `error` у
    # `/graph/save` — решение №3, доведённое до конца (первая запись вкладки
    # «Контуры» приходит сюда); у `/binding/save` `error` НЕТ, и `/binding/apply`
    # с тем же списком теперь тоже его не пускает.
    "graph_save": {"built", "validating_graph", "validated_graph",
                   "contours_validated", "ocr_completed", "ocr_bound",
                   "generating_fxml", "completed", "error"},
    "binding_save": GRAPH_READY - {"built", "validating_graph", "error"},
}

#: Пускает ли эндпоинт `ERROR` с `error_stage` упавшей СБОРКИ ГРАФА.
#: Решение №3 редтима: упавшая сборка — 400, упавший OCR фазу B не запирает.
#: `/ocr/start` в перечень гейта не входит — это ЕГО штатный путь перезапуска
#: (`error` в его списке стоял всегда), а сборку он не трогает.
ACCEPTS_FAILED_BUILD = {e: False for e in ENDPOINTS}
ACCEPTS_FAILED_BUILD["ocr_start"] = True


# ── харнесс ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """Поверхность `AsyncSession` этих десяти эндпоинтов."""

    def __init__(self, diagram, artifacts=None):
        self.diagram = diagram
        self.artifacts = dict(artifacts or {})
        self.commits = 0
        self.added = []
        self.removed = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Diagram:
            return _FakeResult(self.diagram)
        params = stmt.compile().params
        return _FakeResult(self.artifacts.get(params.get("artifact_type_1")))

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.removed.append(obj)


class FakeUpload:
    """Поверхность `UploadFile` трёх загрузок."""

    def __init__(self, payload):
        self._payload = payload

    async def read(self):
        return self._payload


def _artifact(path):
    art = Artifact()
    art.file_path = path
    art.file_size = 1
    art.mime_type = "application/json"
    return art


def _diagram(status_value, error_stage=None):
    diagram = Diagram()
    diagram.uid = UID
    diagram.status = DiagramStatus(status_value)
    diagram.error_stage = error_stage
    diagram.error_message = "boom" if error_stage else None
    diagram.project_code = "thermohydraulics"
    return diagram


# Артефакты на месте: гейты наличия пройдены, судится ГЕЙТ СТАТУСА.
ARTIFACTS = {
    ArtifactType.GRAPH_VALIDATED: _artifact(
        f"{UID}/graph/graph_validated.json"),
    ArtifactType.OCR_RESULT: _artifact(f"{UID}/ocr/ocr_result.json"),
    ArtifactType.OCR_BINDING: _artifact(
        f"{UID}/ocr_binding/ocr_binding.json"),
    ArtifactType.CONTOURS_AUTO: _artifact(f"{UID}/contours/contours_auto.json"),
    ArtifactType.CONTOURS_VALIDATED: _artifact(
        f"{UID}/contours/contours_validated.json"),
}

CALL = {
    "contours_extract": lambda db: extract_contours(UID, ann_ids=None, db=db),
    "contours_validated_put": lambda db: upload_contours_validated(
        UID, file=FakeUpload(b'{"nodes": []}'), db=db),
    "contours_auto_accept": lambda db: auto_accept_contours(UID, db=db),
    "contours_complete": lambda db: complete_contour_validation(UID, db=db),
    "contours_training_put": lambda db: upload_contours_training(
        UID, file=FakeUpload(b'{"samples": []}'), db=db),
    "ocr_result_put": lambda db: update_ocr_result(
        UID, file=FakeUpload(b'{"target": []}'), db=db),
    "ocr_validation_save": lambda db: save_ocr_validation(
        UID, file=FakeUpload(b'{"classifications": []}'), db=db),
    "ocr_recognize": lambda db: recognize_ocr_boxes(
        UID, payload={"boxes": []}, db=db),
    "ocr_binding_apply": lambda db: apply_ocr_binding(UID, db=db),
    "ocr_start": lambda db: start_ocr(UID, db=db),
    "graph_save": lambda db: save_validated_graph(
        UID, file=FakeUpload(b'{"nodes": [], "edges": []}'), db=db),
    "binding_save": lambda db: save_ocr_binding(
        UID, file=FakeUpload(b'{"bindings": []}'), db=db),
}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    """Хранилище с уже лежащими файлами фазы B."""
    monkeypatch.setattr(storage_mod.settings, "STORAGE_PATH", str(tmp_path))
    for stage, name, payload in (
        ("graph", "graph_validated.json", {"nodes": [], "links": []}),
        ("ocr", "ocr_result.json", {"target": []}),
        ("ocr_binding", "ocr_binding.json", {"version": 2, "bindings": []}),
        ("contours", "contours_auto.json", {"nodes": []}),
        ("contours", "contours_validated.json", {"nodes": []}),
    ):
        d = tmp_path / str(UID) / stage
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def layout(monkeypatch):
    """Раскладка — чужая подсистема; здесь только факт вызова."""
    calls = []

    async def _stub(uid, db, force=False):
        calls.append((str(uid), force))
        return {"status": "stub"}

    monkeypatch.setattr(contours_api, "dispatch_layout", _stub)
    return calls


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    """Брокер — чужая подсистема; журнал имён отправленных задач."""
    sent = []

    class _AsyncResult:
        id = "task-stub"

    def _send_task(name, *args, **kwargs):
        sent.append(name)
        return _AsyncResult()

    from worker.celery_app import celery_app
    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return sent


@pytest.fixture(autouse=True)
def ocr_enabled(monkeypatch):
    """OCR включён — как на бою (`thermohydraulics.yaml: ocr.enabled: true`)."""
    monkeypatch.setattr(contours_api, "_ocr_enabled", lambda code: True)


def _run(endpoint, diagram, artifacts=None):
    db = FakeDB(diagram, ARTIFACTS if artifacts is None else artifacts)
    return asyncio.run(CALL[endpoint](db)), db


# ── сторожа самих таблиц ─────────────────────────────────────────────────

def test_status_machine_size_is_locked():
    assert len(list(DiagramStatus)) == STATUS_COUNT


def test_table_keys_name_real_statuses():
    for endpoint in ENDPOINTS:
        for value in ACCEPTS[endpoint]:
            assert DiagramStatus(value).value == value, (endpoint, value)


def test_every_endpoint_has_a_rule():
    for table in (ACCEPTS, ACCEPTS_FAILED_BUILD, CALL):
        assert set(table) == set(ENDPOINTS)


# ── решётка: 31 статус × 10 эндпоинтов ───────────────────────────────────

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", list(DiagramStatus), ids=lambda s: s.value)
def test_gate_over_every_status(endpoint, status, layout, broker):
    """Каждая клетка: пропущена ровно та, что в таблице.

    Порог заперт с двух сторон: отказ дополнительно утверждает, что статус
    не сдвинут, транзакция не коммитилась и в брокер ничего не ушло.
    """
    diagram = _diagram(status.value)
    cell = (endpoint, status.value)
    allowed = status.value in ACCEPTS[endpoint]

    if not allowed:
        with pytest.raises(HTTPException) as exc:
            _run(endpoint, diagram)
        assert exc.value.status_code == 400, cell
        assert diagram.status.value == status.value, cell
        assert layout == [], cell
        assert broker == [], cell
        return

    _result, _db = _run(endpoint, diagram)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_gate_over_a_failed_graph_build(endpoint, layout, broker):
    """`ERROR` от УПАВШЕЙ СБОРКИ: решение №3 редтима.

    Отдельной клеткой, потому что `error` в решётке выше судится с пустым
    `error_stage` — а вся разница именно в нём.
    """
    diagram = _diagram("error", error_stage=GRAPH_BUILD_STAGE)

    if not ACCEPTS_FAILED_BUILD[endpoint]:
        with pytest.raises(HTTPException) as exc:
            _run(endpoint, diagram)
        assert exc.value.status_code == 400, endpoint
        assert diagram.status is DiagramStatus.ERROR, endpoint
        assert layout == [], endpoint
        assert broker == [], endpoint
        return

    _result, _db = _run(endpoint, diagram)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_failed_ocr_does_not_lock_phase_b(endpoint, layout, broker):
    """Второй берег решения №3: упавший OCR фазу B НЕ запирает.

    Без этой клетки гейт мог бы закрыть `ERROR` целиком, и тест выше остался
    бы зелёным — а оператор после падения распознавания терял бы и контуры.

    ⛔ Клетка судится ПО ТАБЛИЦЕ `ACCEPTS`, а не «все пускают» (возврат ревизии
    связки 3+5): у `/binding/save` `ERROR` в списке нет, и `/binding/apply`,
    которому этот же список передан, обязан вести себя ТАК ЖЕ. Пока
    `graph_is_ready` отвечал по `ERROR` до проверки `allowed`, они расходились
    знаком — «применить можно, сохранить нельзя».
    """
    diagram = _diagram("error", error_stage="ocr")
    if "error" not in ACCEPTS[endpoint]:
        with pytest.raises(HTTPException) as exc:
            _run(endpoint, diagram)
        assert exc.value.status_code == 400, endpoint
        return
    _result, _db = _run(endpoint, diagram)


def test_apply_and_save_agree_on_a_failed_ocr(layout, broker):
    """Именно тот дефект, за который вернули: два соседа по вкладке сошлись.

    Утверждается РАВЕНСТВО исходов, а не каждый по отдельности: порознь оба
    были «объяснимы», и расхождение пряталось в зазоре между двумя наборами.
    """
    def _outcome(endpoint):
        try:
            _run(endpoint, _diagram("error", error_stage="ocr"))
            return 200
        except HTTPException as exc:
            return exc.status_code

    assert _outcome("ocr_binding_apply") == _outcome("binding_save")


def test_the_allowed_list_really_decides_the_error_cell():
    """Сторож НАШЕГО решения: `allowed` управляет клеткой `ERROR`.

    Проверяется предикат, а не эндпоинт: подставляем два разных списка одному
    и тому же объекту и требуем разных ответов. Верни кто-нибудь прежний
    порядок (ветка `ERROR` раньше `allowed`) — оба ответа станут `True`.
    """
    from app.api.build_gate import GRAPH_READY_STATUSES, graph_is_ready
    from app.api.ocr import _BINDING_SAVE_STATUSES

    broken = _diagram("error", error_stage="ocr")
    assert graph_is_ready(broken, GRAPH_READY_STATUSES) is True
    assert graph_is_ready(broken, _BINDING_SAVE_STATUSES) is False

    building = _diagram("error", error_stage=GRAPH_BUILD_STAGE)
    assert graph_is_ready(building, GRAPH_READY_STATUSES) is False


# ── машинный признак отказа: клиент и сервер сведены ─────────────────────

GATED = tuple(e for e in ENDPOINTS if e != "ocr_start")


@pytest.mark.parametrize("endpoint", GATED)
def test_refusal_carries_the_machine_marker(endpoint, layout, broker):
    """В `detail` отказа есть машинный признак — по нему клиент морозит буфер.

    Опознавать «граф пересобирается» по русской прозе нельзя: её перепишут, и
    заслон клиента ослепнет молча. Признак ASCII и утверждается здесь, а не
    в клиенте, потому что порождает его сервер.
    """
    with pytest.raises(HTTPException) as exc:
        _run(endpoint, _diagram("building_graph"))
    assert REBUILD_REFUSAL in str(exc.value.detail), endpoint


def test_client_and_server_know_the_same_marker():
    """Сведение копий: клиент носит СВОЮ константу (он пакуется отдельно).

    Разъедься они — заслон перестанет срабатывать, и ни один тест клиента
    этого не увидит: там подделка отвечает своей строкой.
    """
    from ui.tabs.save_mode import REBUILD_REFUSAL as client_marker
    assert client_marker == REBUILD_REFUSAL


#: Три ЗАПИСИ, куда клиент фазы B приходит ПЕРВЫМИ (замер §P5.3): у каждой был
#: свой белый список ещё до блока 5, и гейт пересборки встал ПЕРЕД ним ради
#: машинного признака. Проверяется вложенность — иначе гейт менял бы не только
#: текст отказа, но и пускаемое множество, а это уже чужие пункты (3.1в/Н8+).
NESTED_LISTS = {
    "/validation/graph/save": {
        "built", "validating_graph", "validated_graph", "contours_validated",
        "ocr_completed", "ocr_bound", "generating_fxml", "completed"},
    "/validation/graph/canvas/save": {
        "ocr_completed", "ocr_bound", "generating_fxml", "completed"},
    "/ocr/binding/save": {
        "validated_graph", "extracting_contours", "contours_extracted",
        "contours_validated", "ocr_processing", "ocr_completed", "ocr_bound",
        "generating_fxml", "completed"},
}


@pytest.mark.parametrize("endpoint", sorted(NESTED_LISTS))
def test_the_gate_only_changes_the_wording_there(endpoint):
    """Список эндпоинта ВЛОЖЕН в `GRAPH_READY` — значит гейт не запер ни клетки.

    Литералы сняты чтением кода, как и всё в этом файле; расширь кто-нибудь
    любой из трёх списков за пределы `GRAPH_READY` — гейт начнёт запирать
    то, что сосед пускал, и эта клетка скажет об этом вслух.
    """
    ready = {s.value for s in GRAPH_READY_STATUSES}
    assert NESTED_LISTS[endpoint] <= ready, endpoint


def test_nested_lists_are_the_real_ones():
    """Сторож самих литералов: они те же, что в коде эндпоинтов."""
    from app.api.ocr import _BINDING_SAVE_STATUSES
    assert {s.value for s in _BINDING_SAVE_STATUSES} == \
        NESTED_LISTS["/ocr/binding/save"]


def test_other_400s_are_not_the_rebuild_refusal(layout, broker):
    """Порог заперт с другой стороны: чужой 400 признака НЕ несёт.

    Иначе клиент морозил бы буфер на любом отказе — например на «OCR result
    not available yet» соседнего `/binding/save`, где правки оператора живы
    и лечится всё повтором.
    """
    with pytest.raises(HTTPException) as exc:
        _run("ocr_start", _diagram("uploaded"))
    assert REBUILD_REFUSAL not in str(exc.value.detail)


# ── что именно пишет пропущенный вызов ───────────────────────────────────

def test_contours_complete_still_moves_forward(layout):
    """Порог заперт с другой стороны: разрешённый статус работу ДЕЛАЕТ.

    Иначе гейт мог бы отменить сам переход, и решётка выше не заметила бы.
    """
    diagram = _diagram("contours_extracted")
    _run("contours_complete", diagram)
    assert diagram.status is DiagramStatus.CONTOURS_VALIDATED
    assert layout == [(str(UID), False)]


def test_binding_apply_writes_kks_when_allowed(storage, layout):
    """`/binding/apply` на разрешённом статусе и правда правит граф.

    ⚠ Эта клетка поймала ЧУЖОЙ дефект, без починки которого судить эндпоинт
    нечем: перезапись графа шла через `Path.rename`, а у него на Windows нет
    перезаписи — `os.rename` на существующий файл поднимает `FileExistsError`.
    Граф на этом пути существует ВСЕГДА (без него выше 404), то есть на Windows
    эндпоинт не работал ни разу. На POSIX (бой — Linux) `rename` перезаписывает,
    поэтому там клетка зелена по обе стороны правки: среда, в которой она
    доказуема, — Windows.
    """
    graph_path = storage / str(UID) / "graph" / "graph_validated.json"
    graph_path.write_text(
        json.dumps({"nodes": [{"id": "node_1"}], "links": []}),
        encoding="utf-8")
    binding_path = storage / str(UID) / "ocr_binding" / "ocr_binding.json"
    binding_path.write_text(
        json.dumps({"version": 2, "bindings": [
            {"node_id": "node_1", "text": "10LBA10AA001"}]}),
        encoding="utf-8")

    result, _db = _run("ocr_binding_apply", _diagram("ocr_completed"))

    assert result["updated_nodes"] == 1
    written = json.loads(graph_path.read_text(encoding="utf-8"))
    assert written["nodes"][0]["kks_full"] == "10LBA10AA001"
    assert not graph_path.with_suffix(".tmp").exists(), "временный файл остался"


# ── что переживает саму пересборку ───────────────────────────────────────
#
# Вторая половина блока 5: гейт запирает работу НА ВРЕМЯ сборки, а эти клетки
# судят, что останется ПОСЛЕ неё. Считает настоящая `_artifacts_to_delete` —
# не наше представление о ней.

from app.api.rollback import _artifacts_to_delete                  # noqa: E402

#: Цель отката кнопок, ведущих к пересборке: «Сборка схемы» →
#: `validated_junctions`, «Проверка узлов» → `detected_junctions`. Обе РАНЬШЕ
#: `BUILT`, то есть граф после них собирается заново.
REBUILD_TARGETS = (DiagramStatus.VALIDATED_JUNCTIONS,
                   DiagramStatus.DETECTED_JUNCTIONS)


@pytest.mark.parametrize("target", REBUILD_TARGETS, ids=lambda s: s.value)
def test_preserve_ocr_keeps_the_raw_and_kills_the_binding(target):
    """Решение Максима: сырой OCR живёт, привязка гибнет.

    Утверждается РАЗНИЦА внутри одного флага, а не «флаг работает»: до блока 5
    `preserve_ocr` сохранял и `OCR_BINDING`, то есть после пересборки на узлах
    нового поколения висели KKS от старых `node_id` — и никто об этом не знал.
    """
    doomed = set(_artifacts_to_delete(target, preserve_ocr=True))

    assert ArtifactType.OCR_RESULT not in doomed, "сырой OCR снесён"
    assert ArtifactType.OCR_CLEANED not in doomed, "чистый OCR снесён"
    # Решение №4 редтима: ключи `block_N` от СЫРЫХ блоков, а не от узлов.
    assert ArtifactType.OCR_VALIDATION not in doomed, "правки текстов снесены"
    assert ArtifactType.OCR_BINDING in doomed, (
        "привязка пережила пересборку — KKS повиснут на чужих узлах")


@pytest.mark.parametrize("target", REBUILD_TARGETS, ids=lambda s: s.value)
def test_rebuild_kills_the_contours(target):
    """Контуры пересборку не переживают: они вливаются в граф по IoU молча."""
    doomed = set(_artifacts_to_delete(target, preserve_ocr=True,
                                      preserve_contours=False))
    assert ArtifactType.CONTOURS_AUTO in doomed
    assert ArtifactType.CONTOURS_VALIDATED in doomed


def test_preserve_contours_still_saves_them_where_it_should():
    """Порог заперт с другой стороны: сам флаг не сломан (пункт 5-1).

    «Переделать OCR» (цель `validated_graph`) контуры по-прежнему бережёт —
    их считают поточечно руками, и к распознаванию они отношения не имеют.
    """
    doomed = set(_artifacts_to_delete(DiagramStatus.VALIDATED_GRAPH,
                                      preserve_contours=True))
    assert ArtifactType.CONTOURS_VALIDATED not in doomed
    assert ArtifactType.OCR_RESULT in doomed, "«Переделать OCR» не снёс старый OCR"


# ── файловая половина сброса контуров (возврат ревизии связки 3+5) ────────
#
# «Контуры сбрасываются» было выполнено ТОЛЬКО в БД: строка снята, а
# `contours_validated.json` и `contours_auto.json` остались лежать — и все ТРИ
# потребителя читают их ПО ПУТИ, мимо строки (`worker/tasks/layout.py`,
# `app/services/layout_dispatch.py`, `worker/tasks/graph.py`). Влив идёт по IoU,
# то есть контуры прошлого поколения садились на узлы нового молча.
#
# Файловых клеток у контуров не было НИ ОДНОЙ — судилась только таблица типов.

from app.api.rollback import purge_artifacts                    # noqa: E402


class _PurgeDB:
    """Поверхность сессии для `purge_artifacts`: только `execute` с DELETE."""

    class _Res:
        rowcount = 1

    async def execute(self, stmt):
        return self._Res()


def _contours_on_disk(storage):
    d = storage / str(UID) / "contours"
    return sorted(p.name for p in d.glob("contours_*.json"))


@pytest.mark.parametrize("target", REBUILD_TARGETS, ids=lambda s: s.value)
def test_rebuild_takes_the_contour_files_off_the_disk(target, storage):
    """Строка и ФАЙЛ сносятся вместе — иначе конвейер читает сироту.

    Утверждается РАЗНИЦА: до вызова файлы лежат, после — их нет. Тот же класс,
    что `graph_canvas.json` до pains-3, и то же лекарство.
    """
    assert _contours_on_disk(storage) == ["contours_auto.json",
                                          "contours_validated.json"], "стенд пуст"

    doomed = _artifacts_to_delete(target, preserve_ocr=True,
                                  preserve_contours=False)
    asyncio.run(purge_artifacts(UID, doomed, _PurgeDB()))

    assert _contours_on_disk(storage) == [], (
        "контуры прошлого поколения остались на диске — их вольют по IoU "
        "в новый граф")


def test_preserved_contours_stay_on_the_disk(storage):
    """Порог заперт с другой стороны: «Переделать OCR» файлы НЕ трогает.

    Иначе «сносить файл вместе со строкой» вылечилось бы «сносить всегда»,
    и пункт 5-1 (контуры считают поточечно руками) был бы отменён молча.
    """
    doomed = _artifacts_to_delete(DiagramStatus.VALIDATED_GRAPH,
                                  preserve_contours=True)
    asyncio.run(purge_artifacts(UID, doomed, _PurgeDB()))

    assert _contours_on_disk(storage) == ["contours_auto.json",
                                          "contours_validated.json"]


def test_layout_dispatch_does_not_see_the_old_contours(storage):
    """Клетка ревизора: после пересборки диспетчер раскладки контуров НЕ видит.

    Судится не наше представление о читателе, а САМ читатель —
    `_validated_contours` из `app/services/layout_dispatch.py`, которая ходит
    ПО ПУТИ и мимо БД. Ради неё дефект и чинился: сноси мы только строку,
    эта функция продолжала бы отдавать полигоны прошлого поколения, а влив
    идёт по IoU.

    Фикстура несёт НАСТОЯЩИЙ подтверждённый полигон: с пустым списком узлов
    читатель отдавал бы `[]` и до правки, и после, — клетка была бы слепа.
    """
    from app.services import layout_dispatch

    (storage / str(UID) / "contours" / "contours_validated.json").write_text(
        json.dumps({"nodes": [{
            "ann_id": 1,
            "polygon_validated": [10, 10, 40, 10, 40, 40, 10, 40],
            "status": "approved",
        }]}), encoding="utf-8")

    before = layout_dispatch._validated_contours(UID)
    assert before, "стенд пуст — читатель не увидел контуров ДО отката"

    doomed = _artifacts_to_delete(DiagramStatus.VALIDATED_JUNCTIONS,
                                  preserve_ocr=True, preserve_contours=False)
    asyncio.run(purge_artifacts(UID, doomed, _PurgeDB()))

    assert layout_dispatch._validated_contours(UID) == [], (
        "диспетчер раскладки читает контуры прошлого поколения")


# ── сведение `/ocr/start` с клиентским «кнопка зелёная» ──────────────────
#
# Возврат ревизии связки 3+5. Приём тот же, что уже работает для привязки
# (`tests/test_phase_b_reentry.py::test_client_binding_threshold_matches_the_
# server_gate`), — на этом эндпоинте его просто не применили. Цена промаха
# замерена: из ОДИННАДЦАТИ статусов, при которых клиент красит кнопку
# «Распознавание текста» зелёной, ТРИ отвечали 400 уже ПОСЛЕ вопроса
# «Перезапустить распознавание?» и ответа «Да», причём по-английски.

#: Клиентское множество «кнопка зелёная», снятое чтением обеих веток
#: `_apply_status`. Литерал держит набор pains-3 — здесь он ПОВТОРЁН, а не
#: импортирован: две независимые копии ловят правку одной из них.
CLIENT_GREEN = {
    "building_graph", "built", "validating_graph", "validated_graph",
    "extracting_contours", "contours_extracted", "contours_validated",
    "ocr_completed", "ocr_bound", "generating_fxml", "completed",
}

#: Единственное НАМЕРЕННОЕ расхождение: во время сборки перезапуск запрещён
#: (эндпоинт первым делом сносит `OCR_RESULT`, а сборка ждёт текстовые блоки).
#: Пока кнопка не погашена — отказ обязан быть по-русски.
GREEN_BUT_REFUSED = {"building_graph"}


def test_client_green_button_matches_the_ocr_start_gate():
    """Зелёная кнопка и белый список сервера — одно и то же, кроме одной клетки.

    Перебор ведётся ПОЛНЫМ клиентским множеством, а не выборкой.
    """
    from app.api.ocr import _OCR_START_STATUSES

    server = {s.value for s in _OCR_START_STATUSES}
    refused = {s for s in CLIENT_GREEN if s not in server}
    assert refused == GREEN_BUT_REFUSED, (
        f"кнопка зелёная, а сервер отвечает 400 на: {sorted(refused)}")


def test_the_client_green_set_is_the_real_one():
    """Сторож копии: клиентский литерал тот же, что у набора pains-3.

    Без него две копии разъедутся молча, и сведение выше начнёт сверять
    сервер сам с собой.
    """
    from tests.ui.test_ocr_restart_asks import ALL_GREEN
    assert set(ALL_GREEN) == CLIENT_GREEN


@pytest.mark.parametrize("status", sorted(GREEN_BUT_REFUSED))
def test_the_intentional_refusal_speaks_russian(status, layout, broker):
    """Оператор читает отказ ПОСЛЕ «Да» на вопрос о потере результата.

    Утверждается НАШЕ решение (русский текст и никакой отправки в брокер), а не
    факт четырёхсотки: прежняя английская строка про 'validated_junctions'
    прилетала в модалку клиента как есть.
    """
    with pytest.raises(HTTPException) as exc:
        _run("ocr_start", _diagram(status))
    detail = str(exc.value.detail)
    assert exc.value.status_code == 400
    assert "Cannot start OCR" not in detail, detail
    assert "Распознавание" in detail, detail
    assert broker == [], "отказ всё равно поставил задачу в очередь"


# ── черновики: общий tmp у двух писателей из трёх ────────────────────────

def _tmp_writers():
    """Все места, где строится имя черновика: (файл, аргумент `with_suffix`).

    Аргумент вытаскивается регуляркой по ВСЕМУ тексту, а не построчно: у
    писателя FXML вызов разнесён на две строки, и построчный перебор его
    не видел (поймано собственным сторожем перебора).
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    out = []
    for rel in ("app/api/ocr.py", "app/services/ocr_graph_merge.py",
                "worker/tasks/graph.py"):
        src = (repo / rel).read_text(encoding="utf-8")
        for arg in re.findall(r"with_suffix\(\s*(.*?)\)\s*$",
                              src, re.S | re.M):
            arg = " ".join(arg.split())
            if ".tmp" in arg:
                out.append((rel, arg))
    return out


def test_every_tmp_writer_carries_the_pid():
    """Все писатели черновиков несут PID в имени — перебор ГРЕПОМ, не выборкой.

    Правило записано у третьего писателя (`worker/tasks/graph.py`): «Суффикс
    PID обязателен: общий tmp просто перенёс бы гонку на шаг раньше — второй
    писатель обрезал бы черновик первого, и `os.replace` положил бы поверх цели
    уже испорченный файл». Обстановка для гонки есть: `acks_late` и
    `worker_concurrency=2` (`worker/celery_app.py`).

    Возврат ревизии связки 3+5: двое из трёх писателей правило не соблюдали, и
    ни один набор этого не видел — сторожа на популяцию не было вовсе.
    """
    without = [f"{rel}: {arg}" for rel, arg in _tmp_writers()
               if "getpid" not in arg]
    assert without == [], f"черновики без PID в имени: {without}"


def test_the_grep_really_sees_the_writers():
    """Сторож самого сторожа: перебор находит ВСЕ три места, а не ноль.

    Без этой клетки правило выше зеленело бы и на пустом списке — тот же класс,
    что «гейт судит по отсутствию наблюдения».
    """
    assert len(_tmp_writers()) == 3, (
        f"писателей черновиков найдено {len(_tmp_writers())}, а не 3")
