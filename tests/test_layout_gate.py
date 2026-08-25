# -*- coding: utf-8 -*-
"""Гейт «Ручной правки»: не пускаем, пока раскладка не готова.

Правило вынесено из виджета в чистый модуль ровно затем, чтобы его можно было
проверить без GUI. Замок здесь опаснее обычного бага: если гейт закроется и не
откроется, оператор не попадёт в схему вовсе — поэтому **неизвестные** исходы
по-прежнему трактуются в пользу входа.

⚠ **Пересъём 2026-08-26 (mefx-8, решение Максима №9).** Два исхода перестали
быть «неизвестными» и переехали из «пустить» в «замок»: `failed` и
просроченное ожидание. Раньше вход в них имел смысл — вкладка собирала
аварийный холст `pretransform`-ом, и оператор хотя бы видел схему. Блок 8
аварийную пересборку убрал (`base_graph_tab.canvas_verdict`), поэтому «пустить
без раскладки» стало «пустить в пустой экран»: замок честнее, и он обязан
называть дверь повтора. Тесты `..._lets_operator_in_...` и `test_wait_has_a_limit`
пересняты сюда с обратной полярностью — это не регрессия, а смена решения.

Граница замка названа прямо: запирают только ПОЛОЖИТЕЛЬНО известные «раскладки
не будет» (упала) и «ждём дольше предела». Отсутствие стадии, чужие стадии и
стадия без отметок времени — по-прежнему вход, потому что предел ожидания для
них не наступает никогда и замок стал бы вечным.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ui.services.layout_gate import (
    LOCK_DEAD_END, RETRY_DOOR, WAIT_LIMIT_S, gate_state, layout_task_pending,
)

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


def test_failed_layout_locks_the_door_and_names_the_retry():
    """Раскладка упала — замок с повтором (решение №9), а не вход с модалкой.

    Утверждается РАЗНИЦА с соседней клеткой: на `completed` та же функция
    отдаёт `allow=True` (тест выше), значит замок здесь — вердикт про упавшую
    стадию, а не «дверь заперта всегда».
    """
    g = gate_state([_stage("failed", error_message="брокер недоступен")],
                   "ocr_bound", now=NOW)
    assert g.allow is False and g.waiting is False
    assert "брокер недоступен" in g.reason, g.reason
    assert RETRY_DOOR in g.reason, (
        f"замок не называет дверь повтора: {g.reason!r}")
    assert g.warn is None, "модалка «схема откроется без раскладки» — ложь: "\
                           "аварийного холста больше нет"
    # ⛔ И ЦЕНА ВЫХОДА, если дверь не сработала. Диспетчер ставит задачу только
    # на СМЕНУ истины: под упавшей строкой может лежать целый холст, и тогда
    # повторное подтверждение «Контуров» отвечает ALREADY_FRESH — замок вечный.
    # Решение Максима 2026-08-26: в этом состоянии не пускать; значит замок
    # обязан назвать настоящий выход (откат, стирающий холст), а не только дверь.
    assert LOCK_DEAD_END in g.reason, (
        f"замок молчит о том, что делать, если дверь не сработала: {g.reason!r}")
    assert "откатом" in g.reason and "привязку" in g.reason


def test_overdue_wait_locks_the_door_too():
    """Ожидание сверх предела — тот же замок: собирать вместо раскладки нечем.

    ⚠ Предел считается ТОЛЬКО от `started_at`: `created_at` до клиента не
    доезжает вовсе (см. `test_stage_without_timestamps_opens_the_gate`),
    поэтому эта клетка достижима лишь у стадии, которую воркер уже начал.
    """
    g = gate_state([_stage("running", WAIT_LIMIT_S + 1)], "ocr_bound", now=NOW)
    assert g.allow is False and g.waiting is False, (
        "просроченная задача снова пускает в пустую вкладку")
    assert RETRY_DOOR in g.reason, g.reason
    assert str(int(WAIT_LIMIT_S // 60)) in g.reason, (
        f"замок не называет срок: {g.reason!r}")


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

    ⚠ mefx-8: модалка «схема откроется как есть» отсюда УБРАНА — она обещала
    аварийный холст, которого больше нет. Правду теперь говорит сама вкладка
    (экран «Раскладка пересчитывается» / дверь в «Контуры»), а гейт остаётся
    при одном решении: не запирать по незнанию.
    """
    row = {"id": 1, "stage_type": "layout", "status": "pending"}
    g = gate_state([row], "ocr_bound", now=NOW)
    assert g.allow is True and g.waiting is False
    assert g.warn is None


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


# ── 8.2: «задача бежит» против «задачи нет» ───────────────────────────────
#
# Предикат отдельный от гейта: гейт решает про КНОПКУ, вкладка — про ЭКРАН.
# Ошибка здесь стоит ровно того, ради чего блок 8 и делался: «пересчитывается»
# при отсутствующей задаче — ложь навечно, дверь в «Контуры» при бегущей —
# выброшенные 2–5 минут счёта.


def test_pending_and_running_are_a_real_task():
    for st in ("pending", "running"):
        assert layout_task_pending([_stage(st, 10)]) is True, st


def test_finished_stages_are_not_a_task():
    """`completed`/`failed`/`skipped` — считать больше нечего: это дверь.

    `completed` тут не формальность: холст умеет родиться протухшим при
    завершённой задаче (угол `ALREADY_RUNNING` диспетчера), и ждать в этом
    случае нечего — ждали бы вечно.
    """
    for st in ("completed", "failed", "skipped"):
        assert layout_task_pending([_stage(st, 10)]) is False, st


def test_no_stages_is_not_a_task():
    assert layout_task_pending([]) is False
    assert layout_task_pending(None) is False
    assert layout_task_pending(
        [{"id": 3, "stage_type": "ocr", "status": "running"}]) is False, \
        "чужая бегущая стадия принята за раскладку"


def test_only_the_latest_layout_attempt_counts():
    """Прошлая попытка упала, новая бежит — задача ЕСТЬ (и наоборот)."""
    stages = [dict(_stage("failed", 100), id=1), dict(_stage("running", 5), id=2)]
    assert layout_task_pending(stages) is True
    stages = [dict(_stage("running", 100), id=1),
              dict(_stage("completed", 5), id=2)]
    assert layout_task_pending(stages) is False, (
        "старая бегущая строка держит экран ожидания после новой готовой")
