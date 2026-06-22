"""
UISettings — обёртка над QSettings для кроссплатформенного хранения настроек UI.

Реестр Windows / ~/.config Linux.
"""

from PySide6.QtCore import QSettings


class UISettings:
    _instance = None

    @classmethod
    def instance(cls) -> "UISettings":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._qs = QSettings("PID", "P&ID Pipeline")

    # --- Autosave ---
    @property
    def autosave_enabled(self) -> bool:
        return self._qs.value("autosave/enabled", True, type=bool)

    @autosave_enabled.setter
    def autosave_enabled(self, val: bool):
        self._qs.setValue("autosave/enabled", val)

    @property
    def autosave_interval_sec(self) -> int:
        return self._qs.value("autosave/interval_sec", 120, type=int)

    @autosave_interval_sec.setter
    def autosave_interval_sec(self, val: int):
        self._qs.setValue("autosave/interval_sec", val)

    # --- Appearance (оформление, по диаграмме) ---
    def get_appearance(self, uid: str, key: str, default):
        """Прочитать настройку оформления для конкретной диаграммы.

        Тип результата приводится к типу default (float/int/str/bool).
        """
        val = self._qs.value(f"appearance/{uid}/{key}", default)
        try:
            if isinstance(default, bool):
                if isinstance(val, str):
                    return val.lower() in ("1", "true", "yes")
                return bool(val)
            if isinstance(default, float):
                return float(val)
            if isinstance(default, int):
                return int(val)
            return val
        except (TypeError, ValueError):
            return default

    def set_appearance(self, uid: str, key: str, value):
        """Сохранить настройку оформления для конкретной диаграммы."""
        self._qs.setValue(f"appearance/{uid}/{key}", value)

    def has_appearance(self, uid: str, key: str) -> bool:
        """True если для диаграммы сохранено пользовательское значение настройки."""
        return self._qs.contains(f"appearance/{uid}/{key}")

    def clear_appearance(self, uid: str):
        """Удалить все пользовательские настройки оформления диаграммы."""
        self._qs.remove(f"appearance/{uid}")
