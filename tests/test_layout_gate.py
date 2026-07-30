# -*- coding: utf-8 -*-
"""Гейт «Ручной правки»: не пускаем, пока раскладка не готова.

Правило вынесено из виджета в чистый модуль ровно затем, чтобы его можно было
проверить без GUI. Замок здесь опаснее обычного бага: если гейт закроется и не
откроется, оператор не попадёт в схему вовсе — поэтому все «неизвестные»
исходы трактуются в пользу входа.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ui.services.layout_gate import WAIT_LIMIT_S, gate_state

NOW = datetime(2026, 7, 28, 12, 0, 0)


def _stage(status, started_delta_s=0, **extra):
    row = {"id": 1, "stage_type": "layout", "status": status}
    if started_delta_s is not None:
        row["started_at"] = (NOW - timedelta(seconds=started_delta_s)).isoformat()
    row.update(extra)
    return row


def test_binding_not_passed_keeps_door_closed():
    """Гейт и прячет 2-5 минут счёта: «не пускаем, пока ты с текстом не поработаешь»."""
    g = gate_state([_stage("completed")], "contours_validated", now=NOW)
    assert g.allow is False and g.waiting is False
    assert "привязк" in g.reason


def test_ocr_completed_alone_is_not_enough():
    """OCR_COMPLETED достижим только откатом бусины привязки — значит привязку
    надо пройти заново, и вкладку открывать рано."""
    g = gate_state([_stage("completed")], "ocr_completed", now=NOW)
    assert g.allow is False


def test_running_layout_makes_the_button_wait_with_progress():
    g = gate_state([_stage("running", 30)], "ocr_bound", now=NOW)
    assert g.allow is False and g.waiting is True
    assert g.warn is None


def test_completed_layout_opens_the_door():
    g = gate_state([_stage("completed")], "ocr_bound", now=NOW)
    assert g.allow is True and g.waiting is False and g.warn is None


def test_skipped_layout_opens_the_door():
    """SKIPPED — штатный исход гонки (истина сменилась / холст правился руками),
    ждать нечего."""
    g = gate_state([_stage("skipped")], "ocr_bound", now=NOW)
    assert g.allow is True and g.waiting is False


def test_failed_layout_lets_operator_in_with_a_message():
    g = gate_state([_stage("failed", error_message="брокер недоступен")],
                   "ocr_bound", now=NOW)
    assert g.allow is True and g.waiting is False
    assert "брокер недоступен" in g.warn


def test_wait_has_a_limit():
    """Иначе упавший брокер или потерянная задача запирают оператора навсегда."""
    g = gate_state([_stage("running", WAIT_LIMIT_S + 1)], "ocr_bound", now=NOW)
    assert g.allow is True and g.waiting is False
    assert g.warn and "без неё" in g.warn


def test_wait_limit_not_reached_still_waits():
    g = gate_state([_stage("running", WAIT_LIMIT_S - 1)], "ocr_bound", now=NOW)
    assert g.allow is False and g.waiting is True


def test_no_layout_stage_at_all_opens_the_door():
    """Старые схемы и случай «диспетчер не сработал»: замок был бы хуже
    отсутствия раскладки."""
    g = gate_state([], "ocr_bound", now=NOW)
    assert g.allow is True
    g2 = gate_state([{"id": 3, "stage_type": "ocr", "status": "completed"}],
                    "ocr_bound", now=NOW)
    assert g2.allow is True


def test_latest_stage_wins():
    """Повторное закрытие контуров: старая стадия skipped, новая бежит."""
    stages = [
        {"id": 1, "stage_type": "layout", "status": "skipped"},
        dict(_stage("running", 10), id=2),
    ]
    g = gate_state(stages, "ocr_bound", now=NOW)
    assert g.allow is False and g.waiting is True


def test_stage_without_timestamps_opens_the_gate():
    """Стадия без единой отметки времени НЕ запирает (решение 2026-07-30).

    Прежнее правило («ждём») выглядело осторожным, а на деле запирало
    навсегда: у стадии, застрявшей в `pending`, нет `started_at`, а
    `created_at` API отдаёт как None — значит предел ожидания не наступал
    никогда, и оператор не мог войти во вкладку вообще. Цена ошибок
    несимметрична: ложный пропуск — увидел холст без раскладки и перезашёл;
    ложный запрет — не может работать. Гейт закрывается ТОЛЬКО по
    положительному знанию «стадия сейчас бежит».
    """
    row = {"id": 1, "stage_type": "layout", "status": "pending"}
    g = gate_state([row], "ocr_bound", now=NOW)
    assert g.allow is True and g.waiting is False
    assert g.warn                      # но предупреждаем, что раскладки может не быть


def test_running_with_started_at_waits():
    """А вот когда стадия РЕАЛЬНО бежит — ждём, это и есть смысл гейта."""
    row = {"id": 1, "stage_type": "layout", "status": "running",
           "started_at": NOW.isoformat()}
    g = gate_state([row], "ocr_bound", now=NOW)
    assert g.allow is False and g.waiting is True


def test_export_and_completed_statuses_are_open():
    for status in ("generating_fxml", "completed"):
        g = gate_state([_stage("completed")], status, now=NOW)
        assert g.allow is True, status
