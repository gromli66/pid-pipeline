"""Точка входа для сборки десктоп-клиента P&ID в .exe / бинарник (PyInstaller).

Обычный запуск для разработки — `python -m ui.main`.
Эта обёртка нужна только для собранного клиента: ставит рабочую папку,
читает настройки из client.cfg рядом с программой.
"""
import os
import sys


def _data_dir():
    # где лежат упакованные данные (configs): onefile -> _MEIPASS, onedir -> рядом с exe
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir():
    # где лежит сам файл (для client.cfg, который правит пользователь)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _load_client_cfg():
    """Прочитать client.cfg (строки KEY=VALUE) в окружение.

    Уже заданные переменные окружения имеют приоритет (setdefault).
    """
    cfg = os.path.join(_exe_dir(), "client.cfg")
    if not os.path.exists(cfg):
        return
    for raw in open(cfg, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


# относительные пути вроде "configs/projects/..." ищем рядом с упакованными данными
os.chdir(_data_dir())

_load_client_cfg()
# адрес сервера: env -> client.cfg -> заглушка
os.environ.setdefault("PID_API_URL", "http://REPLACE_WITH_SERVER_IP:8000")
# PID_GL_BACKEND НЕ форсируем: по умолчанию gles (ANGLE, без мигания CVAT),
# при необходимости переопределяется в client.cfg (например, software).
# Софт-рендер Qt Quick: без него WebEngine (вкладка CVAT) не поднимается
# на Astra/VM без GPU; env/client.cfg имеют приоритет (setdefault).
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from ui.main import main  # noqa: E402

if __name__ == "__main__":
    main()
