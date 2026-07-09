"""
Примитив инструментирования под-шагов (Волна 0 — фундамент).

``bind()`` кладёт корреляционный контекст (uid/phase/...) в ``contextvars``;
``step()`` логирует границы под-шага (``start`` / ``end`` + ``duration_ms``) и
оборачивает сбой в типизированную ошибку с проставленным ``step``.
См. код-скетч в ARCH §3. Поведение пайплайна не меняется — это только слой
логов и атрибуции ошибок.
"""

import contextvars
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.errors import ArtifactMissingError, ArtifactWriteError, PipelineError

try:  # celery есть в api/worker-образах; вне их (чистые тесты без celery) — заглушка
    from celery.exceptions import SoftTimeLimitExceeded as _SoftTimeLimitExceeded
except Exception:  # pragma: no cover — среда без celery, ветка passthrough недостижима
    class _SoftTimeLimitExceeded(BaseException):
        """Заглушка на случай отсутствия celery."""

# Корреляционный контекст фазы; инъектируется в лог-строки ContextFilter'ом
# (сам фильтр — отдельный файл этой же волны, app/core/logging.py).
_ctx: contextvars.ContextVar = contextvars.ContextVar("obs_ctx", default={})

# Репортер текущего под-шага (Волна B, current_step). Отдельная переменная,
# НЕ в _ctx: поля _ctx инъектируются ContextFilter'ом в лог-строки, функции там
# не место. Живёт в контексте задачи; reset() снимает.
_step_sink: contextvars.ContextVar = contextvars.ContextVar("obs_step_sink", default=None)


def bind(**kw) -> None:
    """Добавить поля в корреляционный контекст (вызывается в начале фазы)."""
    _ctx.set({**_ctx.get(), **kw})


def reset() -> None:
    """Обнулить корреляционный контекст (в начале каждой задачи).

    В prefork-воркере ``contextvars`` живёт весь процесс и НЕ обнуляется между
    задачами Celery. Без сброса неинструментированная стадия наследует
    ``uid``/``phase``/``task`` предыдущей задачи (смоук Волны 4: skeleton
    логировался как ``phase=detecting`` с task детекции). Вызывается из
    ``task_prerun`` → каждая задача стартует с чистым контекстом (незаполненные
    поля → ``-``, а не чужие значения)."""
    _ctx.set({})
    _step_sink.set(None)


def bind_step_sink(sink) -> None:
    """Привязать репортер текущего под-шага (Волна B, ``current_step``).

    ``step()`` зовёт ``sink(name)`` на старте каждого под-шага; сбой репортера
    глотается (наблюдаемость не роняет пайплайн). Задача привязывает репортер
    после ``start_stage``; ``reset()`` в ``task_prerun`` снимает привязку."""
    _step_sink.set(sink)


@contextmanager
def step(name: str, logger: logging.Logger, **fields) -> Iterator[None]:
    """Обернуть под-шаг: лог ``start``/``end`` + ``duration_ms``; сбой →
    типизированная ошибка с проставленным ``step`` (пробрасывается наверх)."""
    t0 = time.perf_counter()
    logger.info("step.start", extra={"step": name, "event": "start", **fields})
    sink = _step_sink.get()
    if sink is not None:
        try:
            sink(name)
        except Exception:  # репортер не должен ронять пайплайн
            logger.debug("step sink failed", exc_info=True)
    try:
        yield
    except PipelineError as exc:
        exc.step = exc.step or name
        logger.error(
            f"step.error code={exc.code}",
            extra={"step": name, "event": "error", "code": exc.code},
            exc_info=True,
        )
        raise
    except _SoftTimeLimitExceeded:
        # Таймаут Celery НЕ оборачиваем: задачи ловят его сами (терминальный
        # fail без retry). Оборачивание в PipelineError уводило таймаут в
        # generic-ветку retry — до 3× времени впустую (аудит 2026-07-09, C1).
        logger.error(
            "step.error code=timeout",
            extra={"step": name, "event": "error", "code": "timeout"},
        )
        raise
    except Exception as exc:
        # Неожиданное — типизируем в PipelineError, проставляя step/phase,
        # чтобы failed_step/error_stage заполнялись и для нетипизированных сбоев.
        wrapped = PipelineError(
            str(exc), stage=_ctx.get().get("phase"), step=name, cause=exc
        )
        logger.error(
            f"step.error code={wrapped.code}",
            extra={"step": name, "event": "error", "code": wrapped.code},
            exc_info=True,
        )
        raise wrapped from exc
    else:
        logger.info(
            "step.end",
            extra={
                "step": name,
                "event": "end",
                "duration_ms": round((time.perf_counter() - t0) * 1000),
            },
        )


@contextmanager
def load_artifact(kind: str, path, logger: logging.Logger) -> Iterator[Path]:
    """Загрузить входной артефакт (под-шаг ``load_inputs``).
    Нет файла → ``ArtifactMissingError``."""
    with step("load_inputs", logger, artifact=kind, path=str(path)):
        if not Path(path).exists():
            raise ArtifactMissingError(f"{kind} not found: {path}")
        yield Path(path)


def persist_artifact(db, uid: str, kind, path, base, logger: logging.Logger):
    """Сохранить артефакт фазы (под-шаг ``persist_artifacts``).
    Сбой записи → ``ArtifactWriteError``."""
    # Ленивый импорт: не тянем worker в app.core на этапе импорта модуля.
    from worker.utils.db_helpers import upsert_artifact

    with step("persist_artifacts", logger, artifact=kind, path=str(path)):
        try:
            return upsert_artifact(db, uid, kind, path, base)
        except Exception as exc:
            raise ArtifactWriteError(
                f"failed to persist {kind}: {path}", cause=exc
            ) from exc
