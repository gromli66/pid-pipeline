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
