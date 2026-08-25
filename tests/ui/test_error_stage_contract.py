# -*- coding: utf-8 -*-
"""Пункт 1.x11 дороги — контракт `error_stage`: ДВА МНОЖЕСТВА обязаны сходиться.

Дефект (найден ревизией связки 1.12+1.14, `BOARD §78`). `app/api/cvat.py:374`
пишет `error_stage="fetching_annotations"`. СЕРВЕР это значение понимает —
`app/api/diagrams.py:453` возвращает по нему диаграмму в `VALIDATING_BBOX`.
КЛИЕНТ не понимает: в `ui/**` этой строки не было вовсе, поэтому в фолбэк-ветке
`_update_buttons` (стадии из `/stages` недоступны) `_error_key = None`, а в
статусе `ERROR` ни одна из 13 кнопок не попадает ни в `available`, ни в
`completed`, ни в `processing` (`ERROR` не входит в `_STATUS_ORDER`). Итог —
ноль кнопок из тринадцати: оператор видит серый столбец и не может ни повторить,
ни откатить.

Почему проверяется не список, а СОВПАДЕНИЕ МНОЖЕСТВ. Решётки ноги 1.12
(`test_direction_retry_deadend.py`) перебирают только ВОРКЕРНЫЕ значения —
литералы `set_diagram_error`. Писатели со стороны API идут мимо этого перебора,
поэтому весь набор оставался зелёным при живом тупике в дереве. Здесь множество
писателей снимается `ast`-разбором `app/**` и `worker/**` при каждом прогоне:
новый писатель попадает в перебор сам, а не после того, как о нём вспомнят.

Второе расхождение, найденное шагом 2 этого же пункта и к первому не сводимое:
значение вообще не доезжало до клиента на пути ОТКРЫТИЯ диаграммы. `DiagramInfo`
(ответ `get_diagram`) поля `error_stage` не имел, а `_refresh_status` берёт его
оттуда через `getattr(..., None)` — то есть молча получал `None` ВСЕГДА. Полинг
(`get_status` → `DiagramStatusInfo`) поле несёт, но `load_diagram` слежение не
включает: у диаграммы, открытой уже сломанной, опрашивать нечего. Поэтому
харнесс собирает `DiagramInfo` НАСТОЯЩИМ `APIClient.get_diagram` из тела ответа
сервера — фальшивка с лишним полем показала бы зелёное там, где у оператора
пусто (ровно так и вышло у решёток 1.12).

Гейты сервера судятся НАСТОЯЩИМИ корутинами (`retry_operation`,
`reopen_bbox_validation`) — не моим представлением о том, что они пускают:
образец `tests/ui/test_direction_retry_deadend.py`, нога 1.12.

⚠ Модальные диалоги пути подменены (`PROTOCOL §5`): без подмены красный прогон
не падает, а ВИСНЕТ на `QMessageBox`, и на CI это выглядит как «долго».

⚠ Уборка своих виджетов — точечная (`sendPostedEvents(DeferredDelete)`), а не общим
`processEvents()`. Замер §85: те же 72 теста шли 3.5 с в одиночку и 201.9 с после
13 соседних файлов — общий `processEvents()` в моём teardown выпивал очередь ЧУЖИХ
отложенных удалений (набор 1.12 свои воркспейсы не сносит вовсе). Оборотная сторона
правила 1-32: кто крутит очередь событий, тот и платит — временем или чужим сегфолтом.
"""
import ast
import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from fastapi import HTTPException                          # noqa: E402
from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

from app.api.cvat import reopen_bbox_validation as srv_reopen   # noqa: E402
from app.api.diagrams import retry_operation as srv_retry       # noqa: E402
from app.models import Diagram                             # noqa: E402
from app.models import DiagramStatus as SrvStatus          # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import (                       # noqa: E402
    APIClient, APIError, DiagramStatus, DiagramStatusInfo,
)

UID = "d74eb9f1-1111-2222-3333-444455556666"
ERROR_TEXT = "CVAT вернул 502 при выгрузке аннотаций"
ROOT = Path(__file__).resolve().parents[2]

# Абсолютные числа обоих множеств на момент пункта (2026-08-19). Новое значение
# обязано пройти через решётку, а не проскочить мимо неё молча.
API_WRITERS = {"fetching_annotations"}
WORKER_WRITERS = {
    "building_graph", "contour_extraction", "detecting", "detecting_junctions",
    "direction_classification", "generating_fxml", "ocr", "segmenting",
    # `skeletonizing_final` вместо `skeletonizing_simple` с 2026-08-25 (боль 1,
    # Б16): задача финальной скелетизации называет СВОЙ этап, а не соседний.
    "skeletonizing", "skeletonizing_final",
}
BUTTON_COUNT = 13
# Понимает клиент, но не пишет никто: `validating_graph` — это ЗНАЧЕНИЕ СТАТУСА
# (`app/models/diagram.py:64`), в `error_stage` его не кладёт ни один писатель.
# Лишняя клетка карты безвредна, но зафиксирована: реестр стадий (5-4) обязан
# знать, что она держится ни на чём.
# `skeletonizing_simple` — вторая такая клетка с 2026-08-25 (pains-1, боль 1):
# писатель переехал на `skeletonizing_final`, а прежнее значение осталось в карте
# клиента ЛЕГАСИ — им помечены строки `error_stage` у диаграмм, сломавшихся до
# правки, и снять клетку можно только вместе с этими строками в БД.
CLIENT_ONLY = {"validating_graph", "skeletonizing_simple"}
# Динамические писатели: значение считается в рантайме, перечислить его нельзя.
# Оба живут в `worker/utils/db_helpers.py` (:36 — параметр `set_diagram_error`,
# :146 — `task_name.split('.')[-1]` в мёртвом сегодня `safe_dispatch`).
# Появится третий — множество перестанет быть перечислимым, и решётка обязана
# об этом сказать.
DYNAMIC_WRITERS = 2


# ── множества: снимаются с кода, а не переписываются руками ──────────────

def _written_error_stages(package: str):
    """Значения `error_stage`, которые ПИШЕТ пакет: (константы, динамические).

    Считает три формы ЗАПИСИ: `x.error_stage = "..."`; четвёртый аргумент
    `set_diagram_error(db, uid, msg, "...")`; именованный аргумент у записи
    в БД (`Diagram(...)`, `.update(...)`, `.values(...)`). Именованный аргумент
    у ЧТЕНИЯ не считается: `DiagramStatusResponse(error_stage=row.error_stage)`
    (`app/api/diagrams.py:306`) — это ответ клиенту, а не запись.
    """
    const, dynamic = {}, []
    for path in sorted((ROOT / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        addr = f"{path.relative_to(ROOT).as_posix()}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "error_stage":
                        values.append(node.value)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("Diagram", "update", "values"):
                    for kw in node.keywords:
                        if kw.arg == "error_stage":
                            values.append(kw.value)
                if name == "set_diagram_error":
                    stage = node.args[3] if len(node.args) >= 4 else None
                    for kw in node.keywords:
                        if kw.arg == "stage":
                            stage = kw.value
                    if stage is not None:
                        values.append(stage)
            for value in values:
                if isinstance(value, ast.Constant):
                    if isinstance(value.value, str):
                        const.setdefault(value.value, []).append(f"{addr}:{node.lineno}")
                    continue          # `= None` — сброс, а не значение
                dynamic.append(f"{addr}:{node.lineno}")
    return const, dynamic


def client_fallback_map():
    """Карта `_STAGE_TO_KEY` — то, что клиент понимает в фолбэк-ветке.

    Карта локальна для `_update_buttons`, поэтому снимается разбором, а не
    импортом: иначе решётка судила бы по своей копии, а не по коду клиента.
    """
    source = (ROOT / "ui/widgets/diagram_workspace.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STAGE_TO_KEY":
                    return {k.value: v.value for k, v in
                            zip(node.value.keys, node.value.values)}
    raise AssertionError("_STAGE_TO_KEY не найдена в diagram_workspace.py")


API_CONST, API_DYNAMIC = _written_error_stages("app")
WORKER_CONST, WORKER_DYNAMIC = _written_error_stages("worker")
WRITTEN = sorted(set(API_CONST) | set(WORKER_CONST))
# Полный перебор фолбэка: всё, что пишут, плюс отсутствие значения, плюс
# заведомо чужая строка — будущий писатель, которого сегодня нет.
FALLBACK_CASES = WRITTEN + [None, "totally_unknown_stage"]


# ── поддельный сервер: гейты настоящие ───────────────────────────────────

class _FakeResult:
    def __init__(self, obj=None):
        self._obj = obj
        self.rowcount = 0

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDB:
    def __init__(self, diagram):
        self.diagram = diagram
        self.commits = 0

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

    async def commit(self):
        self.commits += 1


class FakeServer:
    """Одна диаграмма в БД. Пускать или нет — решают настоящие корутины."""

    def __init__(self, status, error_stage=None, error_message=ERROR_TEXT):
        self.diagram = Diagram()
        self.diagram.uid = uuid.UUID(UID)
        self.diagram.number = 7
        self.diagram.project_code = "thermohydraulics"
        self.diagram.original_filename = "shema.png"
        self.diagram.status = status
        self.diagram.error_stage = error_stage
        self.diagram.error_message = error_message
        self.diagram.cvat_task_id = 42
        self.diagram.cvat_job_id = 77
        self.retries = 0

    def payload(self):
        """Тело ответа `GET /api/diagrams/{uid}` — поля `DiagramResponse`."""
        return {
            "uid": UID,
            "number": self.diagram.number,
            "project_code": self.diagram.project_code,
            "original_filename": self.diagram.original_filename,
            "status": self.diagram.status.value,
            "error_message": self.diagram.error_message,
            "error_stage": self.diagram.error_stage,
            "cvat_task_id": self.diagram.cvat_task_id,
            "cvat_job_id": self.diagram.cvat_job_id,
        }

    def _call(self, coroutine):
        try:
            return asyncio.run(coroutine(uuid.UUID(UID), db=_FakeDB(self.diagram)))
        except HTTPException as exc:
            # `APIClient._request` превращает ответ >= 400 ровно в это.
            raise APIError(str(exc.detail), exc.status_code) from None

    def retry(self):
        """`POST /api/diagrams/{uid}/retry` — настоящая корутина."""
        self.retries += 1
        return self._call(srv_retry)

    def reopen(self):
        """`POST /api/cvat/{uid}/reopen-bbox-validation` — настоящая корутина."""
        return self._call(srv_reopen)


def real_diagram_info(payload):
    """`DiagramInfo` собран НАСТОЯЩИМ клиентом из тела ответа сервера.

    Через `__new__` — чтобы не поднимать httpx-соединение: `get_diagram`
    пользуется только `_request`.
    """
    client = APIClient.__new__(APIClient)
    client._request = lambda *a, **kw: payload
    return APIClient.get_diagram(client, UID)


class FakeAPI:
    """Поверхность `APIClient`, которой воркспейс пользуется в сценарии."""

    def __init__(self, server, stages, stages_error=False):
        self.server = server
        self._stages = stages
        self._stages_error = stages_error

    def get_diagram(self, uid):
        return real_diagram_info(self.server.payload())

    def get_stages(self, uid):
        if self._stages_error:
            raise APIError("stages unavailable", 503)
        return list(self._stages)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_stage_durations(self):
        return {}

    def retry_operation(self, uid):
        return self.server.retry()


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


class FakeMsgBox:
    """Подмена модалки: висеть на ней тест не должен (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[2] if len(args) > 2 else ""))
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok


def stage_row(stage_type, status="failed"):
    """Строка `ProcessingStage`, как её отдаёт `/stages`."""
    started = datetime(2026, 8, 19, 10, 0, 0).isoformat()
    return {
        "id": 1,
        "stage_type": stage_type,
        "status": status,
        "attempt": 1,
        "error_message": ERROR_TEXT,
        "started_at": started,
        "created_at": started,
    }


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    made = []

    def _make(status, error_stage=None, stages=None, stages_error=False):
        server = FakeServer(status, error_stage=error_stage)
        ws = dw.DiagramWorkspace(FakeAPI(server, stages or [], stages_error),
                                 FakeStatusProvider())
        # Воркспейс показан по-настоящему (offscreen): у скрытого родителя
        # `isVisible()` ребёнка ложно False, и вопрос «видит ли оператор
        # кнопку» подменился бы вопросом «выставлен ли флаг».
        ws.show()
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        return ws, server

    yield _make

    # Свои виджеты набор сносит сам, детерминированно: брошенные на сборщик
    # мусора детонируют в `processEvents` СОСЕДНЕГО теста (`PROTOCOL §5`,
    # замер 1-32). Выпиваем РОВНО отложенные удаления, а не всю очередь:
    # общий `processEvents()` здесь оплачивал чужой мусор — с соседними
    # наборами тот же самый набор шёл 3.5 с в одиночку и 200 с в компании
    # (замер §85).
    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def enabled_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items() if b.isEnabled())


def red_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items()
                  if b.styleSheet() == dw._BTN_STYLE_RED)


def exits(ws):
    """Всё, что оператор может нажать: 13 кнопок этапов + страховочная.

    Страховка берётся через `getattr`: до правки её в воркспейсе нет вовсе,
    и решётка обязана показать это как «нажать нечего», а не как
    `AttributeError` о самом стенде.
    """
    keys = enabled_keys(ws)
    btn = getattr(ws, "btn_error_retry", None)
    if btn is not None and btn.isVisible() and btn.isEnabled():
        keys = keys + ["<страховка>"]
    return keys


# ── часть 1: два множества ───────────────────────────────────────────────

def test_api_writers_are_exactly_one():
    """`app/**` пишет РОВНО одно значение — и оно из `app/api/cvat.py`."""
    assert set(API_CONST) == API_WRITERS
    assert API_CONST["fetching_annotations"] == ["app/api/cvat.py:466"]


def test_worker_writers_are_exactly_ten():
    """Воркерные значения — те самые десять, что перебирала нога 1.12."""
    assert set(WORKER_CONST) == WORKER_WRITERS
    assert len(WORKER_CONST) == 10


def test_dynamic_writers_are_two_and_both_in_worker():
    """Перечислимость множества — сама по себе утверждение, и оно проверяется.

    Появится динамический писатель со стороны API — перебор ниже станет
    неполным молча, и об этом обязана сказать решётка, а не оператор.
    """
    assert len(WORKER_DYNAMIC) == DYNAMIC_WRITERS, WORKER_DYNAMIC
    assert API_DYNAMIC == [], API_DYNAMIC


def test_client_understands_every_written_value():
    """ГЛАВНАЯ решётка пункта: множества обязаны совпадать.

    Кто пишет ⊆ кто понимает. До правки не выполнялось на одном значении —
    `fetching_annotations`, и это давало ноль кнопок из 13.
    """
    understood = set(client_fallback_map())
    assert set(WRITTEN) - understood == set(), (
        "сервер пишет значение, которого клиент не знает — тупик в фолбэке"
    )


def test_client_map_has_exactly_one_value_nobody_writes():
    """Порог с другой стороны: карта не обрастает клетками ни на чём."""
    understood = set(client_fallback_map())
    assert understood - set(WRITTEN) == CLIENT_ONLY


# ── часть 2: тупик закрыт на всём множестве ──────────────────────────────

@pytest.mark.parametrize("stage", FALLBACK_CASES, ids=lambda s: s or "none")
def test_every_error_leaves_at_least_one_exit(stage, bench):
    """Гейт пункта. Стадии недоступны, значение любое — выход есть всегда."""
    ws, _ = bench(SrvStatus.ERROR, stage, stages=[])
    assert exits(ws), (
        f"error_stage={stage!r}: ноль кнопок из {BUTTON_COUNT} — оператор в тупике"
    )


KNOWN = sorted(set(WRITTEN) & set(client_fallback_map()))


@pytest.mark.parametrize("stage", KNOWN, ids=lambda s: s)
def test_known_value_paints_exactly_one_red_button(stage, bench):
    """Известное значение — ровно одна красная кнопка и она же единственная.

    Порог с двух сторон: правка не имеет права раздать кнопки веером там,
    где стадия названа точно.
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stages=[])
    expected = client_fallback_map()[stage]
    assert red_keys(ws) == [expected]
    assert enabled_keys(ws) == [expected]
    assert not ws.btn_error_retry.isVisible(), (
        "страховка вылезла там, где этап назван точно — две двери вместо одной"
    )


def test_fetching_annotations_opens_the_cvat_button(bench):
    """Живой тупик пункта: `fetching_annotations` → «Проверка элементов».

    Кнопка выбрана не на глаз: сервер по этому значению возвращает диаграмму
    в `VALIDATING_BBOX` (`app/api/diagrams.py:453`), а `cvat_validation` —
    та же кнопка в карте основного пути (`_STAGE_TYPE_TO_KEY`).
    """
    ws, _ = bench(SrvStatus.ERROR, "fetching_annotations", stages=[])
    btn = ws._action_buttons["cvat"]
    assert btn.isEnabled(), "выход из упавшей выгрузки аннотаций закрыт"
    assert btn.styleSheet() == dw._BTN_STYLE_RED
    assert btn.text().startswith("🔄")


def test_server_accepts_the_cvat_exit_from_this_error():
    """И этот выход не фальшивый: настоящая корутина его пускает.

    `reopen_bbox_validation` — дверь кнопки «Проверка элементов» с любого
    позднего этапа (`_open_cvat`, ветка else). Из `ERROR` она обязана
    пускать, иначе красная кнопка вела бы в 409.
    """
    server = FakeServer(SrvStatus.ERROR, "fetching_annotations")
    result = server.reopen()
    assert result["status"] == "validating_bbox"
    assert server.diagram.status is SrvStatus.VALIDATING_BBOX
    assert server.diagram.error_stage is None
    assert server.diagram.error_message is None


@pytest.mark.parametrize("stage", [None, "totally_unknown_stage"],
                         ids=["none", "unknown"])
def test_unknown_value_shows_the_error_and_offers_the_retry(stage, bench):
    """Неизвестное значение: 0 из 13 — но оператор видит ошибку и выход.

    Клиент перестаёт гадать: решение, куда откатывать, отдаётся серверу —
    у него для этого своя карта (`app/api/diagrams.py:448`).
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stages=[])
    messages = []
    ws.status_message.connect(lambda text, ms: messages.append(text))
    ws._refresh_status()

    assert enabled_keys(ws) == [], "неизвестная стадия не имеет права красить кнопку"
    assert ws.btn_error_retry.isVisible(), "тупик остался: нажать нечего"
    assert ws.btn_error_retry.isEnabled()
    assert ERROR_TEXT in ws.btn_error_retry.toolTip(), (
        "оператор не видит, что именно сломалось"
    )
    assert any(ERROR_TEXT in m for m in messages), (
        f"ошибка не показана оператору: {messages}"
    )


def test_operator_escapes_by_the_safety_button(bench):
    """Выход работает: клик → сервер вернул диаграмму на рабочий шаг.

    Судит настоящая корутина `retry_operation`, а не моё представление о ней.
    """
    ws, server = bench(SrvStatus.ERROR, "totally_unknown_stage", stages=[])
    assert ws.btn_error_retry.isVisible()

    ws.btn_error_retry.click()

    assert server.retries == 1, "клик не дошёл до сервера"
    assert server.diagram.status is not SrvStatus.ERROR, (
        f"диаграмма осталась в ошибке: {server.diagram.status.value}"
    )
    assert server.diagram.error_stage is None
    assert server.diagram.error_message is None
    assert enabled_keys(ws), "после выхода из ошибки кнопок всё ещё нет"
    assert not ws.btn_error_retry.isVisible(), "страховка не убралась после выхода"


def test_operator_can_refuse_the_retry(bench):
    """Отказ в диалоге — сервер не тронут: откат по кнопке спрашивают."""
    ws, server = bench(SrvStatus.ERROR, "totally_unknown_stage", stages=[])
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    ws.btn_error_retry.click()

    assert server.retries == 0, "откат ушёл на сервер без согласия оператора"
    assert server.diagram.status is SrvStatus.ERROR
    assert [c for c in FakeMsgBox.calls if c[0] == "question"], "вопроса не было"


@pytest.mark.parametrize("status", list(SrvStatus), ids=lambda s: s.value)
def test_safety_button_stays_out_of_normal_work(status, bench):
    """Страховка живёт РОВНО в одной клетке машины — в `error` без стадии.

    Тридцать остальных статусов её видеть не должны: иначе кнопка отката
    висела бы над нормальной работой.
    """
    ws, _ = bench(status, None, stages=[])
    if status is SrvStatus.ERROR:
        assert ws.btn_error_retry.isVisible()
    else:
        assert not ws.btn_error_retry.isVisible(), (
            f"страховка вылезла в статусе {status.value}"
        )
        assert enabled_keys(ws), f"статус {status.value} осиротел без ошибки"


def test_stages_path_is_untouched(bench):
    """Порог: путь со стадиями работает по-старому, страховка в него не лезет.

    `/stages` доступен → упавший этап красит бусину и кнопку сам
    (механизм ноги 1.12), и страховке там делать нечего.
    """
    ws, _ = bench(SrvStatus.ERROR, "fetching_annotations",
                  stages=[stage_row("cvat_validation", "failed")])
    assert sorted(ws._stage_errors) == ["cvat"]
    assert red_keys(ws) == ["cvat"]
    assert not ws.btn_error_retry.isVisible()


def test_broken_stages_endpoint_still_leaves_an_exit(bench):
    """`/stages` отвечает ошибкой — фолбэк обязан работать так же."""
    ws, _ = bench(SrvStatus.ERROR, "totally_unknown_stage",
                  stages=[], stages_error=True)
    assert exits(ws) == ["<страховка>"]


# ── часть 3: транспорт — значение обязано доехать до клиента ─────────────

def test_get_diagram_carries_error_stage():
    """`GET /api/diagrams/{uid}` несёт `error_stage` до клиента.

    Сервер его отдаёт (`DiagramResponse.error_stage`), а клиент терял:
    поля не было в `DiagramInfo` вовсе, и `_refresh_status` получал `None`
    через `getattr(..., None)` — молча, при любом значении на сервере.
    """
    info = real_diagram_info(
        FakeServer(SrvStatus.ERROR, "fetching_annotations").payload())
    assert info.status is DiagramStatus.ERROR
    assert info.error_stage == "fetching_annotations"
    assert info.error_message == ERROR_TEXT


def test_poll_path_delivers_the_value_too(bench):
    """Вторая дверь — полинг: `DiagramStatusInfo` поле несёт и доносит."""
    ws, _ = bench(SrvStatus.VALIDATED_BBOX, None, stages=[])
    ws._on_status_updated(UID, DiagramStatusInfo(
        status=DiagramStatus.ERROR,
        error_message=ERROR_TEXT,
        error_stage="fetching_annotations",
    ))
    assert red_keys(ws) == ["cvat"]


def test_opening_a_broken_diagram_paints_the_red_button(bench):
    """Путь оператора целиком: открыть сломанную диаграмму из списка.

    Слежение `load_diagram` не включает (полинг у стоящей диаграммы
    опрашивать нечего), поэтому единственный источник — `get_diagram`.
    """
    ws, _ = bench(SrvStatus.ERROR, "detecting", stages=[])
    assert red_keys(ws) == ["detect"], (
        "открытие сломанной диаграммы не даёт выхода — значение потерялось "
        "по дороге от сервера к клиенту"
    )


# ── сторож самого стенда ─────────────────────────────────────────────────

def test_bench_judges_by_real_endpoint():
    """Стенд судит настоящей корутиной, а не своей копией гейта."""
    server = FakeServer(SrvStatus.BUILT, None)
    with pytest.raises(APIError) as exc:
        server.retry()
    assert exc.value.status_code == 400
    assert "built" in exc.value.message
