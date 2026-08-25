# -*- coding: utf-8 -*-
"""Блок 1 болей (pains-1, 2026-08-25): бусина показывает РАБОТУ, а не статус.

Три жалобы оператора, у которых один корень — бусина живёт по чужому признаку:

* **1.4, OCR.** `_BEAD_DEFS` крутит бусину `ocr` на ВОСЬМИ статусах подряд
  (`building_graph` … `contours_validated`), поэтому кружок «идёт распознавание»
  вертится всю сборку графа и все контуры — независимо от того, бежит ли OCR.
  Лечение: «в процессе» ведём от ЖИВОЙ строки `/stages` (`stage_type: ocr`,
  `status: running`), а статусный набор сужен до мёртвого рудимента
  `OCR_PROCESSING` (в прямом конвейере он не присваивается нигде — только
  откатом; замер VERIFY_part_front2 Q1-a).
* **1.3, гейт раскладки.** `_apply_layout_gate` ставит бусину `edit_graph` в
  IN_PROGRESS на время счёта, а когда дверь открывается — чинит ТОЛЬКО кнопку
  (`_restyle_button`). Кружок крутился до ручного «Обновить».
* **1.5, Б1.** `load_diagram` не сбрасывал ни один из пяти кешей стадий и не
  включал слежение, поэтому кнопки и бусины НОВОЙ диаграммы решались стадиями
  ПРЕДЫДУЩЕЙ (а конфиг проекта — кешем `_project_code` чужого проекта).

Утверждения — о РАЗНИЦЕ (`PROTOCOL §3`): один и тот же статус при бегущей и не
бегущей стадии обязан давать РАЗНЫЕ бусины, иначе набор зелен и без лечения.
Сценарии идут ПОСЛЕ чужого действия (второй опрос, вторая диаграмма), а не с
чистого листа.

⚠ Модалки пути подменены (`PROTOCOL §5`): без подмены красный прогон не падает,
а виснет на `QMessageBox`.
⚠ Свои виджеты набор сносит сам, точечно (`sendPostedEvents(DeferredDelete)`),
а не общим `processEvents()` — замер §85/1-42.
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
from ui.services.api_client import (                        # noqa: E402
    APIClient, DiagramStatus, DiagramStatusInfo,
)
from ui.widgets.progress_beads import BeadState             # noqa: E402

UID_A = "aa11bb22-1111-2222-3333-444455556666"
UID_B = "bb22cc33-1111-2222-3333-444455556666"

# Пять кешей стадий воркспейса — те самые, что переживали смену диаграммы.
STAGE_CACHES = ("_last_stages", "_filled_keys", "_gate_blocked",
                "_layout_gate", "_project_code")


def stage_row(stage_type, status="running", row_id=1, fresh=True):
    """Строка `ProcessingStage`, как её отдаёт `/stages`.

    `fresh=True` — старт «только что»: гейт раскладки и глушение кнопки судят
    по `WAIT_LIMIT_S` = 600 с от `started_at`, и строка из прошлого года читалась
    бы как «висит дольше предела». Часов набор при этом не ЖДЁТ (`PROTOCOL §3`):
    отметка вычисляется один раз, дальше всё детерминировано.
    """
    started = (datetime.utcnow() if fresh else datetime(2026, 8, 20, 10, 0, 0))
    stamp = started.isoformat()
    return {
        "id": row_id,
        "stage_type": stage_type,
        "status": status,
        "attempt": 1,
        "error_message": None,
        "started_at": stamp,
        "created_at": stamp,
    }


class FakeAPI:
    """Поверхность `APIClient`, которой воркспейс пользуется в сценарии."""

    def __init__(self):
        self.stages = []
        self.has_ocr_result = False
        self.status = {UID_A: DiagramStatus.VALIDATED_GRAPH,
                       UID_B: DiagramStatus.VALIDATED_GRAPH}
        self.project = {UID_A: "thermohydraulics", UID_B: "electro"}
        self.stage_calls = 0

    def payload(self, uid):
        return {
            "uid": uid,
            "number": 7,
            "project_code": self.project[uid],
            "original_filename": "shema.png",
            "status": self.status[uid].value,
            "error_message": None,
            "error_stage": None,
            "cvat_task_id": None,
            "cvat_job_id": None,
        }

    def get_diagram(self, uid):
        """`DiagramInfo` собран НАСТОЯЩИМ клиентом из тела ответа сервера."""
        client = APIClient.__new__(APIClient)
        client._request = lambda *a, **kw: self.payload(uid)
        return APIClient.get_diagram(client, uid)

    def get_stages(self, uid):
        self.stage_calls += 1
        return list(self.stages)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": self.has_ocr_result}

    def get_stage_durations(self):
        return {}

    def download_artifact(self, uid, kind, path):
        raise OSError("холста нет")      # `_has_saved_canvas` → False, без модалки


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.watched = []
        self.unwatched = []

    def watch(self, uid):
        self.watched.append(uid)

    def unwatch(self, uid):
        self.unwatched.append(uid)

    def is_watching(self, uid):
        return uid in self.watched and uid not in self.unwatched


class FakeMsgBox:
    """Подмена модалки: висеть на ней набор не должен (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Yes

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args[2] if len(args) > 2 else ""))
        return cls.StandardButton.Ok


class Bench:
    """Воркспейс + рычаги сценария (опрос статуса, опрос стадий, тик поллера)."""

    def __init__(self, ws, api, provider):
        self.ws = ws
        self.api = api
        self.provider = provider

    def poll_status(self, status, uid=UID_A):
        """Опрос принёс статус — тем же сигналом, что и в бою."""
        self.api.status[uid] = status
        self.provider.status_updated.emit(uid, DiagramStatusInfo(status=status))

    def poll_stages(self, rows, uid=UID_A):
        """Опрос принёс строки `/stages`."""
        self.api.stages = list(rows)
        self.provider.stages_updated.emit(uid, list(rows))

    def ocr_tick(self):
        """Тик OCR-поллера — единственный источник свежести при остановленном опросе."""
        self.ws._check_ocr_artifact()

    def bead(self, idx):
        return self.ws.beads.get_state(idx)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    FakeMsgBox.calls = []
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    made = []

    def _make(status=DiagramStatus.VALIDATED_GRAPH, stages=(), has_ocr_result=False):
        api = FakeAPI()
        api.status[UID_A] = status
        api.stages = list(stages)
        api.has_ocr_result = has_ocr_result
        provider = FakeStatusProvider()
        ws = dw.DiagramWorkspace(api, provider)
        ws.show()
        ws.load_diagram(UID_A, "схема оператора")
        made.append(ws)
        return Bench(ws, api, provider)

    yield _make

    for ws in made:
        ws.cleanup()
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ── 1.4: бусина OCR крутится, пока бежит OCR ─────────────────────────────

def test_running_ocr_row_spins_the_bead(bench):
    """Строка `/stages` говорит `ocr running` — кружок крутится."""
    b = bench(status=DiagramStatus.VALIDATED_GRAPH)
    b.poll_stages([stage_row("ocr", status="running", row_id=9)])
    b.poll_status(DiagramStatus.VALIDATED_GRAPH)

    assert b.bead(dw.BEAD_OCR) is BeadState.IN_PROGRESS


def test_same_status_without_a_running_row_is_quiet(bench):
    """РАЗНИЦА, а не совпадение: тот же статус, но OCR не бежит — не крутится.

    Без этой половины утверждение выше было бы зелёным и у сегодняшнего
    клиента, который крутит бусину на восьми статусах подряд.
    """
    b = bench(status=DiagramStatus.VALIDATED_GRAPH)
    b.poll_stages([stage_row("contour_extraction", status="running", row_id=8)])
    b.poll_status(DiagramStatus.VALIDATED_GRAPH)

    assert b.bead(dw.BEAD_OCR) is BeadState.AVAILABLE


def test_graph_build_alone_does_not_spin_the_ocr_bead(bench):
    """Сама боль: сборка графа больше не выдаёт себя за распознавание."""
    b = bench(status=DiagramStatus.BUILDING_GRAPH)
    b.poll_stages([stage_row("graph_building", status="running", row_id=7)])
    b.poll_status(DiagramStatus.BUILDING_GRAPH)

    assert b.bead(dw.BEAD_OCR) is not BeadState.IN_PROGRESS


def test_contours_alone_does_not_spin_the_ocr_bead(bench):
    """Вторая половина набора из восьми статусов — контуры."""
    b = bench(status=DiagramStatus.CONTOURS_VALIDATED)
    b.poll_status(DiagramStatus.CONTOURS_VALIDATED)

    assert b.bead(dw.BEAD_OCR) is not BeadState.IN_PROGRESS


def test_poll_tick_is_the_source_of_freshness(bench):
    """При остановленном опросе свежесть приносит тик OCR-поллера.

    `validated_graph` числится финальным (`status_provider._FINAL_STATUSES`),
    и статусный опрос на нём снимается. Значит бусину обязан оживить тик
    `_ocr_poll_timer` — без единой смены статуса.
    """
    b = bench(status=DiagramStatus.VALIDATED_GRAPH)
    assert b.bead(dw.BEAD_OCR) is not BeadState.IN_PROGRESS

    b.api.stages = [stage_row("ocr", status="running", row_id=9)]
    b.ocr_tick()

    assert b.bead(dw.BEAD_OCR) is BeadState.IN_PROGRESS


def test_finished_ocr_stops_the_spin(bench):
    """Замок с другой стороны: строка `completed` + артефакт — бусина зелёная."""
    b = bench(status=DiagramStatus.VALIDATED_GRAPH)
    b.poll_stages([stage_row("ocr", status="running", row_id=9)])
    assert b.bead(dw.BEAD_OCR) is BeadState.IN_PROGRESS

    b.api.stages = [stage_row("ocr", status="completed", row_id=9)]
    b.api.has_ocr_result = True
    b.ocr_tick()

    assert b.bead(dw.BEAD_OCR) is BeadState.COMPLETED


# ── 1.3: гейт раскладки отпускает бусину сам ─────────────────────────────

def test_gate_spins_the_bead_while_layout_counts(bench):
    """Раскладка считается — бусина «Ручной правки» в процессе."""
    b = bench(status=DiagramStatus.OCR_BOUND)
    b.poll_stages([stage_row("layout", status="running", row_id=3)])

    assert b.bead(dw.BEAD_EDIT_GRAPH) is BeadState.IN_PROGRESS
    assert b.ws._layout_gate.waiting is True


def test_open_gate_returns_the_bead_to_its_status_state(bench):
    """Дверь открылась — кружок гаснет САМ, без ручного «Обновить».

    Сценарий идёт после чужого действия: первый опрос застал счёт, второй
    принёс готовую раскладку — ровно так это и приходит в бою.
    """
    b = bench(status=DiagramStatus.OCR_BOUND)
    b.poll_stages([stage_row("layout", status="running", row_id=3)])
    assert b.bead(dw.BEAD_EDIT_GRAPH) is BeadState.IN_PROGRESS

    b.poll_stages([stage_row("layout", status="completed", row_id=3)])

    assert b.ws._layout_gate.allow is True
    assert b.bead(dw.BEAD_EDIT_GRAPH) is BeadState.AVAILABLE


def test_open_gate_keeps_the_button_alive(bench):
    """Кнопку гейт чинил и раньше — правка не имеет права это отнять."""
    b = bench(status=DiagramStatus.OCR_BOUND)
    b.poll_stages([stage_row("layout", status="running", row_id=3)])
    b.poll_stages([stage_row("layout", status="completed", row_id=3)])

    assert b.ws._action_buttons["edit_graph"].isEnabled() is True


# ── 1.5 (Б1): смена диаграммы обнуляет состояние стадий ──────────────────

def test_second_diagram_starts_with_empty_caches(bench):
    """Пять кешей — пустые, слежение включено на НОВЫЙ uid.

    Кеши наполняются ЧУЖОЙ диаграммой по-настоящему: опрос стадий (заливка
    кнопок и гейт) плюс запрос кода проекта, который клиент кеширует на
    первый вопрос вкладки.
    """
    b = bench(status=DiagramStatus.OCR_BOUND)
    b.poll_stages([stage_row("layout", status="running", row_id=3)])
    assert b.ws._get_project_code() == "thermohydraulics"
    filled = {name: getattr(b.ws, name, None) for name in STAGE_CACHES}
    assert all(filled.values()), filled          # предыстория действительно есть

    b.ws.load_diagram(UID_B, "другая схема")

    for name in STAGE_CACHES:
        assert not getattr(b.ws, name, None), f"{name} пережил смену диаграммы"
    assert UID_B in b.provider.watched


def test_second_diagram_gets_its_own_project_code(bench):
    """Наблюдаемое следствие кеша `_project_code`: конфиг чужого проекта.

    Вкладки спрашивают код проекта у воркспейса; до правки вторая диаграмма
    получала код первой и открывалась с чужим словарём классов.
    """
    b = bench(status=DiagramStatus.OCR_BOUND)
    assert b.ws._get_project_code() == "thermohydraulics"

    b.ws.load_diagram(UID_B, "другая схема")

    assert b.ws._get_project_code() == "electro"


def test_second_diagram_does_not_inherit_a_closed_gate(bench):
    """Замок гейта принадлежит диаграмме, а не окну.

    До правки `_gate_blocked`/`_layout_gate` первой схемы держали кнопку
    «Ручной правки» второй закрытой до первого же опроса стадий.
    """
    b = bench(status=DiagramStatus.OCR_BOUND)
    b.poll_stages([stage_row("layout", status="running", row_id=3)])
    assert b.ws._gate_blocked is True

    b.api.status[UID_B] = DiagramStatus.OCR_BOUND
    b.ws.load_diagram(UID_B, "другая схема")

    assert getattr(b.ws, "_gate_blocked", False) is False
    assert b.bead(dw.BEAD_EDIT_GRAPH) is not BeadState.IN_PROGRESS
