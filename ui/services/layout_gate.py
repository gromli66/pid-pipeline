# -*- coding: utf-8 -*-
"""layout_gate.py — пускать ли оператора в «Ручную правку».

Решение заказчика 2026-07-28 (§3.2 плана docs/planning/
AUTO_LAYOUT_INTEGRATION.md): вкладка не открывается, пока не пройдена привязка
и не готова раскладка. Этот гейт и прячет 2-5 минут счёта — «не пускаем, пока
ты с текстом не поработаешь».

Состояние определяется **стадией `layout`**, а не наличием холста: отсутствие
файла неразличимо между «считает», «упала» и «не стартовала» (диспетчер при
недоступном брокере вернёт `dispatch_failed`, и никакого артефакта не будет).

Ожидание — с пределом. Если задача висит дольше лимита, оператора пускают:
он увидит `pretransform`-холст без раскладки, но с явным сообщением, а не
бесконечный замок.

Модуль чистый (без Qt и без сети), чтобы правило проверялось тестом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Предел ожидания раскладки. Замер на dev-CPU: 936 узлов — 72 с; план
# оценивает боевой CPU-only в 2-5 мин. Лимит с запасом; уточняется тем же
# замером, что и `soft_time_limit` задачи (docs/AUTO_LAYOUT_E2E_TESTPLAN.md §5).
WAIT_LIMIT_S = 600.0

STAGE_TYPE = "layout"

# Статусы, при которых привязка уже пройдена и гейт по статусу открыт.
# OCR_COMPLETED в прямом конвейере не присваивается вовсе (OCR-таск статус не
# меняет) — он появляется только откатом бусины привязки, и оттуда оператор
# обязан пройти привязку заново.
_BOUND_STATUSES = ("ocr_bound", "generating_fxml", "completed")


@dataclass
class GateState:
    """Что показывать на кнопке «Ручная правка»."""
    allow: bool                 # пускать во вкладку
    waiting: bool               # идёт счёт — кнопка с процентами, не серая
    reason: str                 # для тултипа, лога и сообщения оператору
    warn: Optional[str] = None  # предупреждение при входе (пускаем, но не всё гладко)


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").split("+")[0])
    except ValueError:
        return None


def _latest_layout(stages):
    rows = [s for s in (stages or [])
            if (s.get("stage_type") or "").lower() == STAGE_TYPE]
    if not rows:
        return None
    return max(rows, key=lambda s: s.get("id") or 0)


def gate_state(stages, status_value: str, *, now: Optional[datetime] = None,
               wait_limit_s: float = WAIT_LIMIT_S) -> GateState:
    """Пускать ли в «Ручную правку». status_value — значение DiagramStatus."""
    status = (status_value or "").lower()

    if status not in _BOUND_STATUSES:
        return GateState(
            False, False,
            "сначала подтвердите привязку подписей")

    row = _latest_layout(stages)
    if row is None:
        # Раскладки для этой схемы не заводилось: старая диаграмма или
        # диспетчер не сработал. Замок здесь был бы хуже отсутствия раскладки.
        return GateState(True, False, "раскладка не запускалась")

    st = (row.get("status") or "").lower()

    if st in ("pending", "running"):
        # ОТСЧЁТ ОТ created_at, если старта не было. Задача, застрявшая в
        # `pending` (брокер недоступен, воркер не поднялся), не имеет
        # `started_at` — раньше предел ожидания для неё не наступал никогда,
        # и оператор оставался заперт без единого способа войти.
        started = _parse_dt(row.get("started_at")) or _parse_dt(
            row.get("created_at"))
        if started is not None:
            elapsed = ((now or datetime.utcnow()) - started).total_seconds()
            if elapsed > wait_limit_s:
                return GateState(
                    True, False, "раскладка считается дольше обычного",
                    warn="Раскладка не успела посчитаться. Схема откроется без "
                         "неё — расположение элементов останется прежним.")
        else:
            # ни старта, ни создания — судить не о чем, запирать нельзя
            return GateState(
                True, False, "у стадии раскладки нет отметок времени",
                warn="Не удалось понять, посчитана ли раскладка. Схема "
                     "откроется как есть.")
        return GateState(False, True, "идёт раскладка схемы")

    if st == "failed":
        return GateState(
            True, False, "раскладка не удалась",
            warn="Раскладка не удалась: " + (row.get("error_message") or
                                             "причина неизвестна") +
                 ".\n\nСхема откроется без неё — расположение элементов "
                 "останется прежним.")

    # completed / skipped: считать больше нечего. SKIPPED — штатный исход
    # гонки (истина сменилась или холст правился руками), и он тоже значит
    # «ждать нечего»: если поставлена новая задача, она и будет последней.
    return GateState(True, False, f"раскладка: {st}")
