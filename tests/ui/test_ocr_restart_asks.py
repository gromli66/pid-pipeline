# -*- coding: utf-8 -*-
"""Повторный запуск распознавания спрашивает — на КАЖДОМ пути (возврат приёмки pains-3).

Дефект, найденный приёмкой глазами Максима. Клик по зелёной кнопке
«Распознавание текста» запускал OCR **молча**: сырой результат сносился
(`app/api/ocr.py` удаляет `OCR_RESULT` на `/ocr/start`), уходили минуты CPU,
привязку подписей приходилось проходить заново — и всё без единого вопроса.

Почему вопрос, поставленный пунктом Н3+, не сработал. Он стоял в
`_on_button_click` под условием `key in completed`, а `completed` считает
`_buttons_for_status` по **статусу диаграммы**. У распознавания своего статуса
нет: оно идёт параллельно сборке графа, и `OCR_COMPLETED` в этом репозитории
**не присваивает никто** (`MEASUREMENTS §P3.2`). Кнопка же зеленеет по
АРТЕФАКТУ — `_ocr_notified` из `/ocr/status` (`_apply_status`,
`_check_ocr_artifact`). Значит в живом конвейере оператор жмёт зелёную кнопку
при `validated_graph`/`contours_validated`, где `"ocr" not in completed`, —
и условие не срабатывает ни разу. Прежний набор был зелёным, потому что брал
единственный статус, при котором оно срабатывает (`completed`), то есть судил
ВЫБОРКУ вместо множества (`PROTOCOL §3`).

Как чинится. Вопрос переехал из ветки клика в САМ `_start_ocr` — единственную
воронку, через которую все пути попадают в `POST /ocr/start`. Перечень путей
остаётся доказательством, а не несущей конструкцией: новая кнопка, ведущая
в `_start_ocr`, получит вопрос без правки этого файла.

⚠ `QMessageBox` и окно отчёта об ошибке подменены (`PROTOCOL §5`): без подмены
красный прогон не падает, а ВИСНЕТ на живой модалке.
"""
import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                              # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal          # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

import ui.widgets.error_report_dialog as erd               # noqa: E402
import ui.widgets.diagram_workspace as dw                  # noqa: E402
from ui.services.api_client import DiagramStatus           # noqa: E402
from ui.widgets.progress_beads import BeadState            # noqa: E402

UID = str(uuid.UUID("0c111111-2222-3333-4444-555566667777"))

# Статусы, при которых кнопка «Распознавание текста» ЗЕЛЁНАЯ по артефакту, а
# не по статусу. Список снят ЧТЕНИЕМ обеих веток `_apply_status`, которые
# красят её при `has_ocr_result` (ветки `not self._ocr_notified` и
# `elif self._ocr_notified`), — это и есть живой конвейер.
GREEN_BY_ARTIFACT = [
    "building_graph",
    "built",
    "validating_graph",
    "validated_graph",
    "extracting_contours",
    "contours_extracted",
    "contours_validated",
]

# Статусы, при которых этап числится пройденным и по статусу тоже. Только на
# них работало прежнее условие `key in completed`.
GREEN_BY_STATUS = ["ocr_completed", "ocr_bound", "generating_fxml", "completed"]

ALL_GREEN = GREEN_BY_ARTIFACT + GREEN_BY_STATUS


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeMsgBox:
    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("question", title, text))
        return cls.answer

    @classmethod
    def warning(cls, parent, title, text="", *args, **kwargs):
        cls.calls.append(("warning", title, text))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, parent, title, text="", *args, **kwargs):
        cls.calls.append(("information", title, text))
        return cls.StandardButton.Ok


class StubErrorDialog:
    """Окно отчёта об ошибке: оператор нажал «🔄 Перезапустить»."""

    opened = []
    retry = True

    def __init__(self, stage, parent=None, phase_label=None, diagram_name=None):
        StubErrorDialog.opened.append(phase_label)

    def exec_retry(self):
        return StubErrorDialog.retry


class FakeDiagramInfo:
    def __init__(self, status, error_stage):
        self.status = status
        self.error_stage = error_stage
        self.project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность APIClient этого сценария + журнал запусков OCR."""

    def __init__(self, status, has_ocr_result, stages, error_stage=None):
        self.status = status
        self.has_ocr_result = has_ocr_result
        self._stages = stages
        self.error_stage = error_stage
        self.started = []

    def get_diagram(self, uid):
        return FakeDiagramInfo(self.status, self.error_stage)

    def get_stages(self, uid):
        return list(self._stages)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": self.has_ocr_result}

    def get_stage_durations(self):
        """Бюджеты прогресса: `_on_stages_updated` читает их у клиента."""
        return {}

    def start_ocr(self, uid):
        self.started.append(uid)
        # Сервер удаляет `OCR_RESULT` в самом начале `/ocr/start`
        # (`app/api/ocr.py`) — без этого стенд судил бы состояние, которого
        # у оператора не бывает.
        self.has_ocr_result = False
        return {"status": "dispatched", "task_id": "task-0001"}

    def rollback_diagram(self, uid, target_status, preserve_ocr=False,
                         preserve_contours=False):
        raise AssertionError("перезапуск распознавания не откатывает конвейер")


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


def _ocr_stage(status="completed"):
    return [{"id": 11, "stage_type": "ocr", "status": status, "attempt": 1,
             "error_message": "OCR упал" if status == "failed" else None,
             "error_traceback": "", "error_code": "PipelineError"}]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Живой воркспейс на заданном состоянии.

    Свои виджеты и ТАЙМЕРЫ набор сносит сам, детерминированно (`PROTOCOL §5`,
    правила 1-32 и 1-36): `_start_ocr` поднимает поллер артефакта, и брошенный
    бегущий таймер штрафует соседний набор, а не свой.
    """
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    StubErrorDialog.opened = []
    StubErrorDialog.retry = True
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr(erd, "ErrorReportDialog", StubErrorDialog)

    made = []

    def _make(status, has_ocr_result=True, stages=None, error_stage=None,
              feed_stages=True):
        api = FakeAPI(DiagramStatus(status), has_ocr_result,
                      stages if stages is not None else [],
                      error_stage=error_stage)
        ws = dw.DiagramWorkspace(api, FakeStatusProvider())
        ws.load_diagram(UID, "схема оператора")
        # Живой клиент получает стадии СИГНАЛОМ провайдера, а не из
        # `_refresh_status`. Флаг `feed_stages` нужен, чтобы признаки воронки
        # проверялись ПООТДЕЛЬНОСТИ: если кэш стадий налит всегда, он один
        # покрывает все клетки, и зонд на любой другой признак остаётся
        # зелёным — стенд слепнет целиком (`PROTOCOL §5`).
        if feed_stages:
            ws._on_stages_updated(UID, api.get_stages(UID))
        made.append(ws)
        return ws, api

    yield _make

    for ws in made:
        ws._stop_ocr_poll()
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _click_ocr(ws):
    """Клик по кнопке распознавания — ровно так, как её связал воркспейс."""
    ws._on_button_click("ocr", ws._original_handlers["ocr"])


# ── сторожа стенда и границы перебора ────────────────────────────────────

def test_the_only_door_to_the_endpoint_is_start_ocr():
    """Перечень путей проверяется КОМАНДОЙ, а не впечатлением (`PROTOCOL §3`).

    Греп `start_ocr` по `ui/` даёт ровно три места: объявление в APIClient,
    единственный вызов внутри `_start_ocr` и запись `ocr` в карте
    обработчиков. Значит воронка одна, и вопрос, поставленный в ней,
    накрывает все пути — включая те, которых сегодня нет.
    """
    import inspect
    from pathlib import Path

    root = Path(dw.__file__).resolve().parents[2]
    hits = []
    for path in (root / "ui").rglob("*.py"):
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "start_ocr" in line and "_start_ocr_poll" not in line \
                    and "_stop_ocr_poll" not in line:
                hits.append((path.name, num, line.strip()))

    callers = [h for h in hits if "self.api_client.start_ocr" in h[2]]
    assert len(callers) == 1, f"дверей к эндпоинту больше одной: {callers}"
    assert callers[0][0] == "diagram_workspace.py"

    src = inspect.getsource(dw.DiagramWorkspace._start_ocr)
    assert "self.api_client.start_ocr" in src, "воронка переехала — пересверить пункт"


def test_ocr_completed_is_never_assigned_by_the_pipeline():
    """Корень дефекта, названный адресно: статус `ocr_completed` никто не ставит.

    Именно поэтому условие `key in completed` в ветке клика было мёртвым:
    в живом конвейере оператор жмёт зелёную кнопку при статусе РАНЬШЕ него.
    """
    from pathlib import Path

    root = Path(dw.__file__).resolve().parents[2]
    writers = []
    for sub in ("app", "worker"):
        for path in (root / sub).rglob("*.py"):
            for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "= DiagramStatus.OCR_COMPLETED" in line:
                    writers.append((str(path), num))
    assert writers == [], f"статус снова кто-то пишет — пересверить пункт: {writers}"


@pytest.mark.parametrize("status", GREEN_BY_ARTIFACT)
def test_button_is_green_while_the_stage_is_not_completed(status, bench):
    """Обстановка дефекта: кнопка зелёная, а этап «пройденным» НЕ числится.

    Без этой клетки набор ниже мог бы зеленеть просто потому, что состояние
    недостижимо; здесь оно названо числом.
    """
    ws, _api = bench(status, stages=[])

    _available, completed, _processing = dw._buttons_for_status(
        DiagramStatus(status))
    assert "ocr" not in completed, status
    assert ws._ocr_notified is True, "кнопка не позеленела — стенд не тот"
    assert ws._action_buttons["ocr"].isEnabled(), status


# ── гейт пункта: повторный запуск спрашивает ─────────────────────────────

@pytest.mark.parametrize("status", ALL_GREEN)
def test_repeat_click_asks_before_restarting(status, bench):
    """Повторный клик при готовом результате — вопрос, потом запуск.

    Строки стадии здесь НЕТ намеренно: у семи статусов из одиннадцати
    единственный признак — артефакт (`_ocr_notified`), и клетка обязана
    проверять именно его, а не соседний признак.
    """
    ws, api = bench(status, stages=[])

    _click_ocr(ws)

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"{status}: распознавание запустилось молча"
    )
    assert api.started == [UID], status


@pytest.mark.parametrize("status", ALL_GREEN)
def test_refusal_starts_nothing(status, bench):
    """Порог заперт с другой стороны: «Нет» — и ни одного запроса к серверу."""
    ws, api = bench(status, stages=[])
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    _click_ocr(ws)

    assert len(FakeMsgBox.calls) == 1, status
    assert api.started == [], f"{status}: отказ оператора не остановил запуск"


def test_question_names_what_the_restart_costs(bench):
    """Вопрос честен: он называет цену — снос результата и повтор привязки."""
    ws, _api = bench("validated_graph", stages=[])

    _click_ocr(ws)

    _kind, title, text = FakeMsgBox.calls[0]
    assert "Откат" not in title, f"вопрос всё ещё откатный: {title}"
    assert "удал" in text.lower(), text
    assert "привязк" in text.lower(), text


# ── первый запуск: вопроса нет, как и был ────────────────────────────────

def test_first_run_asks_nothing(bench):
    """OCR ещё не бежал и результата нет — запуск без вопроса."""
    ws, api = bench("validated_graph", has_ocr_result=False, stages=[])

    _click_ocr(ws)

    assert FakeMsgBox.calls == [], f"первый запуск спросил: {FakeMsgBox.calls}"
    assert api.started == [UID]


def test_running_ocr_is_a_repeat_too(bench):
    """Распознавание ещё идёт — второй клик тоже спрашивает.

    Результата на диске нет, но задача в очереди есть: молчаливый второй
    запуск удвоил бы минуты CPU. «Первый запуск» — это «не бежал И результата
    нет», обе половины.
    """
    ws, api = bench("validated_graph", has_ocr_result=False,
                    stages=_ocr_stage("running"))

    _click_ocr(ws)

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        "второй запуск поверх бегущего прошёл молча"
    )
    assert api.started == [UID]


# ── красная кнопка упавшего этапа ────────────────────────────────────────

def test_red_button_restart_also_asks(bench):
    """Второй путь оператора: перезапуск из окна отчёта об ошибке.

    Он тоже повторный — распознавание уже бежало, — поэтому цена называется
    и здесь. Окно отчёта при этом никуда не девается: сначала оно, потом вопрос.
    """
    ws, api = bench("error", has_ocr_result=False,
                    stages=_ocr_stage("failed"), error_stage="ocr",
                    feed_stages=False)

    assert "ocr" in ws._stage_errors, "красная кнопка не опознана — стенд не тот"

    _click_ocr(ws)

    assert StubErrorDialog.opened == ["Распознавание текста"], "окно отчёта не открылось"
    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        "перезапуск с красной кнопки прошёл молча"
    )
    assert api.started == [UID]


def test_red_button_refusal_starts_nothing(bench):
    """И на красной кнопке «Нет» останавливает запуск."""
    ws, api = bench("error", has_ocr_result=False,
                    stages=_ocr_stage("failed"), error_stage="ocr",
                    feed_stages=False)
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    _click_ocr(ws)

    assert api.started == [], "отказ на красной кнопке не остановил запуск"


def test_error_report_refusal_short_circuits(bench):
    """Отказ в САМОМ окне отчёта не доводит до вопроса — лишней модалки нет."""
    ws, api = bench("error", has_ocr_result=False,
                    stages=_ocr_stage("failed"), error_stage="ocr",
                    feed_stages=False)
    StubErrorDialog.retry = False

    _click_ocr(ws)

    assert FakeMsgBox.calls == [], f"лишний вопрос после отказа: {FakeMsgBox.calls}"
    assert api.started == []


# ── дверь привязки на время перезапуска (замечание приёмки, доработка №3) ──
#
# Замечание Максима: во время подтверждённого перезапуска распознавания кнопка
# и бусина «Привязка» ВРЕМЕНАМИ снова доступны. «Временами» — это статус:
# `_buttons_for_status` числит `ocr_binding` пройденным при `ocr_bound` и позже,
# поэтому `_start_ocr` гасил кнопку, а первый же тик опроса возвращал её на
# место. Дверь вела в пустую вкладку: сервер удаляет `OCR_RESULT` в самом
# начале `/ocr/start`.
#
# ⚠ Названная в замечании причина («`_ocr_notified` не сбрасывается») ЗАМЕРОМ НЕ
# подтвердилась: `_start_ocr` сбрасывает флаг и поднимает поллер с самого начала
# (см. `test_restart_resets_the_artifact_flag_and_starts_the_poll`). Настоящая
# причина — перекраска ПО СТАТУСУ, и лечится она отдельной меткой перезапуска.

# Статусы, при которых привязка включается ПО СТАТУСУ, а не по артефакту:
# ровно там замечание и воспроизводилось.
BINDING_GREEN_BY_STATUS = ["ocr_bound", "generating_fxml", "completed"]

# Где привязка вообще достижима: порог `_binding_reachable` — `validated_graph`
# (доработка pains-1). Раньше него дверь закрыта и БЕЗ перезапуска — вкладке
# нечего показывать, `graph_validated.json` ещё не существует.
BINDING_REACHABLE = [s for s in ALL_GREEN
                     if s not in ("building_graph", "built", "validating_graph")]


def _tick(ws, status):
    """Тик опроса статуса — то, что клиент делает каждые 2 секунды."""
    ws._apply_status(DiagramStatus(status))


def test_restart_resets_the_artifact_flag_and_starts_the_poll(bench):
    """Часть, которая работала и до правки, — заперта, чтобы не сломать её.

    Замер приёмки №3: `_ocr_notified` сбрасывается и поллер поднимается уже
    сейчас. Клетка нужна, чтобы правка метки перезапуска не «починила» это
    второй раз и чтобы наследник не искал дефект здесь.
    """
    ws, api = bench("completed")

    _click_ocr(ws)

    assert api.started == [UID]
    assert ws._ocr_notified is False, "флаг готовности не сброшен"
    assert ws._ocr_poll_timer.isActive(), "поллер артефакта не поднят"


@pytest.mark.parametrize("status", ALL_GREEN)
def test_binding_stays_closed_until_a_new_result(status, bench):
    """Гейт замечания: пока идёт перезапуск, привязки нет — при ЛЮБОМ статусе.

    Проверяется ТИК опроса, а не мгновение после клика: до правки кнопка
    гасла и возвращалась на первом же тике.
    """
    ws, api = bench(status)

    _click_ocr(ws)
    _tick(ws, status)

    assert not ws._action_buttons["ocr_binding"].isEnabled(), (
        f"{status}: привязка снова доступна во время перезапуска"
    )
    assert ws.beads.get_state(dw.BEAD_OCR_BINDING) == BeadState.UNAVAILABLE, status


@pytest.mark.parametrize("status", BINDING_REACHABLE)
def test_binding_returns_when_the_new_result_arrives(status, bench):
    """Порог с другой стороны: новый результат — и дверь открыта снова.

    Утверждается РАЗНИЦА: закрыта во время счёта, открыта после него. Без
    второй половины правка «погасить навсегда» осталась бы зелёной.
    """
    ws, api = bench(status)

    _click_ocr(ws)
    _tick(ws, status)
    assert not ws._action_buttons["ocr_binding"].isEnabled(), status

    api.has_ocr_result = True          # воркер положил новый ocr_result
    ws._check_ocr_artifact()
    _tick(ws, status)

    assert ws._ocr_rerun_in_flight() is False, "метка перезапуска не снята"
    assert ws._action_buttons["ocr_binding"].isEnabled(), (
        f"{status}: привязка не вернулась после нового результата"
    )


@pytest.mark.parametrize("status", ALL_GREEN)
def test_ocr_bead_spins_during_the_rerun(status, bench):
    """Бусина распознавания крутится по-настоящему, а не показывает «готово».

    Строки стадии на этот момент может ещё не быть — её заводит воркер, — и
    по статусу бусина остаётся зелёной. Оператор смотрел бы на «готово» у
    этапа, который в эту секунду считается заново.
    """
    ws, _api = bench(status)

    _click_ocr(ws)
    _tick(ws, status)

    assert ws.beads.get_state(dw.BEAD_OCR) == BeadState.IN_PROGRESS, status


@pytest.mark.parametrize("status", ["building_graph", "built", "validating_graph"])
def test_binding_stays_closed_before_its_own_threshold(status, bench):
    """Сторож границы: раньше «Проверки схемы» привязка закрыта и БЕЗ перезапуска.

    Порог `_binding_reachable` (доработка pains-1) старше этой правки: вкладка
    кладёт подписи на узлы ПРОВЕРЕННОГО графа, которого там ещё нет. Без этой
    клетки предыдущий тест пришлось бы писать по всем статусам и он краснел бы
    на чужом, законном замке.
    """
    ws, api = bench(status)

    api.has_ocr_result = True
    ws._check_ocr_artifact()
    _tick(ws, status)

    assert not ws._action_buttons["ocr_binding"].isEnabled(), status


def test_a_fresh_load_of_a_finished_diagram_keeps_the_binding_open(bench):
    """Сторож границы: метка перезапуска НЕ гасит привязку на свежей загрузке.

    `_ocr_notified` на готовой схеме тоже False (ветка опроса артефакта на
    статусы после контуров не заходит), поэтому гасить по нему было нельзя —
    оператор потерял бы дверь в законно пройденную привязку. Отдельная метка
    заведена ровно из-за этой клетки.
    """
    ws, _api = bench("completed")

    assert ws._ocr_notified is False, "стенд не воспроизводит свежую загрузку"
    assert ws._ocr_rerun_in_flight() is False
    assert ws._action_buttons["ocr_binding"].isEnabled(), (
        "привязка закрыта на схеме, где её давно прошли"
    )


def test_refused_restart_does_not_close_the_binding(bench):
    """Оператор ответил «Нет» — ничего не гаснет: перезапуска не было."""
    ws, api = bench("completed")
    FakeMsgBox.answer = QMessageBox.StandardButton.No

    _click_ocr(ws)
    _tick(ws, "completed")

    assert api.started == []
    assert ws._ocr_rerun_in_flight() is False
    assert ws._action_buttons["ocr_binding"].isEnabled()


# ── цвет пройденной привязки (второе замечание приёмки, доработка №3) ─────
#
# Замечание Максима: во время пересчёта раскладки ПРОЙДЕННАЯ привязка горит
# жёлтым. Три ветки параллельного OCR красили кнопку жёлтым БЕЗУСЛОВНО, стоило
# появиться `has_ocr_result`, — а жёлтый значит «сделай это». Замер §P3.11: при
# `ocr_bound`/`generating_fxml`/`completed` кнопка после загрузки зелёная, а
# после первого же тика поллера — жёлтая.

# Статусы, при которых привязка УЖЕ пройдена (порог `OCR_BOUND`).
BINDING_DONE = ["ocr_bound", "generating_fxml", "completed"]

# Статусы, при которых привязка ещё ВПЕРЕДИ, — там жёлтый честен.
BINDING_AHEAD = ["validated_graph", "extracting_contours",
                 "contours_extracted", "contours_validated"]


def _binding_colour(ws):
    css = ws._action_buttons["ocr_binding"].styleSheet()
    for name, style in (("жёлтая", dw._BTN_STYLE_YELLOW),
                        ("зелёная", dw._BTN_STYLE_GREEN),
                        ("серая", dw._BTN_STYLE_GRAY),
                        ("синяя", dw._BTN_STYLE_BLUE)):
        if css == style:
            return name
    return "иная"


def _artifact_branches(ws, status):
    """Прогнать ВСЕ три ветки параллельного OCR по очереди.

    Перебор ведётся списком, а не выборкой: правило было написано трижды, и
    замечание вылезло ровно потому, что чинили бы одну ветку из трёх.
    """
    yield "поллер", lambda: (setattr(ws, "_ocr_notified", False),
                             ws._check_ocr_artifact())
    yield "apply_status/B6.4", lambda: (setattr(ws, "_ocr_notified", False),
                                        ws._apply_status(DiagramStatus(status)))
    yield "apply_status/elif", lambda: (setattr(ws, "_ocr_notified", True),
                                        ws._apply_status(DiagramStatus(status)))


@pytest.mark.parametrize("status", BINDING_DONE)
def test_passed_binding_stays_green_on_every_branch(status, bench):
    """Пройденная привязка остаётся зелёной — какая бы ветка ни сработала."""
    ws, _api = bench(status)

    for name, run in _artifact_branches(ws, status):
        run()
        assert _binding_colour(ws) == "зелёная", f"{status}/{name}"
        assert ws.beads.get_state(dw.BEAD_OCR_BINDING) == BeadState.COMPLETED, (
            f"{status}/{name}"
        )


@pytest.mark.parametrize("status", BINDING_AHEAD)
def test_pending_binding_is_yellow_on_every_branch(status, bench):
    """Порог с другой стороны: пока привязка ВПЕРЕДИ, жёлтый честен.

    Без этой клетки правка «красить зелёным всегда» осталась бы зелёной, а
    оператор перестал бы видеть, что этап ждёт его работы.
    """
    ws, _api = bench(status)

    for name, run in _artifact_branches(ws, status):
        run()
        assert _binding_colour(ws) == "жёлтая", f"{status}/{name}"
        assert ws.beads.get_state(dw.BEAD_OCR_BINDING) == BeadState.AVAILABLE, (
            f"{status}/{name}"
        )


def test_all_three_branches_share_one_rule():
    """Правило цвета живёт в ОДНОЙ точке, а не тремя копиями.

    Сторож не про стиль: три копии одного правила и разъехались — их правили
    порознь. Пока ветки зовут общий метод, четвёртая копия не появится молча.
    """
    import inspect

    for func in (dw.DiagramWorkspace._apply_status,
                 dw.DiagramWorkspace._check_ocr_artifact):
        src = inspect.getsource(func)
        assert "_light_binding_button" in src, func.__name__
        assert "_BTN_STYLE_YELLOW" not in src, (
            f"{func.__name__}: правило цвета снова расписано на месте"
        )


# ── «Ручная правка» на время перезапуска (решение Максима, доработка №4) ──
#
# Холст «Ручной правки» несёт подписи, привязанные к узлам, а перезапуск
# распознавания сносит сырой результат на сервере — пересобирать холст поверх
# исчезнувшего OCR не на чем. Гасим ТОЙ ЖЕ меткой `_ocr_rerunning`, что и
# привязку; по завершении перезапуска доступность снова решает ГЕЙТ РАСКЛАДКИ
# штатным путём — сам гейт не тронут.

# Статусы, при которых «Ручная правка» вообще доступна (её порог — `OCR_BOUND`).
EDIT_GRAPH_REACHABLE = ["ocr_bound", "generating_fxml", "completed"]


@pytest.mark.parametrize("status", EDIT_GRAPH_REACHABLE)
def test_manual_edit_is_closed_while_ocr_reruns(status, bench):
    """Гейт пункта: во время перезапуска «Ручная правка» недоступна.

    Проверяется ТИК опроса, а не мгновение после клика: `_start_ocr` гасил
    кнопку и до правки, но первый же тик возвращал её по статусу (замер §P3.12).
    """
    ws, _api = bench(status)

    _click_ocr(ws)
    _tick(ws, status)

    assert not ws._action_buttons["edit_graph"].isEnabled(), (
        f"{status}: «Ручная правка» открыта поверх исчезнувшего OCR"
    )
    assert ws.beads.get_state(dw.BEAD_EDIT_GRAPH) == BeadState.UNAVAILABLE, status


@pytest.mark.parametrize("status", EDIT_GRAPH_REACHABLE)
def test_manual_edit_returns_by_the_layout_gate(status, bench):
    """По завершении OCR доступность решает гейт раскладки, как обычно.

    Утверждается РАЗНИЦА: закрыта во время перезапуска, открыта после него —
    и открыта именно ШТАТНЫМ путём, а не нашей меткой: гейт своего вердикта
    не менял, `_gate_blocked` мы не трогаем.
    """
    ws, api = bench(status)
    gate_before = getattr(ws, "_gate_blocked", False)

    _click_ocr(ws)
    _tick(ws, status)
    assert not ws._action_buttons["edit_graph"].isEnabled(), status

    api.has_ocr_result = True          # воркер положил новый ocr_result
    ws._check_ocr_artifact()
    _tick(ws, status)

    assert ws._ocr_rerun_in_flight() is False, "метка перезапуска не снята"
    assert ws._action_buttons["edit_graph"].isEnabled(), (
        f"{status}: «Ручная правка» не вернулась после нового результата"
    )
    assert getattr(ws, "_gate_blocked", False) == gate_before, (
        "правка залезла в состояние гейта раскладки"
    )


def test_the_rerun_mark_does_not_touch_the_layout_gate():
    """Сторож границы: в самом гейте раскладки правки нет.

    Пункт прямо это оговаривает. Ветка метки живёт в `_update_buttons` /
    `_update_beads`, а `_apply_layout_gate` про перезапуск не знает вовсе.
    """
    import inspect

    src = inspect.getsource(dw.DiagramWorkspace._apply_layout_gate)
    assert "_ocr_rerun" not in src and "_ocr_rerunning" not in src, (
        "гейт раскладки узнал про перезапуск — пункт этого не разрешал"
    )
    gate_src = inspect.getsource(__import__(
        "ui.services.layout_gate", fromlist=["gate_state"]).gate_state)
    assert "ocr" not in gate_src.lower().replace("ocr_bound", ""), (
        "модуль гейта заговорил про OCR"
    )


def test_manual_edit_closed_only_by_the_rerun_not_by_a_fresh_load(bench):
    """Свежая загрузка готовой схемы «Ручную правку» не гасит."""
    ws, _api = bench("completed")

    assert ws._ocr_rerun_in_flight() is False
    assert ws._action_buttons["edit_graph"].isEnabled()
    assert ws.beads.get_state(dw.BEAD_EDIT_GRAPH) == BeadState.COMPLETED
