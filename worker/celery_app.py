"""
Celery Application Configuration.
"""

import logging
import os
import sys

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging, worker_process_init, task_prerun

from app.core.logging import get_logger, setup_logging
from app.core import obs

# Получаем настройки из переменных окружения
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6380/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6380/0")

# Создаём Celery приложение
celery_app = Celery(
    "pid_pipeline",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=[
        "worker.tasks.detection",
        "worker.tasks.direction",
        "worker.tasks.segmentation",
        "worker.tasks.skeleton",
        "worker.tasks.junction",
        "worker.tasks.graph",
        "worker.tasks.ocr",
        "worker.tasks.contours",
        "worker.tasks.layout",
    ]
)

# Конфигурация
celery_app.conf.update(
    # Сериализация
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Временная зона
    timezone="UTC",
    enable_utc=True,

    # Таймауты
    task_time_limit=3600,  # 1 час максимум на задачу
    task_soft_time_limit=3300,  # Мягкий лимит 55 минут

    # Окно невидимости брокера (пункт 1.2 · Б10). До его истечения Redis не
    # выдаёт сообщение второй раз; при task_acks_late подтверждение приходит
    # только по завершении задачи. Значение ОБЯЗАНО быть больше самого длинного
    # time_limit (детекция — 5400, detection.py:56), иначе брокер переоткрывает
    # длинную задачу и на CPU-сервере считаются две сразу. Дефолт kombu — 3600,
    # то есть меньше детекции в 1.5 раза и ровно на границе у сегментации и OCR.
    # Стенд: tools/redelivery_bench.py, инвариант заперт тестом.
    broker_transport_options={"visibility_timeout": 7200},

    # Retry
    task_acks_late=True,  # Подтверждение после выполнения
    task_reject_on_worker_lost=True,

    # Очереди
    task_default_queue="default",
    task_queues={
        "default": {},
        "gpu": {},   # GPU detection/segmentation/junction
        "ocr": {},   # OCR worker (separate container)
        "sam2": {},  # SAM2 contour extraction (same worker as gpu)
    },

    # Worker
    worker_prefetch_multiplier=1,  # Для GPU задач лучше 1
    worker_concurrency=2,
)

# Роутинг задач по очередям
celery_app.conf.task_routes = {
    "worker.tasks.detection.*": {"queue": "gpu"},
    "worker.tasks.direction.*": {"queue": "gpu"},
    "worker.tasks.segmentation.*": {"queue": "gpu"},
    "worker.tasks.skeleton.*": {"queue": "default"},
    "worker.tasks.junction.*": {"queue": "gpu"},
    "worker.tasks.graph.*": {"queue": "default"},
    "worker.tasks.ocr.*": {"queue": "ocr"},
    "worker.tasks.contours.*": {"queue": "sam2"},
    "worker.tasks.layout.*": {"queue": "default"},
}


class _StdoutToLogger:
    """Мост stdout→logging: строки print() уходят в лог (тегируются ContextFilter).

    fileno/isatty делегируются исходному потоку, чтобы не ломать библиотеки,
    которым нужен реальный дескриптор (subprocess, C-расширения).
    """

    def __init__(self, logger: logging.Logger, original, level: int = logging.INFO):
        self._logger = logger
        self._original = original
        self._level = level
        self._buf = ""

    def write(self, msg: str) -> int:
        self._buf += msg
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line)
        return len(msg)

    def flush(self) -> None:
        if self._buf.strip():
            self._logger.log(self._level, self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._original.fileno()

    def __getattr__(self, name):
        return getattr(self._original, name)


@celery_setup_logging.connect
def _use_project_logging(**_kwargs) -> None:
    """Забрать настройку логов у Celery целиком.

    Без этого Celery хайджекает root-логгер и наш формат/ContextFilter в задачах
    теряется (видно по смоуку: логи задач в celery-формате, без блока uid=...).
    """
    setup_logging()


@worker_process_init.connect
def _init_worker_logging(**_kwargs) -> None:
    """После fork: наш формат/фильтр логов + мост stdout→logging в каждом воркере.

    Порядок важен: setup_logging() сначала (хендлер захватывает реальный stdout),
    только потом подменяем sys.stdout — иначе цикл лог→stdout→лог.
    """
    setup_logging()
    sys.stdout = _StdoutToLogger(get_logger("worker.stdout"), sys.stdout)


@task_prerun.connect
def _reset_obs_context(**_kwargs) -> None:
    """Обнулить корреляционный контекст перед каждой задачей.

    ``contextvars`` в prefork-воркере не сбрасывается между задачами → без этого
    неинструментированная стадия наследует uid/phase/task предыдущей (смоук Волны 4:
    skeleton логировался с контекстом detection). Инструментированные задачи затем
    зовут ``obs.bind`` поверх чистого контекста — поведение то же.
    """
    obs.reset()
