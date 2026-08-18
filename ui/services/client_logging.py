"""Приёмник логов десктоп-клиента: файл с ротацией + перехват падений.

Инструментирование в клиенте уже есть (22 файла из 68 в ``ui/`` зовут logging),
не было приёмника: ``ui/main.py`` настраивал ``basicConfig(stream=sys.stdout)``,
а в собранном ``.exe`` ``console=False``
(``deploy_ready/pid_client_windows.spec:82``) — stdout уходит в никуда.
Необработанное исключение в слоте оставляло замерший UI без единого следа.

Три части, всё остальное — из общего кода сервера:

1. ``setup_client_logging()`` — консоль (как раньше) плюс
   ``RotatingFileHandler``. Формат и корреляционные поля берутся из
   ``app.core.logging`` (Д2 дороги: «встраиваться в obs + ContextFilter, не
   заводить своё»), поэтому строка клиента читается так же, как серверная.
2. ``install_excepthook()`` — необработанное исключение (в том числе в слоте
   Qt: PySide6 отдаёт такие в ``sys.excepthook``, проверено 6.11.1) пишется
   в лог с трассировкой, после чего управление уходит прежнему хуку.
3. ``bind_uid()`` — uid открытой диаграммы в корреляционный контекст, чтобы
   лог клиента сшивался с серверным по ``uid=``.

⚠ Файл открывается с ``encoding="utf-8", errors="replace"``: Windows-консоль
обычно cp1251, эмодзи в сообщениях (💾 ✅ 🔄) роняли ``StreamHandler``
``UnicodeEncodeError``'ом и строка терялась (``ui/main.py:12-18`` чинит это для
консоли). Без явной кодировки тот же дефект переехал бы в файл.

Единственная точка, где ``ui/`` зависит от ``app/`` — шов для этапа 13.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from app.core import obs
from app.core.logging import DATE_FORMAT, LOG_FORMAT, ContextFilter, setup_logging

LOG_FILE_NAME = "client.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
DEFAULT_LEVEL = "INFO"


def log_dir() -> Path:
    """Каталог лога клиента.

    Пишем в пользовательский каталог, а не рядом с программой и не в текущую
    папку: ``client_main.py:42`` делает ``chdir`` в распакованный ``_MEIPASS``
    (временный), а сам ``.exe`` может лежать в ``Program Files`` без прав на
    запись. Переопределяется переменной ``PID_LOG_DIR``.
    """
    env = os.getenv("PID_LOG_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "PID-Client" / "logs"
    return Path(os.path.expanduser("~")) / ".local" / "state" / "pid-client" / "logs"


def setup_client_logging(level: Optional[str] = None) -> Optional[Path]:
    """Настроить логи клиента: консоль + файл с ротацией. Вернуть путь файла.

    Каталог недоступен (нет прав, сетевой диск отвалился) — клиент обязан
    стартовать: пишем предупреждение в консоль и возвращаем ``None``.
    """
    setup_logging(level=level or os.getenv("PID_LOG_LEVEL", DEFAULT_LEVEL))
    logger = logging.getLogger(__name__)

    path = log_dir() / LOG_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        logger.warning(
            "Лог в файл недоступен (%s): %s — остаётся только консоль", path, exc
        )
        return None

    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(ContextFilter())
    logging.getLogger().addHandler(handler)
    # Д3: подсистема сообщает и о нормальной работе, не только об отказе —
    # первая же строка файла говорит оператору, куда смотреть дальше.
    logger.info(
        "Лог клиента: %s (ротация %d × %d Б)", path, BACKUP_COUNT, MAX_BYTES
    )
    return path


def install_excepthook() -> None:
    """Писать необработанные исключения в лог, затем отдавать прежнему хуку.

    Повторная установка — no-op: ``main()`` может быть вызван дважды в одном
    процессе (тесты), а цепочка из копий одного хука дублирует записи.
    """
    previous = sys.excepthook
    if getattr(previous, "_pid_client_hook", False):
        return

    def _hook(
        exc_type: Type[BaseException],
        exc: BaseException,
        tb: Optional[TracebackType],
    ) -> None:
        logging.getLogger("ui").critical(
            "Необработанное исключение", exc_info=(exc_type, exc, tb)
        )
        previous(exc_type, exc, tb)

    _hook._pid_client_hook = True  # type: ignore[attr-defined]
    sys.excepthook = _hook


def bind_uid(uid: Optional[str]) -> None:
    """Проставить uid открытой диаграммы в корреляционный контекст.

    Дальше его подставляет в каждую строку ``ContextFilter`` — тот же механизм,
    что на сервере, поэтому лог клиента и лог воркера сшиваются по ``uid=``.
    Контекст живёт в ``contextvars``: в фоновых потоках вкладок (QThread) он
    свой, там останется ``uid=-``.
    """
    obs.bind(uid=uid or "-")
