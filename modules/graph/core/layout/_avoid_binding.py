# -*- coding: utf-8 -*-
"""_avoid_binding.py — загрузчик vendored-биндинга libavoid (Э7-b).

Бинарь и SWIG-прокси лежат в `vendor/adaptagrams/<платформа>/` (win — в git,
linux — собирается слоем Dockerfile.worker из `vendor/adaptagrams/build/`).
Единственная точка импорта: адаптер `avoid_router` и тесты берут модуль
отсюда, чтобы отсутствие бинаря было ОДНИМ понятным ImportError, а не
рассыпанной по коду охотой за sys.path.

`avoid_available()` — гейт этапа роутинга в `layout()`: без биндинга
раскладка молча работает как раньше (роутинг — надстройка, не зависимость).
"""
from __future__ import annotations

import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[4] / "vendor" / "adaptagrams"

_mod = None
_err: str | None = None


def _platform_dir() -> Path:
    return _VENDOR / ("win" if sys.platform == "win32" else "linux")


def load():
    """Импортировать vendored `adaptagrams`. -> модуль | ImportError.

    Отказ кешируется: раскладка зовёт `avoid_available()` на каждый прогон,
    и повторные попытки импорта сломанного бинаря бессмысленны.
    """
    global _mod, _err
    if _mod is not None:
        return _mod
    if _err is not None:
        raise ImportError(_err)
    d = _platform_dir()
    if not (d / "adaptagrams.py").exists():
        _err = (
            f"биндинг libavoid недоступен: нет {d / 'adaptagrams.py'}. "
            "Windows: бинарь в git (vendor/adaptagrams/win, CPython 3.11 x64); "
            "Linux: собрать vendor/adaptagrams/build/build_linux.sh (слой "
            "Dockerfile.worker). Подробности — vendor/adaptagrams/README.md.")
        raise ImportError(_err)
    sys.path.insert(0, str(d))
    try:
        import adaptagrams  # noqa: F401 — vendored SWIG-прокси
    except Exception as exc:
        _err = (f"биндинг libavoid не загрузился из {d}: {exc!r}. "
                "Бинарь собран под CPython 3.11 x64 — сверить интерпретатор; "
                "пересборка — vendor/adaptagrams/README.md.")
        raise ImportError(_err) from exc
    finally:
        try:
            sys.path.remove(str(d))
        except ValueError:
            pass
    _mod = adaptagrams
    return _mod


def avoid_available() -> bool:
    """Есть ли рабочий биндинг (гейт этапа роутинга)."""
    try:
        load()
        return True
    except ImportError:
        return False
