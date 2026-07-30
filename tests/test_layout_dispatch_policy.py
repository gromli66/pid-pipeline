# -*- coding: utf-8 -*-
"""Правило постановки раскладки: один счёт на одну истину.

Сам диспетчер тянет psycopg2, Celery и файловую систему — в обычном pytest он
не импортируется. Поэтому решение вынесено в чистый `layout_policy`, и
проверяется здесь именно оно: гонки, ради которых §3.2 плана и написан.
"""
from __future__ import annotations

from app.services.layout_policy import (
    ALREADY_FRESH, ALREADY_RUNNING, DISPATCH, plan_dispatch,
)

SHA_A = "aaaaaaaaaaaaaaaa"
SHA_B = "bbbbbbbbbbbbbbbb"


def test_nothing_yet_dispatches():
    assert plan_dispatch(SHA_A) == (DISPATCH, [])


def test_fresh_canvas_stops_dispatch():
    """Повторное подтверждение контуров не должно плодить задачи по 2-5 минут."""
    canvas = {"layout_applied": True, "stale": False}
    assert plan_dispatch(SHA_A, canvas) == (ALREADY_FRESH, [])


def test_stale_canvas_dispatches():
    canvas = {"layout_applied": True, "stale": True}
    action, revoke = plan_dispatch(SHA_A, canvas)
    assert action == DISPATCH and revoke == []


def test_fallback_canvas_does_not_count_as_done():
    """Клиентский pretransform-фолбэк — не раскладка, считать всё равно надо."""
    canvas = {"layout_applied": False, "stale": False}
    action, _ = plan_dispatch(SHA_A, canvas)
    assert action == DISPATCH


def test_running_on_same_truth_is_idempotent():
    """Оба contours-эндпоинта зовутся повторно из любого статуса."""
    action, revoke = plan_dispatch(SHA_A, None, [("stage-1", SHA_A)])
    assert action == ALREADY_RUNNING and revoke == []


def test_running_on_other_truth_is_revoked():
    """Иначе старая задача финиширует позже и перезапишет результат новой."""
    action, revoke = plan_dispatch(SHA_A, None, [("stage-1", SHA_B)])
    assert action == DISPATCH and revoke == ["stage-1"]


def test_only_foreign_truths_are_revoked():
    action, revoke = plan_dispatch(
        SHA_A, None, [("stage-1", SHA_B), ("stage-2", SHA_A)])
    assert action == ALREADY_RUNNING and revoke == []


def test_several_stale_tasks_all_revoked():
    action, revoke = plan_dispatch(
        SHA_A, None, [("stage-1", SHA_B), ("stage-2", None)])
    assert action == DISPATCH
    assert sorted(revoke) == ["stage-1", "stage-2"]


def test_return_recomputes_even_when_nothing_changed():
    """Возврат по бусине: холст пересчитывается всегда (§3.2).

    Не ради чистоты правила: готовый холст несёт правки оператора, сделанные
    ДО возврата, а они возврат пережить не должны.
    """
    canvas = {"layout_applied": True, "stale": False}
    action, revoke = plan_dispatch(SHA_A, canvas, force=True)
    assert action == DISPATCH and revoke == []


def test_return_still_waits_for_task_on_same_truth():
    """Даже на возврате не плодим вторую задачу поверх бегущей: её результат
    и есть то, что нужно."""
    canvas = {"layout_applied": True, "stale": False}
    action, _ = plan_dispatch(SHA_A, canvas, [("stage-1", SHA_A)], force=True)
    assert action == ALREADY_RUNNING


def test_return_revokes_task_on_other_truth():
    action, revoke = plan_dispatch(SHA_A, None, [("stage-1", SHA_B)], force=True)
    assert action == DISPATCH and revoke == ["stage-1"]


def test_stale_canvas_and_running_same_truth_waits():
    """Холст ещё старый, но задача на новую истину уже бежит — ждём её."""
    canvas = {"layout_applied": True, "stale": True}
    action, revoke = plan_dispatch(SHA_A, canvas, [("stage-1", SHA_A)])
    assert action == ALREADY_RUNNING and revoke == []
