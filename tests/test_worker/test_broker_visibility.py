"""Окно невидимости брокера против таймаутов задач (пункт 1.2 · Б10 дороги).

Дефект, который здесь заперт: `visibility_timeout` не был задан нигде, работал
дефолт kombu 3600 с, а детекция объявлена на `time_limit=5400`. При
`task_acks_late=True` это значит, что брокер ГАРАНТИРОВАННО выдаёт 90-минутную
задачу второй раз на 60-й минуте. Живое воспроизведение — `tools/redelivery_bench.py`
(нужен Redis, поэтому в CI его нет). Здесь — инварианты конфига, они в CI.

Числа абсолютные: тест не имеет права вычислять ожидание из проверяемой
константы, иначе он останется зелёным при любом её значении. Поэтому сверху
стоит 7200, снизу — 5400 самой длинной задачи, и отдельно проверяется, что
сканер лимитов вообще что-то нашёл: сломанный сканер («задач нет») иначе
проходил бы приёмку.
"""
import os

import pytest

from tools.redelivery_bench import task_time_limits
from worker.celery_app import celery_app

VISIBILITY_TIMEOUT = 7200
LONGEST_TASK = "worker.tasks.detection.task_detect_yolo"
LONGEST_LIMIT = 5400
TASKS_WITH_LIMIT = 12


@pytest.fixture(scope="module")
def limits():
    return task_time_limits()


def test_visibility_timeout_is_configured():
    """Опция задана и равна согласованному значению."""
    assert celery_app.conf.broker_transport_options.get(
        "visibility_timeout") == VISIBILITY_TIMEOUT


def test_scanner_sees_every_task(limits):
    """Сторож самого теста: сканер декораторов не потерял задачи."""
    assert len(limits) >= TASKS_WITH_LIMIT
    assert limits[LONGEST_TASK] == LONGEST_LIMIT


def test_longest_task_is_detection(limits):
    """Самый длинный лимит — детекция; вырастет другой — тест это покажет."""
    assert max(limits.values()) == LONGEST_LIMIT
    assert max(limits, key=limits.get) == LONGEST_TASK


def test_visibility_timeout_outlasts_every_task(limits):
    """Главный инвариант: окно длиннее любой задачи, иначе будет дубль."""
    vt = celery_app.conf.broker_transport_options["visibility_timeout"]
    for name, limit in limits.items():
        assert vt > limit, (
            f"{name}: time_limit={limit} не короче окна брокера {vt} — "
            "брокер переоткроет задачу до её завершения")


def test_visibility_timeout_outlasts_global_limit():
    """Глобальный потолок 3600 тоже внутри окна."""
    vt = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert vt > celery_app.conf.task_time_limit
    assert celery_app.conf.task_time_limit == 3600


def test_acks_late_still_on():
    """Без acks_late окно бессмысленно: подтверждение уходит до работы."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_bench_import_does_not_touch_broker_env():
    """Импорт стенда ради хелпера не имеет права уводить процесс в базу 15.

    Возврат ревизора 2026-08-19: стенд пиннил `CELERY_BROKER_URL` на уровне
    модуля, поэтому этот самый импорт перепиливал окружение процесса pytest и
    затирал осознанный `tests/conftest.py:27` — боевой `celery_app` строился на
    тестовой базе. Числа абсолютные: база из conftest — 0, база стенда — 15.
    """
    import tools.redelivery_bench as bench

    assert os.environ["CELERY_BROKER_URL"].endswith("/0")
    assert os.environ.get("CELERY_RESULT_BACKEND", "").endswith("/0") or \
        "CELERY_RESULT_BACKEND" not in os.environ
    assert bench.app is None, "приложение стенда не строится при импорте ради хелперов"
    assert celery_app.conf.broker_url.endswith("/0")
