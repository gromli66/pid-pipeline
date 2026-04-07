"""
Celery Application Configuration.
"""

import os
from celery import Celery

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
        "worker.tasks.segmentation",
        "worker.tasks.skeleton",
        "worker.tasks.junction",
        "worker.tasks.graph",
        "worker.tasks.ocr",
        "worker.tasks.contours",
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
    "worker.tasks.segmentation.*": {"queue": "gpu"},
    "worker.tasks.skeleton.*": {"queue": "default"},
    "worker.tasks.junction.*": {"queue": "gpu"},
    "worker.tasks.graph.*": {"queue": "default"},
    "worker.tasks.ocr.*": {"queue": "ocr"},
    "worker.tasks.contours.*": {"queue": "sam2"},
}