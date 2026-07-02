"""Версия десктоп-клиента P&ID.

Значение НЕ хранится в исходнике жёстко — оно подставляется при сборке
скриптом ``deploy_ready/build_client.ps1`` через файл ``version.txt``,
который кладётся рядом с собранным бинарником. Так исходный код не меняется
от сборки к сборке, а собранный клиент знает свою дату-версию.

Порядок разрешения версии (первое непустое побеждает):
  1. переменная окружения ``PID_CLIENT_VERSION`` (удобно для отладки);
  2. ``version.txt`` рядом с exe/бинарником (кладёт сборка);
  3. ``version.txt`` внутри упакованных данных (_MEIPASS);
  4. ``version.txt`` рядом с этим модулем (запуск из исходников после сборки);
  5. ``"dev"`` — обычный запуск из исходников.
"""
from __future__ import annotations

import os
import sys


def _candidate_paths() -> list[str]:
    paths: list[str] = []
    if getattr(sys, "frozen", False):
        # собранный клиент: рядом с исполняемым файлом (onedir)
        paths.append(os.path.join(os.path.dirname(sys.executable), "version.txt"))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            paths.append(os.path.join(meipass, "version.txt"))
    # запуск из исходников
    paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt"))
    return paths


def _read_version() -> str:
    env = os.environ.get("PID_CLIENT_VERSION", "").strip()
    if env:
        return env
    for path in _candidate_paths():
        try:
            with open(path, encoding="utf-8-sig") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    return "dev"


__version__ = _read_version()
