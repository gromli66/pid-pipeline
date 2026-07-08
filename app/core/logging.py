"""
Logging configuration - централизованное логирование.

Использование:
    from app.core.logging import get_logger

    logger = get_logger(__name__)
    logger.info("Сообщение")
    logger.error("Ошибка", exc_info=True)
"""

import logging
import os
import sys
from typing import Optional


# Формат логов: стабильные корреляционные поля (заполняются ContextFilter).
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "uid=%(uid)s phase=%(phase)s step=%(step)s attempt=%(attempt)s task=%(task_id)s "
    "dur=%(duration_ms)s | "
    "%(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Корреляционные поля, инъектируемые в каждую запись (из obs.bind / step()).
_CTX_FIELDS = ("uid", "phase", "step", "attempt", "task_id")

# Уровни: LOG_LEVEL — наши логгеры; LOG_LEVEL_LIBS — сторонние;
# LOG_OVERRIDES — точечно ("httpx:ERROR,sqlalchemy:INFO").
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")
LOG_LEVEL_LIBS = os.getenv("LOG_LEVEL_LIBS", "WARNING")


def _parse_overrides(raw: str) -> dict:
    """'httpx:ERROR,sqlalchemy:INFO' -> {'httpx': 'ERROR', 'sqlalchemy': 'INFO'}."""
    out: dict = {}
    for pair in raw.split(","):
        name, sep, level = pair.partition(":")
        name, level = name.strip(), level.strip().upper()
        if sep and name and level:
            out[name] = level
    return out


LOG_OVERRIDES = _parse_overrides(os.getenv("LOG_OVERRIDES", ""))

# Цвета для консоли (ANSI)
COLORS = {
    "DEBUG": "\033[36m",     # Cyan
    "INFO": "\033[32m",      # Green
    "WARNING": "\033[33m",   # Yellow
    "ERROR": "\033[31m",     # Red
    "CRITICAL": "\033[35m",  # Magenta
    "RESET": "\033[0m",      # Reset
}


class ColoredFormatter(logging.Formatter):
    """Форматтер с цветным выводом для консоли."""

    def __init__(self, fmt: str, datefmt: str, use_colors: bool = True):
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        # Сохраняем оригинальный levelname
        orig_levelname = record.levelname

        if self.use_colors and sys.stdout.isatty():
            color = COLORS.get(record.levelname, COLORS["RESET"])
            record.levelname = f"{color}{record.levelname}{COLORS['RESET']}"

        result = super().format(record)

        # Восстанавливаем
        record.levelname = orig_levelname

        return result


class ContextFilter(logging.Filter):
    """Инъекция корреляционного контекста (obs.bind / step) в каждую запись.

    uid/phase/attempt/task_id — из obs._ctx; step/duration_ms — из extra под-шага
    (step() пишет duration_ms в extra на step.end); чего нет — подставляется "-".
    """

    def __init__(self) -> None:
        super().__init__()
        from app.core.obs import _ctx  # локальный импорт — без связи на импорте модуля
        self._ctx = _ctx

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = self._ctx.get()
        for field in _CTX_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, ctx.get(field, "-"))
        # duration_ms — не из контекста, а из extra step.end; иначе LOG_FORMAT
        # упал бы на записях без него.
        if not hasattr(record, "duration_ms"):
            record.duration_ms = "-"
        return True


def setup_logging(
    level: Optional[str] = None,
    log_format: Optional[str] = None,
    date_format: Optional[str] = None,
) -> None:
    """
    Настроить логирование для всего приложения.

    Args:
        level: Уровень логирования (DEBUG, INFO, WARNING, ERROR); по умолчанию LOG_LEVEL
        log_format: Формат сообщений (опционально)
        date_format: Формат даты (опционально)

    Вызывать один раз при старте приложения:
        from app.core.logging import setup_logging
        setup_logging()
    """
    level = level or LOG_LEVEL
    log_format = log_format or LOG_FORMAT
    date_format = date_format or DATE_FORMAT

    # Преобразуем строку в уровень
    numeric_level = getattr(logging, level.upper(), logging.DEBUG)

    # Корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Удаляем существующие handlers (избегаем дубликатов)
    root_logger.handlers.clear()

    # Console handler с цветами + инъекция корреляционного контекста
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(ColoredFormatter(log_format, date_format))
    console_handler.addFilter(ContextFilter())
    root_logger.addHandler(console_handler)

    # Сторонние библиотеки: единый уровень LOG_LEVEL_LIBS + точечные LOG_OVERRIDES.
    libs_level = getattr(logging, LOG_LEVEL_LIBS.upper(), logging.WARNING)
    for lib in ("httpx", "httpcore", "uvicorn.access", "PIL"):
        logging.getLogger(lib).setLevel(libs_level)
    for name, level_name in LOG_OVERRIDES.items():
        logging.getLogger(name).setLevel(getattr(logging, level_name, logging.WARNING))

    # Логируем что настройка завершена
    logger = logging.getLogger("app.core.logging")
    logger.info(f"Logging configured: level={level}")


def get_logger(name: str) -> logging.Logger:
    """
    Получить логгер для модуля.

    Args:
        name: Имя модуля (обычно __name__)

    Returns:
        logging.Logger

    Использование:
        from app.core.logging import get_logger
        logger = get_logger(__name__)

        logger.debug("Детальная информация")
        logger.info("Важное событие")
        logger.warning("Предупреждение")
        logger.error("Ошибка")
        logger.exception("Ошибка с traceback")  # В except блоке
    """
    return logging.getLogger(name)


# Shortcuts для быстрого доступа
def debug(msg: str, *args, **kwargs) -> None:
    """Быстрый debug лог."""
    logging.getLogger("app").debug(msg, *args, **kwargs)


def info(msg: str, *args, **kwargs) -> None:
    """Быстрый info лог."""
    logging.getLogger("app").info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs) -> None:
    """Быстрый warning лог."""
    logging.getLogger("app").warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs) -> None:
    """Быстрый error лог."""
    logging.getLogger("app").error(msg, *args, **kwargs)
