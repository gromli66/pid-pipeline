# -*- coding: utf-8 -*-
"""Пункт 1.3 дороги (Н6-частично) — открытие и закрытие не откатывают молча.

Зачем. В воркспейсе жили ДВА отката, которых оператор не просил:

  • при ОТКРЫТИИ — `_self_heal_stuck_stage` (`diagram_workspace.py:714`), из
    `load_diagram`. Диаграмма в ручном `*ING`-статусе → `rollback_diagram(uid,
    target)` **без preserve-флагов**. Для «Проверки схемы» это откат до `built`,
    то есть снос `graph_validated`, обоих контурных артефактов, всех четырёх
    OCR-овых, холста (файл `graph_canvas.json` сносится с диска) и FXML;
  • при ЗАКРЫТИИ — `_rollback_if_validating`, из `_close_active_tab` и
    `_force_close_tab`. Флаги там были (`preserve_ocr`/`preserve_contours` для
    `val_graph`), но ключ вкладки — `"graph_val"` (`:1931`), а таблица
    `_VALIDATING_ROLLBACK` знала `"val_graph"` (`:1284`): для графовой вкладки
    ветка не срабатывала никогда, и диаграмму подбирало самолечение — без
    флагов. Для «Очистки рамки» ветка срабатывала и сносила `original_cleaned`
    вместе с сохранённой оператором очисткой (возврат `image_raw.png`).

Чинить надо было НЕ ключ: совпадение ключей лишь включило бы штатный откат со
всеми его последствиями (ловушка `PLAN_AUDIT §Этап 1`).

Замер §51 (`tools/`-стенд не нужен, считается из таблиц воркспейса): откат
покупал РОВНО ОДИН цвет бусины и НИ ОДНОЙ кнопки. Для всех четырёх пар
«*ING → стабильный» набор доступных/пройденных/бегущих кнопок совпадает
побитово (`_buttons_for_status` + `_MANUAL_INPROGRESS`), различие бусины есть
только у `frame` и `junction` (IN_PROGRESS против AVAILABLE). Поэтому оба
молчаливых отката сняты, а цвет бусины восстановлен той же поправкой
`_MANUAL_INPROGRESS`, что уже держала кнопку кликабельной.

Что проверяется — только ДАННЫЕ (принцип набора 0.4): множество артефактов на
сервере и журнал вызовов `rollback_diagram`. Ни одного утверждения про
внутренние поля вкладок. Числа абсолютные и заперты с двух сторон: и «ничего не
исчезло», и «до правки исчезало ровно 9 из 11».

Удаление в поддельном сервере считает НАСТОЯЩАЯ `app.api.rollback
._artifacts_to_delete` — иначе стенд судил бы по собственному представлению о
том, что откат сносит.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest                                            # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QObject, Signal       # noqa: E402
from PySide6.QtWidgets import (                          # noqa: E402
    QApplication, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget,
)

from app.api.rollback import _artifacts_to_delete        # noqa: E402
from app.models import ArtifactType                      # noqa: E402
from app.models import DiagramStatus as SrvStatus        # noqa: E402

import ui.widgets.diagram_workspace as dw                # noqa: E402
from ui.services.api_client import DiagramStatus         # noqa: E402
from ui.widgets.progress_beads import BeadState          # noqa: E402

UID = "d74eb9f1-1111-2222-3333-444455556666"

# Состояние «оператор вернулся в Проверку схемы»: граф собран, схема выправлена,
# контуры посчитаны и поправлены руками, OCR отработал, холст и FXML есть.
GRAPH_STATE = [
    ArtifactType.GRAPH_JSON,
    ArtifactType.GRAPH_OVERLAY,
    ArtifactType.GRAPH_VALIDATED,
    ArtifactType.CONTOURS_AUTO,
    ArtifactType.CONTOURS_VALIDATED,
    ArtifactType.OCR_CLEANED,
    ArtifactType.OCR_RESULT,
    ArtifactType.OCR_BINDING,
    ArtifactType.OCR_VALIDATION,
    ArtifactType.GRAPH_CANVAS,
    ArtifactType.FXML,
]
# Откат до `built` без preserve-флагов оставляет только два артефакта сборки.
SURVIVES_ROLLBACK_TO_BUILT = {
    ArtifactType.GRAPH_JSON,
    ArtifactType.GRAPH_OVERLAY,
}


# ── харнесс ──────────────────────────────────────────────────────────────

class FakeServer:
    """Сервер: статус, множество артефактов и журнал откатов.

    Что именно сносит откат, считает настоящая `_artifacts_to_delete`.
    """

    def __init__(self, status, artifacts):
        self.status = status
        self.artifacts = set(artifacts)
        self.rollbacks = []

    def rollback(self, target, preserve_ocr, preserve_contours):
        self.rollbacks.append((target, preserve_ocr, preserve_contours))
        doomed = set(_artifacts_to_delete(
            SrvStatus(target),
            preserve_ocr=preserve_ocr,
            preserve_contours=preserve_contours,
        ))
        self.artifacts -= doomed
        self.status = DiagramStatus(target)
        return {"deleted_artifacts": len(doomed)}


class FakeDiagram:
    def __init__(self, server):
        self.status = server.status
        self.error_stage = None
        self.project_code = "thermohydraulics"


class FakeAPI:
    """Поверхность APIClient, которой пользуется воркспейс в этом сценарии."""

    def __init__(self, server):
        self.server = server

    def get_diagram(self, uid):
        return FakeDiagram(self.server)

    def get_ocr_status(self, uid):
        return {"has_ocr_result": True}

    def get_stages(self, uid):
        return []

    def rollback_diagram(self, uid, target_status,
                         preserve_ocr=False, preserve_contours=False):
        return self.server.rollback(target_status, preserve_ocr, preserve_contours)

    def start_graph_validation(self, uid):
        self.server.status = DiagramStatus.VALIDATING_GRAPH
        return {"status": "validating_graph"}

    def start_frame_removal(self, uid):
        self.server.status = DiagramStatus.CLEANING_FRAME
        return {"status": "cleaning_frame"}


class FakeStatusProvider(QObject):
    status_updated = Signal(str, object)
    stages_updated = Signal(str, object)

    def watch(self, uid):
        pass

    def unwatch(self, uid):
        pass

    def is_watching(self, uid):
        return False


class StubTab(QWidget):
    """Вкладка-заглушка: тулбар первым элементом — воркспейс врежет «← Назад»."""

    confirmed = Signal()
    status_message = Signal(str)

    def __init__(self, diagram_uid=None, diagram_name=None, api_client=None,
                 parent=None):
        super().__init__(parent)
        self._confirmed = False
        root = QVBoxLayout(self)
        root.addLayout(QHBoxLayout())

    def set_project_code(self, code):
        pass


class FakeMsgBox:
    """Подмена модального диалога: диалог в пути закрытия штатен, и зонд по
    нему обязан падать, а не виснуть (`PROTOCOL §5`)."""

    StandardButton = QMessageBox.StandardButton
    calls = []
    answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args[1] if len(args) > 1 else ""))
        return cls.answer

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Ok

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args[1] if len(args) > 1 else ""))
        return cls.StandardButton.Ok


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bench(qapp, monkeypatch):
    """Собрать воркспейс на поддельном сервере; вкладки — заглушки."""
    FakeMsgBox.calls = []
    FakeMsgBox.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(dw, "QMessageBox", FakeMsgBox)
    monkeypatch.setattr("ui.tabs.simple_graph_tab.SimpleGraphTab", StubTab)
    monkeypatch.setattr("ui.tabs.frame_tab.FrameTab", StubTab)

    made = []

    def _make(status, artifacts):
        server = FakeServer(status, artifacts)
        ws = dw.DiagramWorkspace(FakeAPI(server), FakeStatusProvider())
        made.append(ws)
        return ws, server

    yield _make

    # Свои воркспейсы набор сносит сам, детерминированно (`PROTOCOL §5`,
    # форма 1-36): брошенные виджеты живут до конца процесса и штрафуют
    # СОСЕДА — доставку событий и его же `processEvents()`.
    for ws in made:
        ws.hide()
        ws.setParent(None)
        ws.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _open_close_open(ws, key):
    """Жест оператора: открыть диаграмму → открыть вкладку → ← Назад → выйти."""
    ws.load_diagram(UID, "схема оператора")
    ws._action_buttons[key].click()
    assert ws._active_tab is not None, f"вкладка «{key}» не открылась"
    ws._btn_back_injected.click()
    assert ws._active_tab is None, f"вкладка «{key}» не закрылась"
    ws.cleanup()


# ── сценарий гейта: открыть — закрыть — открыть, три раза подряд ─────────

def test_graph_tab_cycles_keep_canvas_and_contours(bench):
    """Холст, ручные контуры, OCR и FXML переживают три полных цикла."""
    ws, server = bench(DiagramStatus.VALIDATING_GRAPH, GRAPH_STATE)

    for cycle in range(1, 4):
        _open_close_open(ws, "val_graph")
        assert server.artifacts == set(GRAPH_STATE), (
            f"цикл {cycle}: с сервера пропало "
            f"{sorted(a.value for a in set(GRAPH_STATE) - server.artifacts)}"
        )
        assert len(server.artifacts) == 11, f"цикл {cycle}: артефактов не 11"


def test_graph_tab_cycles_issue_no_rollback(bench):
    """За три цикла клиент не отправил ни одного отката."""
    ws, server = bench(DiagramStatus.VALIDATING_GRAPH, GRAPH_STATE)

    for _ in range(3):
        _open_close_open(ws, "val_graph")

    assert server.rollbacks == [], f"молчаливые откаты: {server.rollbacks}"


def test_graph_tab_cycles_ask_nothing(bench):
    """Ни одного диалога: жест «зашёл — вышел» ничего не спрашивает."""
    ws, server = bench(DiagramStatus.VALIDATING_GRAPH, GRAPH_STATE)

    for _ in range(3):
        _open_close_open(ws, "val_graph")

    assert FakeMsgBox.calls == [], f"лишние диалоги: {FakeMsgBox.calls}"


def test_rollback_to_built_would_cost_nine_of_eleven():
    """Замер, ради которого пункт заведён: откат уносил 9 артефактов из 11.

    Число снято с настоящей `_artifacts_to_delete`, не с представлений стенда.
    """
    doomed = set(_artifacts_to_delete(SrvStatus.BUILT))
    survives = set(GRAPH_STATE) - doomed
    assert survives == SURVIVES_ROLLBACK_TO_BUILT
    assert len(set(GRAPH_STATE) - survives) == 9


# ── закрытие вкладки: очистка рамки ──────────────────────────────────────

def test_frame_tab_close_keeps_saved_cleaning(bench):
    """Сохранённая очистка рамки переживает «← Назад» без подтверждения."""
    ws, server = bench(DiagramStatus.CLEANING_FRAME, [ArtifactType.ORIGINAL_CLEANED])

    _open_close_open(ws, "frame")

    assert ArtifactType.ORIGINAL_CLEANED in server.artifacts, (
        "очистка рамки снесена откатом при закрытии вкладки"
    )
    assert server.rollbacks == [], f"молчаливые откаты: {server.rollbacks}"


# ── явный откат оператора — не тронут ────────────────────────────────────

def test_operator_rollback_still_works(bench):
    """Кнопка пройденного этапа ФАЗЫ A по-прежнему спрашивает и откатывает.

    Порог заперт с другой стороны: сняты откаты, которых оператор не просил,
    а не его возможность откатить.

    ⚠ Осознанный пересъём (блок 3 «точечных болей», пункт Н3+, 2026-08-25):
    прежняя редакция брала кнопку «Проверка схемы». Она в фазе B, а вход в
    пройденный этап фазы B откатом больше НЕ является (решения Максима
    №7/№8) — клик уходит прямо во вкладку. Замена на «Сборку схемы» не
    ослабляет утверждение: фаза A линейна, повторный проход там разрушающий,
    и диалог отката остаётся именно у неё. Что фаза B теперь молчит,
    утверждает `tests/ui/test_phase_b_free_entry.py`.
    """
    ws, server = bench(DiagramStatus.CONTOURS_VALIDATED, GRAPH_STATE)
    ws.load_diagram(UID, "схема оператора")

    ws._action_buttons["graph"].click()

    assert [c[0] for c in FakeMsgBox.calls] == ["question"], (
        f"явный откат перестал спрашивать: {FakeMsgBox.calls}"
    )
    assert server.rollbacks == [("validated_junctions", True, True)], (
        f"явный откат ушёл не так: {server.rollbacks}"
    )
    # preserve-флаги на месте: независимые OCR и контуры пережили явный откат.
    assert ArtifactType.CONTOURS_VALIDATED in server.artifacts
    assert ArtifactType.OCR_RESULT in server.artifacts
    assert ArtifactType.GRAPH_VALIDATED not in server.artifacts
    assert ArtifactType.GRAPH_CANVAS not in server.artifacts


def test_phase_b_entry_is_no_longer_a_rollback(bench):
    """Второй берег того же пересъёма — на том же стенде, что и первый.

    Клик по пройденной «Проверке схемы» ни о чём не спрашивает и ничего не
    сносит: артефакты фазы B на месте, журнал откатов пуст.
    """
    ws, server = bench(DiagramStatus.CONTOURS_VALIDATED, GRAPH_STATE)
    ws.load_diagram(UID, "схема оператора")

    ws._action_buttons["val_graph"].click()

    assert FakeMsgBox.calls == [], f"вход в фазу B снова спрашивает: {FakeMsgBox.calls}"
    assert server.rollbacks == [], f"вход в фазу B откатил конвейер: {server.rollbacks}"
    assert ArtifactType.GRAPH_VALIDATED in server.artifacts
    assert ArtifactType.GRAPH_CANVAS in server.artifacts


# ── бусина и кнопка после «зашёл — вышел» ────────────────────────────────

@pytest.mark.parametrize("key, bead, status", [
    ("frame", dw.BEAD_FRAME, DiagramStatus.CLEANING_FRAME),
    ("junction", dw.BEAD_VAL_JUNCTION, DiagramStatus.VALIDATING_JUNCTIONS),
])
def test_stage_stays_reachable_without_rollback(bench, key, bead, status):
    """Прерванный этап виден доступным и кликабельным — без отката.

    Ровно та картинка, которую раньше рисовал откат: замер §51 — набор кнопок
    у `*ING` и у стабильного статуса совпадает, бусина различалась.
    """
    ws, server = bench(status, GRAPH_STATE)
    ws.load_diagram(UID, "схема оператора")

    assert ws._action_buttons[key].isEnabled(), f"кнопка «{key}» мертва"
    assert ws.beads.get_state(bead) == BeadState.AVAILABLE, (
        f"бусина «{key}» показывает {ws.beads.get_state(bead)}"
    )
    assert server.rollbacks == [], f"молчаливые откаты: {server.rollbacks}"
