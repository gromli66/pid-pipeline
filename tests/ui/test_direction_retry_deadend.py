# -*- coding: utf-8 -*-
"""Пункт 1.12 дороги (Т2 аудита) — из упавшей классификации направления есть выход.

Дефект. Направление падает → воркер ставит `status=ERROR`,
`error_stage="direction_classification"` (`worker/tasks/direction.py:251,265`)
и `ProcessingStage(stage_type="direction_classification", status="failed")`.
Сервер к этому готов: `POST /api/segmentation/{uid}/segment` пускает `error`
и по `_STAGE_DISPATCH` перезапускает цепочку С НАПРАВЛЕНИЯ (таблицу держит
`tests/test_segmentation_status_gate.py`). Слеп КЛИЕНТ: стадии нет ни в
`_STAGE_TYPE_TO_KEY` (`diagram_workspace.py:288`), ни в `_STAGE_TO_KEY`
(`:1010`), поэтому `_error_key = None` — красной кнопки не появляется,
а в фолбэк-ветке (стадии недоступны) серыми становятся ВСЕ тринадцать.

Почему тест сценарный, а не юнит. Дефект живёт на ШВЕ «стадия сервера →
кнопка клиента»: и сервер прав, и клиент сам по себе непротиворечив. Поэтому
здесь живой `DiagramWorkspace` с настоящими сигналами Qt, а поддельный сервер
судит **настоящей** `app.api.segmentation.start_segmentation` — не моим
представлением о том, что она пропускает (образец — `tests/ui/
test_detection_retry_deadend.py`, нога 1.11 того же пункта).

Оба пути к красной кнопке проверяются отдельно, потому что они разные:
  • стадии доступны → `_apply_error_status` → `_stage_errors` → окно отчёта
    (`_show_stage_error_dialog`) → обработчик;
  • стадий нет → фолбэк `_update_buttons(ERROR, error_stage)` → `_error_key`
    → `_on_button_click` → обработчик напрямую.

Решётки — литералы, снятые ЧТЕНИЕМ обеих карт, а не вычисленные из них
(`PROTOCOL §3`): иначе набор остался бы зелёным при любом их содержимом.
Перебор идёт по ПОЛНОМУ множеству — 31 статус `DiagramStatus`, 16 значений
`StageType`, весь словарь `error_stage` конвейера.

⚠ Модальные диалоги пути инъекции подменены (`PROTOCOL §5`): `QMessageBox`
из `_start_segmentation` и `ErrorReportDialog` из `_show_stage_error_dialog`.
Без подмены красный прогон не падает, а ВИСНЕТ, и на CI это выглядит
как «долго».
"""
import asyncio
import os
import uuid
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

import celery                                              # noqa: E402
from fastapi import HTTPException                          # noqa: E402
from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

import ui.widgets.error_report_dialog as erd               # noqa: E402
from app.api.segmentation import start_segmentation as srv_start_segmentation  # noqa: E402
from app.models import Diagram                             # noqa: E402
from app.models import DiagramStatus as SrvStatus          # noqa: E402
from app.models.stage import StageType                     # noqa: E402

import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import APIError, DiagramStatus  # noqa: E402

UID = "d74eb9f1-1111-2222-3333-444455556666"
DIRECTION_TASK = "worker.tasks.direction.task_classify_direction"
SEGMENT_TASK = "worker.tasks.segmentation.task_segment_pipes"

# Размеры обоих множеств. Абсолютные числа: новое значение обязано пройти
# через эту решётку, а не проскочить мимо неё молча.
STATUS_COUNT = 31
STAGE_TYPE_COUNT = 16
BUTTON_COUNT = 13

# Значения `error_stage`, которые пишет конвейер (`set_diagram_error`).
# Словарь шире карты клиента намеренно: половина этих строк ни на какую
# кнопку не ложится, и решётка обязана это показывать.
RUNTIME_ERROR_STAGES = [
    "detecting",
    "direction_classification",
    "segmenting",
    "skeletonizing",
    "skeletonizing_simple",
    "detecting_junctions",
    "building_graph",
    "validating_graph",
    "contour_extraction",
    "generating_fxml",
    "ocr",
]
# Полный перебор: плюс все значения `StageType` (их пишет `/stages`).
ERROR_STAGES = sorted(set(RUNTIME_ERROR_STAGES) | {s.value for s in StageType})

# ── решётка 1: фолбэк-ветка `_update_buttons` (карта `_STAGE_TO_KEY`) ─────
# `error_stage` → ключ кнопки, которая станет красной. Чего нет в карте —
# нет и красной кнопки: в фолбэк-ветке это ноль доступных кнопок из 13.
# ДО пункта 1.12 (зафиксировано коммитом 4564f55, 126 тестов зелёные
# на нетронутом коде):
RED_BY_ERROR_STAGE_BEFORE = {
    "detecting": "detect",
    "segmenting": "segment",
    "skeletonizing": "segment",
    "skeletonizing_simple": "pipe",
    "detecting_junctions": "junction",
    "building_graph": "graph",
    "validating_graph": "val_graph",
    "contour_extraction": "contours",
    "generating_fxml": "fxml",
    "ocr": "ocr",
}

# ПОСЛЕ пункта 1.12. Разница обязана быть ровно в одной клетке — сторож ниже.
RED_BY_ERROR_STAGE = {
    "detecting": "detect",
    "direction_classification": "segment",
    "segmenting": "segment",
    "skeletonizing": "segment",
    "skeletonizing_simple": "pipe",
    "detecting_junctions": "junction",
    "building_graph": "graph",
    "validating_graph": "val_graph",
    "contour_extraction": "contours",
    "generating_fxml": "fxml",
    "ocr": "ocr",
}

# ── решётка 2: основной путь `_apply_error_status` (карта `_STAGE_TYPE_TO_KEY`) ─
# `stage_type` упавшей стадии → ключ, который попадёт в `_stage_errors`
# (красная бусина + красная кнопка + окно отчёта). `upload` кнопки не имеет
# по замыслу — этапа «Загрузка» в столбце нет.
# ДО пункта 1.12 (тот же коммит 4564f55):
FAILED_KEY_BY_STAGE_TYPE_BEFORE = {
    "frame_removal": "frame",
    "detection": "detect",
    "cvat_validation": "cvat",
    "segmentation": "segment",
    "skeletonization": "segment",
    "mask_validation": "pipe",
    "junction_classification": "junction",
    "final_skeletonization": "segment",
    "graph_building": "graph",
    "graph_validation": "val_graph",
    "contour_extraction": "contours",
    "ocr": "ocr",
    "layout": "edit_graph",
    "fxml_generation": "fxml",
}

# ПОСЛЕ пункта 1.12. Разница — ровно одна клетка, сторож ниже.
FAILED_KEY_BY_STAGE_TYPE = {
    "frame_removal": "frame",
    "detection": "detect",
    "cvat_validation": "cvat",
    "direction_classification": "segment",
    "segmentation": "segment",
    "skeletonization": "segment",
    "mask_validation": "pipe",
    "junction_classification": "junction",
    "final_skeletonization": "segment",
    "graph_building": "graph",
    "graph_validation": "val_graph",
    "contour_extraction": "contours",
    "ocr": "ocr",
    "layout": "edit_graph",
    "fxml_generation": "fxml",
}


# ── поддельный сервер: гейт настоящий ────────────────────────────────────

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    def __init__(self, diagram):
        self.diagram = diagram

    async def execute(self, stmt):
        return _FakeResult(self.diagram)

    async def commit(self):
        pass


class FakeServer:
    """Одна диаграмма + журнал отправленных задач.

    Пускать или нет и с какого шага — решает НАСТОЯЩАЯ корутина эндпоинта.
    """

    def __init__(self, status, error_stage=None, error_message=None):
        self.diagram = Diagram()
        self.diagram.uid = uuid.UUID(UID)
        self.diagram.status = status
        self.diagram.error_stage = error_stage
        self.diagram.error_message = error_message
        self.diagram.project_code = "thermohydraulics"
        self.dispatched = []
        self.restart_from = []

    def segment(self):
        """`POST /api/segmentation/{uid}/segment` — ровно так, как её зовёт APIClient."""
        try:
            result = asyncio.run(srv_start_segmentation(
                uuid.UUID(UID), db=_FakeDB(self.diagram),
            ))
        except HTTPException as exc:
            # `APIClient._request` превращает ответ >= 400 ровно в это.
            raise APIError(str(exc.detail), exc.status_code) from None
        self.restart_from.append(result.get("restart_from"))
        return result


class FakeDiagramInfo:
    def __init__(self, server):
        self.status = DiagramStatus(server.diagram.status.value)
        self.error_stage = server.diagram.error_stage
        self.project_code = server.diagram.project_code


class FakeAPI:
    """Поверхность APIClient, которой воркспейс пользуется в этом сценарии."""

    def __init__(self, server, stages):
        self.server = server
        self._stages = stages

    def get_diagram(self, uid):
        return FakeDiagramInfo(self.server)

    def get_stages(self, uid):
        return list(self._stages)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": False}

    def get_stage_durations(self):
        return {}

    def start_segmentation(self, uid):
        return self.server.segment()


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
    """Подмена модалки: жалоба оператору штатна, но висеть на ней тест не должен."""

    StandardButton = QMessageBox.StandardButton
    calls = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Yes

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Ok


class StubErrorDialog:
    """Окно отчёта об ошибке этапа: оператор нажал «🔄 Перезапустить»."""

    opened = []

    def __init__(self, stage, parent=None, phase_label=None, diagram_name=None):
        StubErrorDialog.opened.append(phase_label)

    def exec_retry(self):
        return True


def stage_row(stage_type, status="failed", *, fresh=False):
    """Строка `ProcessingStage`, как её отдаёт `/stages`.

    `fresh=True` — стадия стартовала «сейчас»: иначе `_stage_stuck` снимает
    глушение кнопки по пределу ожидания (600 с) и «бежит» превращается
    в «не бежит».
    """
    started = (datetime.utcnow() if fresh
               else datetime(2026, 8, 19, 10, 0, 0)).isoformat()
    return {
        "id": 1,
        "stage_type": stage_type,
        "status": status,
        "attempt": 1,
        "error_message": "Direction classification timed out (9 min limit)",
        "started_at": started,
        "created_at": started,
    }


def _failed_direction_stages():
    """Боевая картина: валидация детекции прошла, направление упало."""
    return [stage_row("cvat_validation", "completed"),
            stage_row("direction_classification", "failed")]


# ── харнесс ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    FakeMsgBox.calls = []
    StubErrorDialog.opened = []
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr(erd, "ErrorReportDialog", StubErrorDialog)

    from worker.celery_app import celery_app
    made = []

    def _make(status, error_stage=None, stages=None):
        server = FakeServer(status, error_stage=error_stage,
                            error_message="Direction classification timed out (9 min limit)")

        class _AsyncResult:
            id = "task-0001"

        def _send_task(name, args=None, kwargs=None, **rest):
            server.dispatched.append(name)
            return _AsyncResult()

        class _FakeChain:
            def __init__(self, *signatures):
                self._signatures = signatures

            def apply_async(self, *a, **kw):
                for sig in self._signatures:
                    server.dispatched.append(sig["task"])
                return _AsyncResult()

        monkeypatch.setattr(celery_app, "send_task", _send_task)
        monkeypatch.setattr(celery, "chain", _FakeChain)

        ws = dw.DiagramWorkspace(FakeAPI(server, stages or []),
                                 FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        made.append(ws)
        return ws, server

    yield _make

    # Свои воркспейсы набор сносит сам, детерминированно (`PROTOCOL §5`, правило
    # 1-32). Замер 1.x11: без этой уборки файл оставлял 2000 живых виджетов и 87
    # БЕГУЩИХ таймеров на весь процесс, и свежий 10-мс таймер соседнего теста
    # ждал события 0.27 с вместо 0.02 — на раннере CI это уронило
    # `test_unsaved_question.py::test_autosave_tick_reaches_the_tab_save_method`.
    # Выпиваем ровно отложенные удаления, а не всю очередь: общий
    # `processEvents()` оплачивал бы ещё и чужой мусор.
    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def red_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items()
                  if b.styleSheet() == dw._BTN_STYLE_RED)


def enabled_keys(ws):
    return sorted(k for k, b in ws._action_buttons.items() if b.isEnabled())


# ── сторожа самих решёток ────────────────────────────────────────────────

def test_machine_sizes_are_locked():
    """Обе решётки написаны на машину ровно такого размера."""
    assert len(list(SrvStatus)) == STATUS_COUNT
    assert len(list(StageType)) == STAGE_TYPE_COUNT


def test_grid_keys_name_real_buttons(bench):
    """Сторож набора: значение решётки — существующая кнопка, а не опечатка."""
    ws, _ = bench(SrvStatus.ERROR, "detecting", stages=[])
    keys = set(ws._action_buttons)
    assert len(keys) == BUTTON_COUNT
    assert set(RED_BY_ERROR_STAGE.values()) <= keys
    assert set(FAILED_KEY_BY_STAGE_TYPE.values()) <= keys


def test_grid_keys_name_real_stage_types():
    """Ключи решётки 2 — существующие `StageType`, а не выдуманные строки."""
    for value in FAILED_KEY_BY_STAGE_TYPE:
        assert StageType(value).value == value


def test_grids_changed_by_exactly_one_cell_each():
    """Пункт 1.12 добавил по одной клетке в каждую решётку и не отнял ни одной.

    Обе редакции — независимые литералы, поэтому правка одной карты без
    другой краснит этот сторож: «переход вне зафиксированного набора»
    (`PROTOCOL §Гейты`) ловится здесь, а не глазами ревизора.
    """
    added = {k: v for k, v in RED_BY_ERROR_STAGE.items()
             if RED_BY_ERROR_STAGE_BEFORE.get(k) != v}
    assert added == {"direction_classification": "segment"}
    assert set(RED_BY_ERROR_STAGE_BEFORE) - set(RED_BY_ERROR_STAGE) == set()

    added = {k: v for k, v in FAILED_KEY_BY_STAGE_TYPE.items()
             if FAILED_KEY_BY_STAGE_TYPE_BEFORE.get(k) != v}
    assert added == {"direction_classification": "segment"}
    assert set(FAILED_KEY_BY_STAGE_TYPE_BEFORE) - set(FAILED_KEY_BY_STAGE_TYPE) == set()


def test_upload_stays_outside_the_grid():
    """Единственная стадия без кнопки — `upload`, и она такой и остаётся.

    Порог с другой стороны: пункт закрывает дыру направления, а не
    «раздаёт кнопку каждому `StageType`».
    """
    outside = [s.value for s in StageType
               if s.value not in FAILED_KEY_BY_STAGE_TYPE]
    assert outside == ["upload"]


# ── решётка 1: фолбэк-ветка, весь словарь `error_stage` ──────────────────

@pytest.mark.parametrize("stage", ERROR_STAGES)
def test_fallback_paints_exactly_the_grid(stage, bench):
    """Стадий нет: красной становится ровно та кнопка, что в решётке.

    Порог заперт с двух сторон. Есть в решётке — ровно ОДНА красная и она же
    единственная доступная. Нет в решётке — доступных кнопок НОЛЬ из 13,
    то есть тупик: оператору нечего нажать вовсе.
    """
    ws, _ = bench(SrvStatus.ERROR, stage, stages=[])
    expected = RED_BY_ERROR_STAGE.get(stage)

    if expected is not None:
        assert red_keys(ws) == [expected]
        assert enabled_keys(ws) == [expected]
    else:
        assert red_keys(ws) == []
        assert enabled_keys(ws) == [], (
            f"'{stage}' не в решётке, но кнопка доступна — решётка протухла"
        )


# ── решётка 2: основной путь, весь `StageType` ──────────────────────────

@pytest.mark.parametrize("stage_type", list(StageType), ids=lambda s: s.value)
def test_stage_overlay_paints_exactly_the_grid(stage_type, bench):
    """Упавшая стадия из `/stages`: красной становится ровно та, что в решётке.

    Стадия вне решётки не даёт ни красной бусины, ни окна отчёта: сбой
    не показан оператору вовсе.
    """
    ws, _ = bench(SrvStatus.ERROR, stage_type.value,
                  stages=[stage_row(stage_type.value, "failed")])
    expected = FAILED_KEY_BY_STAGE_TYPE.get(stage_type.value)

    if expected is not None:
        assert sorted(ws._stage_errors) == [expected]
        assert red_keys(ws) == [expected]
    else:
        assert sorted(ws._stage_errors) == []
        assert red_keys(ws) == [], (
            f"'{stage_type.value}' не в решётке, но кнопка покрасилась"
        )


# ── решётка 3: тупик на полном множестве статусов ────────────────────────

@pytest.mark.parametrize("status", list(SrvStatus), ids=lambda s: s.value)
def test_direction_error_over_every_status(status, bench):
    """31 статус машины с `error_stage='direction_classification'`.

    Утверждение абсолютное: тупик — РОВНО ОДНА клетка решётки. Ноль доступных
    кнопок бывает только в `error`; во всех тридцати остальных статусах
    `error_stage` вообще не участвует в расчёте (ветка `_error_key` заперта
    условием `status == ERROR`), и оператору всегда есть что нажать.
    """
    ws, _ = bench(status, "direction_classification", stages=[])

    if status is SrvStatus.ERROR:
        assert enabled_keys(ws) == ["segment"], (
            "выход из упавшего направления снова закрыт"
        )
        assert red_keys(ws) == ["segment"]
    else:
        assert enabled_keys(ws), f"статус {status.value} осиротел без ошибки"


# ── гейт пункта: выход из тупика есть ────────────────────────────────────

def test_red_button_restarts_direction_with_stages(bench):
    """Стадии доступны: клик по красной кнопке через окно отчёта чинит тупик.

    Боевая картина: валидация детекции завершена, направление упало. Кнопка
    «Выделение труб» обязана стать красной, а перезапуск — уйти цепочкой,
    начинающейся С НАПРАВЛЕНИЯ.
    """
    ws, server = bench(SrvStatus.ERROR, "direction_classification",
                       _failed_direction_stages())

    btn = ws._action_buttons["segment"]
    assert btn.isEnabled(), "красная кнопка недоступна — тупик виден уже здесь"
    assert sorted(ws._stage_errors) == ["segment"], "упавший этап не опознан"
    assert btn.styleSheet() == dw._BTN_STYLE_RED
    assert btn.text().startswith("🔄")

    btn.click()

    assert StubErrorDialog.opened == ["Выделение труб"], "окно отчёта не открылось"
    assert server.diagram.status is SrvStatus.SEGMENTING, (
        f"направление не перезапущено: на сервере {server.diagram.status.value}"
    )
    assert server.restart_from == ["direction_classification"], (
        f"перезапуск ушёл не с направления: {server.restart_from}"
    )
    assert server.dispatched == [DIRECTION_TASK, SEGMENT_TASK], (
        f"цепочка ушла не та: {server.dispatched}"
    )
    assert FakeMsgBox.calls == [], f"оператор получил отказ: {FakeMsgBox.calls}"


def test_red_button_restarts_direction_without_stages(bench):
    """Стадий нет: та же кнопка в фолбэк-ветке `_update_buttons` делает то же.

    Это та ветка, где до правки серыми были ВСЕ тринадцать кнопок.
    """
    ws, server = bench(SrvStatus.ERROR, "direction_classification", stages=[])

    btn = ws._action_buttons["segment"]
    assert btn.isEnabled(), "красная кнопка недоступна в фолбэк-ветке"
    assert ws._stage_errors == {}, "фолбэк-ветка не должна знать стадий"

    btn.click()

    assert StubErrorDialog.opened == [], "в фолбэк-ветке окна отчёта нет"
    assert server.diagram.status is SrvStatus.SEGMENTING, (
        f"направление не перезапущено: на сервере {server.diagram.status.value}"
    )
    assert server.restart_from == ["direction_classification"]
    assert server.dispatched == [DIRECTION_TASK, SEGMENT_TASK]
    assert FakeMsgBox.calls == [], f"оператор получил отказ: {FakeMsgBox.calls}"


def test_restart_clears_error_on_server(bench):
    """Перезапуск снимает ошибку — иначе диаграмма бежит с протухшим error_stage."""
    ws, server = bench(SrvStatus.ERROR, "direction_classification",
                       _failed_direction_stages())

    ws._action_buttons["segment"].click()

    assert server.diagram.error_stage is None
    assert server.diagram.error_message is None


def test_error_report_carries_the_stage_row(bench):
    """Окно отчёта получает СТРОКУ упавшей стадии, а не пустышку.

    Без неё оператор видит красную кнопку и пустой отчёт: `error_message`
    и traceback направления до него не доезжают.
    """
    ws, _ = bench(SrvStatus.ERROR, "direction_classification",
                  _failed_direction_stages())

    row = ws._stage_errors["segment"]
    assert row["stage_type"] == "direction_classification"
    assert "Direction classification timed out" in row["error_message"]
    assert ws._action_buttons["segment"].toolTip().startswith("Ошибка:")


# ── порог с другой стороны ───────────────────────────────────────────────

def test_foreign_error_stage_does_not_open_segmentation(bench):
    """Упала ДРУГАЯ стадия — «Выделение труб» красной не становится.

    Без этой проверки правка «любая ошибка → segment» осталась бы зелёной.
    """
    ws, server = bench(SrvStatus.ERROR, "ocr", stages=[stage_row("ocr")])

    assert sorted(ws._stage_errors) == ["ocr"]
    assert ws._action_buttons["segment"].styleSheet() != dw._BTN_STYLE_RED
    assert server.dispatched == []


def test_completed_direction_does_not_fake_a_finished_segmentation(bench):
    """Направление ЗАВЕРШЕНО, сегментация упала — красная всё равно она.

    Обе стадии делят одну кнопку, и порядок наложений обязан оставлять
    последнее слово за упавшей: иначе зелёная «Выделение труб» врала бы
    о несделанной сегментации.
    """
    ws, _ = bench(SrvStatus.ERROR, "segmenting", stages=[
        stage_row("cvat_validation", "completed"),
        stage_row("direction_classification", "completed"),
        stage_row("segmentation", "failed"),
    ])

    assert sorted(ws._stage_errors) == ["segment"]
    assert ws._action_buttons["segment"].styleSheet() == dw._BTN_STYLE_RED


def test_running_direction_holds_the_button_and_lets_it_go(bench):
    """Заявленное СЛЕДСТВИЕ той же строки карты — бегущее направление.

    До правки бегущая классификация кнопку не занимала вовсе: на
    `validated_bbox` «Выделение труб» оставалась жёлтой, и второй клик
    отправлял вторую цепочку. Теперь она глушится, как любая авто-стадия.

    Вторая половина — что новой серой кнопки навсегда это не создаёт:
    предел ожидания `_stage_stuck` (600 с, равен жёсткому лимиту задачи)
    повисшую стадию отпускает.
    """
    ws, _ = bench(SrvStatus.VALIDATED_BBOX, None, stages=[
        stage_row("direction_classification", "running", fresh=True),
    ])
    ws._on_stages_updated(UID, ws.api_client.get_stages(UID))
    ws._update_buttons(DiagramStatus.VALIDATED_BBOX)
    assert not ws._action_buttons["segment"].isEnabled(), (
        "бегущее направление не глушит кнопку — вторая цепочка уйдёт по клику"
    )

    ws2, _ = bench(SrvStatus.VALIDATED_BBOX, None, stages=[
        stage_row("direction_classification", "running", fresh=False),
    ])
    ws2._on_stages_updated(UID, ws2.api_client.get_stages(UID))
    ws2._update_buttons(DiagramStatus.VALIDATED_BBOX)
    assert ws2._action_buttons["segment"].isEnabled(), (
        "повисшая стадия глушит кнопку навсегда — предел ожидания не работает"
    )


# ── сторож самого стенда ─────────────────────────────────────────────────

def test_bench_judges_by_real_endpoint():
    """Стенд судит настоящей корутиной эндпоинта, а не своей копией гейта."""
    server = FakeServer(SrvStatus.SKELETONIZED)
    with pytest.raises(APIError) as exc:
        server.segment()
    assert exc.value.status_code == 400
    assert "skeletonized" in exc.value.message
