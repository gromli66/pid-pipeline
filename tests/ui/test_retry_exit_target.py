# -*- coding: utf-8 -*-
"""Нога 1.15 пункта 1-13 — выход из тупика не имеет права уводить в начало.

Дефект. Оператор, у которого не осталось ни одной из 13 кнопок, выходит
страховкой «🔄 Повторить» (пункт 1-36): клиент не гадает, куда откатывать,
а спрашивает сервер — `POST /api/diagrams/{uid}/retry`. Сервер отвечает картой
`error_stage → предыдущий статус`, а четырёх ЗАПИСЫВАЕМЫХ значений в этой карте
нет (`direction_classification`, `ocr`, `contour_extraction`, `generating_fxml`),
и они уходят в дефолт `UPLOADED`. В `uploaded` у оператора остаётся ровно одна
кнопка — «Очистка рамки»: детекцию, CVAT-валидацию и валидацию масок (всё ручное)
надо проходить заново, хотя артефакты лежат на диске нетронутыми.

Почему тест сценарный, а не юнит. Дефект живёт на ШВЕ «страховка клиента → карта
сервера»: и кнопка права, и карта сама по себе непротиворечива. Поэтому здесь
живой `DiagramWorkspace` с настоящими сигналами Qt, а откат судит **настоящая**
корутина `app.api.diagrams.retry_operation` (решётку самой карты держит
`tests/test_retry_target_table.py`).

Обстановка тупика взята не из головы, а замерена (MEASUREMENTS §98): страховка
показывается, когда `/stages` недоступен И в памяти клиента лежит ещё не
протухшая БЕГУЩАЯ строка той самой стадии — она держит кнопку в `processing`,
то есть гасит её раньше, чем фолбэк успевает покрасить в красный. Это ровно та
последовательность, что бывает в бою: строку приносит опрос провайдера, пока
стадия ещё шла, а к моменту её падения `/stages` моргнул.

⭐ Доработка по возврату ревизии связки (§104.12, пересъём §107.2): к проверке
«в целевом статусе дверь есть» добавлен СКВОЗНОЙ сценарий — тот же воркспейс,
та же бегущая строка, реальный клик, и только потом вопрос про кнопки. Прежняя
редакция спрашивала про целевой статус на СВЕЖЕМ воркспейсе с чистыми стадиями,
где предусловие лекарства выполнено по построению («свежий объект», `PROTOCOL §3`),
и потому не видела второй половины дефекта: у двух значений из четырёх дверь
глушила та же бегущая строка, что создала тупик, — до 600 с.

⭐ Вторая доработка, решение Максима (§107.9–107.10): глушение ПОЧИНЕНО, а не
оставлено границей. `_on_error_retry` чистит `_last_stages` — сервер откатил
статус, значит снимок стадий в памяти клиента описывает состояние, которого
больше нет. Замер §107.9 показал, что это безопасно: на сервере строка упавшей
стадии уже `failed`, врал только снимок. Три порога стерегут форму починки:
дверь открыта сразу после клика · настоящая бегущая стадия по-прежнему гасит
свою кнопку · новая строка следующего опроса гасит её снова.

⚠ Модалка `QMessageBox.question` из `_on_error_retry` подменена с утверждением
о ФАКТЕ вызова (`PROTOCOL §5`): без подмены красный прогон не падает, а виснет.
Виджеты набор сносит сам, детерминированно, — брошенные на сборщик мусора
детонируют в `processEvents` соседнего набора (замер 1-32, §85ж).
"""
import os
import uuid
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox     # noqa: E402

import ui.widgets.diagram_workspace as dw                   # noqa: E402
from app.models import DiagramStatus as SrvStatus           # noqa: E402
from ui.services.api_client import APIClient                # noqa: E402

from tests.test_retry_target_table import (                 # noqa: E402
    ERROR_TEXT, FakeDB, call_retry, make_diagram,
)

UID = "d74eb9f1-1111-2222-3333-444455556666"

# Четыре значения ноги: что пишет воркер · `stage_type` его строки стадии ·
# статус, на который обязан вернуть сервер · кнопка, которой оператор идёт
# дальше из этого статуса. Целевые статусы взяты не на глаз: они лежат
# в собственной карте клиента `_ROLLBACK_TARGET` («статус ПЕРЕД этим этапом»),
# и сторож ниже проверяет это совпадение исполнением.
CASES = [
    ("direction_classification", "direction_classification", "validated_bbox", "segment"),
    ("contour_extraction",       "contour_extraction",       "validated_graph", "contours"),
    ("ocr",                      "ocr",                      "validated_graph", None),
    ("generating_fxml",          "fxml_generation",          "ocr_bound",       "edit_graph"),
]
IDS = [c[0] for c in CASES]


# ── поддельный клиентский сервер: гейт настоящий ─────────────────────────


class FakeServer:
    """Одна диаграмма; куда её вернуть — решает настоящая корутина."""

    def __init__(self, status, error_stage=None):
        self.diagram = make_diagram(status, error_stage)
        self.retries = 0

    def payload(self):
        return {
            "uid": UID,
            "number": self.diagram.number,
            "project_code": self.diagram.project_code,
            "original_filename": self.diagram.original_filename,
            "status": self.diagram.status.value,
            "error_message": self.diagram.error_message,
            "error_stage": self.diagram.error_stage,
            "cvat_task_id": None,
            "cvat_job_id": None,
        }

    def retry(self):
        """`POST /api/diagrams/{uid}/retry` — настоящая корутина эндпоинта."""
        self.retries += 1
        import asyncio

        from app.api.diagrams import retry_operation

        db = FakeDB(self.diagram)
        return asyncio.run(retry_operation(uuid.UUID(UID), db=db))


def real_diagram_info(payload):
    """`DiagramInfo` собирает НАСТОЯЩИЙ клиент, а не подделка.

    Подделка бывает БОГАЧЕ клиента и делает набор слепым: у 1.12 так и вышло —
    в её `FakeDiagramInfo` поле `error_stage` было, а в настоящем клиенте нет
    (замер §85г). Через `__new__`, чтобы не поднимать httpx-соединение.
    """
    client = APIClient.__new__(APIClient)
    client._request = lambda *a, **kw: payload
    return APIClient.get_diagram(client, UID)


class FakeAPI:
    def __init__(self, server, stages_error=True):
        self.server = server
        self._stages_error = stages_error

    def get_diagram(self, uid):
        return real_diagram_info(self.server.payload())

    def get_stages(self, uid):
        if self._stages_error:
            raise RuntimeError("stages unavailable")
        return []

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
    """Подмена модалки: висеть на ней набор не должен (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append("question")
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append("warning")
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append("information")
        return cls.StandardButton.Ok


def running_row(stage_type):
    """Свежая БЕГУЩАЯ строка стадии — такую приносит опрос провайдера.

    Время — текущее: `_stage_stuck` отпускает кнопку через `WAIT_LIMIT_S`,
    и строка из прошлого не гасила бы ничего.
    """
    now = datetime.utcnow().isoformat()
    return {
        "id": 1,
        "stage_type": stage_type,
        "status": "running",
        "attempt": 1,
        "error_message": None,
        "started_at": now,
        "created_at": now,
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

    def _make(status, error_stage=None, stale=None, stages_error=True):
        server = FakeServer(status, error_stage=error_stage)
        provider = FakeStatusProvider()
        ws = dw.DiagramWorkspace(FakeAPI(server, stages_error), provider)
        # Показан по-настоящему (offscreen): у скрытого родителя `isVisible()`
        # ребёнка ложно False, и вопрос «видит ли оператор кнопку» подменился
        # бы вопросом «выставлен ли флаг».
        ws.show()
        ws.load_diagram(UID, "схема оператора")
        if stale is not None:
            # Боевой порядок: строки стадий приезжают ОТДЕЛЬНЫМ каналом, пока
            # стадия ещё бежит; падение клиент видит следующим опросом статуса.
            provider.stages_updated.emit(UID, stale)
            ws._refresh_status()
        made.append(ws)
        return ws, server

    yield _make

    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def enabled_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items() if b.isEnabled())


# ── часть 1: обстановка тупика воспроизводится ───────────────────────────


@pytest.mark.parametrize("stage,stage_type,target,door", CASES, ids=IDS)
def test_operator_really_reaches_the_safety_button(stage, stage_type, target, door, bench):
    """Порог снизу: тупик, в котором страховка — единственная дверь.

    Без этого утверждения тест ниже проверял бы недостижимую клетку: «переход
    есть в машине» ≠ «путь достижим оператору» (четыре мёртвых пути этой зоны
    уже найдены ногами 1.11-1.14).
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stale=[running_row(stage_type)])

    assert enabled_keys(ws) == [], (
        f"тупика нет: при '{stage}' доступны кнопки {enabled_keys(ws)}"
    )
    assert ws.btn_error_retry.isVisible() and ws.btn_error_retry.isEnabled()
    assert ERROR_TEXT in ws.btn_error_retry.toolTip()


def test_the_same_error_without_a_running_row_keeps_its_own_button(bench):
    """Порог сверху: страховка не подменяет штатный выход.

    Та же ошибка без протухшей строки красит кнопку своего этапа, и тогда
    оператор идёт ею — `POST /segment` перезапускает цепочку С НАПРАВЛЕНИЯ
    (`app/api/segmentation.py:104-112`), никакого отката не происходит.
    """
    ws, _ = bench(SrvStatus.ERROR, "direction_classification", stale=[])

    assert "segment" in enabled_keys(ws)
    assert not ws.btn_error_retry.isVisible()


# ── часть 2: дефект ноги ─────────────────────────────────────────────────


@pytest.mark.parametrize("stage,stage_type,target,door", CASES, ids=IDS)
def test_retry_does_not_throw_the_operator_to_the_start(stage, stage_type, target, door, bench):
    """Выход страховкой возвращает на РАБОЧИЙ шаг, а не в начало конвейера."""
    ws, server = bench(SrvStatus.ERROR, stage, stale=[running_row(stage_type)])

    ws.btn_error_retry.click()

    assert "question" in FakeMsgBox.calls, "откат ушёл на сервер без вопроса"
    assert server.retries == 1, "клик не дошёл до сервера"
    assert server.diagram.status.value != "uploaded", (
        f"'{stage}': оператора вернули в САМОЕ НАЧАЛО конвейера — заново "
        f"детекция, CVAT-валидация и валидация масок"
    )
    assert server.diagram.status.value == target, (
        f"'{stage}': ушли в '{server.diagram.status.value}', "
        f"ожидался шаг перед упавшей стадией — '{target}'"
    )
    assert server.diagram.error_stage is None
    assert server.diagram.error_message is None


def test_operator_can_refuse_the_retry(bench):
    """Отказ в диалоге — сервер не тронут (порог с другой стороны)."""
    ws, server = bench(SrvStatus.ERROR, "direction_classification",
                       stale=[running_row("direction_classification")])
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    ws.btn_error_retry.click()

    assert server.retries == 0
    assert server.diagram.status is SrvStatus.ERROR
    assert server.diagram.error_stage == "direction_classification"


# ── часть 3: в целевом статусе дверь ЕСТЬ ────────────────────────────────


@pytest.mark.parametrize("stage,stage_type,target,door", CASES, ids=IDS)
def test_target_status_has_a_door(stage, stage_type, target, door, bench):
    """Цель отката — не просто «не начало», а статус с рабочей кнопкой.

    Иначе откат менял бы один тупик на другой. У трёх значений дверь —
    кнопка самой упавшей стадии; у `ocr` прямой кнопки в этом статусе нет
    (она появляется только с `ocr_completed`, а это была бы неправда: OCR
    не завершался).

    ⛔ Дверь для `ocr` НАЗВАНА ЗАНОВО по замеру ревизии связки (§104.10,
    пересняно мной — §107.1). Прежняя редакция этой докстроки и комментарий
    карты называли дверью подтверждение валидации перекрёстков — его гейт
    `validated_graph` не пускает ВООБЩЕ, ответ 400. Настоящая дверь —
    `POST /api/validation/{uid}/graph/complete-simple`: `validated_graph`
    он пускает, «ушли вперёд» у него пустое, и OCR уходит заново. В клиенте
    это кнопка «Проверка схемы» (`val_graph`), и здесь утверждается именно
    она, а не любая доступная.
    """
    ws, _ = bench(SrvStatus(target), None, stages_error=False)

    keys = enabled_keys(ws)
    assert keys, f"в '{target}' у оператора не осталось ни одной кнопки"
    if door is not None:
        assert door in keys, f"в '{target}' нет кнопки '{door}': {keys}"
    else:
        # ⚠ Переснято блоком pains-1: дверей в `validated_graph` теперь ДВЕ.
        # Прямая кнопка «Распознавание текста» перестала быть глухой (боль
        # 1.4 сузила набор «в процессе» бусины OCR до живой стадии), и сервер
        # её пускает — `app/api/ocr.py:37,54`. Косвенная — «Проверка схемы»,
        # та самая, что названа замером §107.1; она остаётся заперта здесь же,
        # чтобы правка бусины не подменила одну дверь другой молча.
        assert "ocr" in keys, keys
        assert "val_graph" in keys, (
            f"в '{target}' нет двери переотправки OCR («Проверка схемы»): {keys}"
        )


# ── часть 3б: СКВОЗНОЙ сценарий — тот же воркспейс после клика ───────────
#
# Клик по страховке чистит `_last_stages` (`_on_error_retry`): сервер откатил
# статус, значит снимок стадий в памяти клиента описывает состояние, которого
# больше нет. Замер §107.2 ДО починки: та же бегущая строка, что создала тупик,
# продолжала держать свою кнопку в `processing` уже в ЦЕЛЕВОМ статусе — до
# `WAIT_LIMIT_S` = 600 с, и у 2 значений из 4 это была ровно дверь
# (`direction_classification` → `segment`, `contour_extraction` → `contours`);
# оператору оставались только откаты с УДАЛЕНИЕМ артефактов. Числа §107.10.
ENABLED_AFTER_CLICK = {
    "direction_classification": ["cvat", "detect", "frame", "segment"],
    # ⚠ `ocr` в двух списках `validated_graph` — следствие pains-1 (боль 1.4):
    # набор «в процессе» бусины OCR сузился до живой стадии, и кнопка
    # «Распознавание текста» в этом статусе больше не глухая. Дверь настоящая,
    # а не украшение: `POST /api/ocr/{uid}/start` пускает `validated_graph`
    # (`app/api/ocr.py:37,54`).
    "contour_extraction": ["contours", "cvat", "detect", "frame", "graph",
                           "junction", "ocr", "pipe", "segment", "val_graph"],
    "ocr": ["contours", "cvat", "detect", "frame", "graph", "junction",
            "ocr", "pipe", "segment", "val_graph"],
    "generating_fxml": ["contours", "cvat", "detect", "edit_graph", "frame",
                        "graph", "junction", "ocr", "ocr_binding", "pipe",
                        "segment", "val_graph"],
}

# Порог с другой стороны: глушение бегущей стадией — рабочий механизм, и чинили
# НЕ его. Здесь отката не было, стадия действительно бежит, и кнопка обязана
# гаснуть. Без этих клеток «починкой» сошло бы и простое отключение цикла.
MUTED_WITHOUT_ROLLBACK = [
    ("validated_bbox", "direction_classification", "segment",
     ["cvat", "detect", "frame"]),
    ("validated_graph", "contour_extraction", "contours",
     ["cvat", "detect", "frame", "graph", "junction", "ocr", "pipe", "segment",
      "val_graph"]),
]


@pytest.mark.parametrize("stage,stage_type,target,door", CASES, ids=IDS)
def test_the_click_lands_in_the_same_workspace(stage, stage_type, target, door, bench):
    """Сквозной сценарий: тупик → реальный клик → кнопки В ТОМ ЖЕ воркспейсе.

    Почему отдельно от `test_target_status_has_a_door`: тот строит НОВЫЙ
    воркспейс с чистыми стадиями, то есть проверяет статус, а не путь оператора
    (ловушка «свежий объект», `PROTOCOL §3`). Здесь воркспейс тот же самый,
    предыстория та же, и клик настоящий — а значит виден шов между откатом
    сервера и памятью клиента.
    """
    ws, server = bench(SrvStatus.ERROR, stage, stale=[running_row(stage_type)])
    assert enabled_keys(ws) == [], "порог: до клика тупик"

    ws.btn_error_retry.click()

    assert server.retries == 1
    assert server.diagram.status.value == target
    assert not ws.btn_error_retry.isVisible(), (
        "страховка осталась висеть после успешного отката"
    )
    assert enabled_keys(ws) == ENABLED_AFTER_CLICK[stage], (
        f"'{stage}': после клика доступны {enabled_keys(ws)}"
    )


@pytest.mark.parametrize("stage,stage_type,target,door", CASES, ids=IDS)
def test_the_stale_running_row_does_not_mute_the_door(stage, stage_type, target,
                                                      door, bench):
    """Дверь целевого статуса открыта СРАЗУ, а не через 600 с.

    Дефект был у двух значений из четырёх и жил ровно на этом шве: карта сервера
    возвращала оператора на рабочий шаг, а кнопка этого шага оставалась глухой,
    потому что клиент всё ещё помнил её стадию бегущей. Выход при этом
    формально был — `cvat`/`detect`/`frame`, — но это откаты с УДАЛЕНИЕМ
    артефактов, то есть цена выхода, а не выход.
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stale=[running_row(stage_type)])

    ws.btn_error_retry.click()

    keys = enabled_keys(ws)
    if door is not None:
        assert door in keys, (
            f"'{stage}': дверь '{door}' глухая сразу после отката — {keys}"
        )
    else:
        assert "val_graph" in keys, keys


@pytest.mark.parametrize("status,stage_type,key,expected", MUTED_WITHOUT_ROLLBACK,
                         ids=[c[1] for c in MUTED_WITHOUT_ROLLBACK])
def test_the_muting_of_a_really_running_stage_is_intact(status, stage_type, key,
                                                        expected, bench):
    """Порог: чинили КЭШ, а не механизм — бегущая стадия по-прежнему гасит.

    Отката не было, строка описывает настоящее состояние. Если бы «починка»
    свелась к отключению цикла глушения в `_update_buttons`, эта клетка бы
    покраснела.
    """
    ws, _ = bench(SrvStatus(status), None, stale=[running_row(stage_type)])

    assert enabled_keys(ws) == expected
    assert key not in enabled_keys(ws), (
        f"строка '{stage_type}' перестала гасить свою кнопку '{key}'"
    )


def test_a_new_running_row_after_the_click_mutes_again(bench):
    """Кэш ИМЕННО ЧИСТИТСЯ: следующий опрос наполняет его заново.

    Утверждается РАЗНИЦА (`PROTOCOL §3`): сразу после клика дверь открыта,
    а после того, как опрос принёс НОВУЮ бегущую строку той же стадии, она
    снова глухая. Значит клик снял снимок, а не выключил глушение навсегда.
    """
    ws, _ = bench(SrvStatus.ERROR, "direction_classification",
                  stale=[running_row("direction_classification")])

    ws.btn_error_retry.click()
    assert "segment" in enabled_keys(ws)

    ws.status_provider.stages_updated.emit(
        UID, [running_row("direction_classification")])
    ws._refresh_status()

    assert enabled_keys(ws) == ["cvat", "detect", "frame"], (
        "новая бегущая строка не заглушила кнопку — клик выключил механизм, "
        "а не снял устаревший снимок"
    )


# ── часть 4: сторож двух карт ────────────────────────────────────────────


def _client_rollback_target(ws, key):
    return ws._ROLLBACK_TARGET[key]


def _server_rollback_target(stage):
    """Куда уводит СЕРВЕР — снято исполнением, а не чтением литерала.

    Карта `stage_to_status` живёт локальной переменной внутри корутины
    и импортироваться не умеет; это и к лучшему — судим по поведению.
    """
    out, _ = call_retry(SrvStatus.ERROR, stage)
    return out["status"]


@pytest.mark.parametrize("stage,stage_type,target,door", CASES, ids=IDS)
def test_server_agrees_with_the_client_map(stage, stage_type, target, door, bench):
    """Четыре значения ноги: сервер и клиент называют ОДИН статус.

    Клиентская карта `_ROLLBACK_TARGET` («статус ПЕРЕД этим этапом») — не моя
    выдумка, а действующий канон отката по кнопке. Ключ кнопки берётся не из
    литерала, а исполнением: какую кнопку клиент красит на этой ошибке.
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stale=[])
    key = next(k for k, b in ws._action_buttons.items()
               if b.styleSheet() == dw._BTN_STYLE_RED)

    assert _server_rollback_target(stage) == _client_rollback_target(ws, key) == target


def test_older_disagreements_between_the_two_maps_are_named_not_fixed(bench):
    """Четыре расхождения СТАРШЕ этой ноги — зафиксированы как есть.

    Их не должны принять за недосмотр: сведение двух карт «стадия → статус»
    в один реестр — предмет волны 5, и трогать их правкой этой ноги значило бы
    решать за неё. Разойдётся ещё что-нибудь — тест покраснеет здесь.
    """
    ws, _ = bench(SrvStatus.ERROR, None, stale=[])
    known = {
        # error_stage           сервер                клиент (кнопка)
        "skeletonizing":       ("segmenting",         "validated_bbox"),
        "skeletonizing_simple": ("validated_masks",   "skeletonized"),
        "detecting_junctions": ("skeletonized_final", "detected_junctions"),
        "fetching_annotations": ("validating_bbox",   "detected"),
    }
    for stage, (srv, cli) in known.items():
        assert _server_rollback_target(stage) == srv, stage
    for stage, key in (("skeletonizing", "segment"),
                       ("skeletonizing_simple", "pipe"),
                       ("detecting_junctions", "junction"),
                       ("fetching_annotations", "cvat")):
        assert ws._ROLLBACK_TARGET[key] == known[stage][1], stage
