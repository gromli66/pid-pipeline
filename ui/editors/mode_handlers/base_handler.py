"""
ModeHandler ABC — базовый класс обработчика режима редактирования.

Каждый режим (add_edge, delete_edge, drag_node, ...) реализует свой handler.
Handler делегирует вызовы методам editor-а.
"""

from abc import ABC, abstractmethod


class ModeHandler(ABC):
    """Обработчик режима редактирования.

    Все координаты (x, y) — в пространстве сцены (scene coords).
    editor — экземпляр BaseGraphEditor (или потомка).
    """

    @abstractmethod
    def on_press(self, editor, x: float, y: float, event) -> bool:
        """Ctrl+Click. Returns True если событие обработано."""
        ...

    def on_move(self, editor, x: float, y: float, event) -> bool:
        """Mouse move. Returns True если событие обработано."""
        return False

    def on_release(self, editor, x: float, y: float, event) -> bool:
        """Mouse release. Returns True если событие обработано."""
        return False

    def on_enter(self, editor):
        """Вызывается при входе в режим (set_mode)."""
        pass

    def on_exit(self, editor):
        """Вызывается при выходе из режима (set_mode другого)."""
        pass
