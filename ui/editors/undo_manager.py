"""
Undo Manager — Command pattern для undo/redo в редакторе графа.

Содержит:
- Command (ABC) — базовый класс команды
- SnapshotCommand — snapshot-based команда для сложных операций
- UndoManager — стек undo/redo
"""

from abc import ABC, abstractmethod
from collections import deque
from typing import Callable, Optional

from ui.editors.graph_data import GraphDataModel


class Command(ABC):
    """Базовый класс команды undo/redo.

    Все мутации графа оборачиваются в команды.
    По умолчанию redo() = execute().
    """

    @abstractmethod
    def execute(self) -> None:
        """Выполнить действие (первый вызов)."""
        ...

    @abstractmethod
    def undo(self) -> None:
        """Отменить действие."""
        ...

    def redo(self) -> None:
        """Повторить действие. По умолчанию = execute().

        Override для команд, создающих ID (AddConnectorOnEdge),
        чтобы переиспользовать сохранённые ID.
        """
        self.execute()

    @property
    def description(self) -> str:
        """Описание для статусбара."""
        return self.__class__.__name__


class SnapshotCommand(Command):
    """Snapshot-based команда для сложных операций.

    Используется для batch drag, auto-fix — операций, где
    инкрементальный undo слишком сложен.

    Паттерн использования:
        cmd = SnapshotCommand(model, redraw_callback)
        cmd.execute()          # сохраняет _before
        ... выполнить операцию ...
        cmd.finalize()         # сохраняет _after
        undo_mgr.push_executed(cmd)
    """

    def __init__(self, model: GraphDataModel, redraw_callback: Callable):
        self._model = model
        self._redraw = redraw_callback
        self._before: Optional[tuple] = None
        self._after: Optional[tuple] = None
        self._description: str = "Snapshot"

    def execute(self):
        """Сохранить состояние ДО операции."""
        self._before = self._model.snapshot()

    def finalize(self):
        """Сохранить состояние ПОСЛЕ операции. Вызвать после завершения."""
        self._after = self._model.snapshot()

    def undo(self):
        """Восстановить состояние ДО операции."""
        if self._before is not None:
            self._model.restore(self._before)
            self._redraw()

    def redo(self):
        """Восстановить состояние ПОСЛЕ операции."""
        if self._after is not None:
            self._model.restore(self._after)
            self._redraw()

    @property
    def description(self) -> str:
        return self._description

    @description.setter
    def description(self, value: str):
        self._description = value


class UndoManager:
    """Менеджер undo/redo стека.

    Поддерживает два паттерна:
    1. execute(cmd) — выполнить + push (для instant-операций)
    2. push_executed(cmd) — push уже выполненной команды (для drag и т.п.)
    """

    def __init__(self, max_steps: int = 100):
        self.undo_stack: deque[Command] = deque(maxlen=max_steps)
        self.redo_stack: deque[Command] = deque(maxlen=max_steps)
        # Монотонный счётчик мутаций (execute / push_executed / undo / redo).
        # В отличие от stack_depth не «застывает» при переполнении deque
        # и не совпадает ложно после undo + повторных правок.
        self._revision: int = 0

    def execute(self, command: Command):
        """Выполнить команду и добавить в undo-стек."""
        command.execute()
        self.undo_stack.append(command)
        self.redo_stack.clear()
        self._revision += 1

    def push_executed(self, command: Command):
        """Добавить уже выполненную команду (для drag, waypoint move)."""
        self.undo_stack.append(command)
        self.redo_stack.clear()
        self._revision += 1

    def undo(self) -> Optional[str]:
        """Отменить последнее действие.

        Returns:
            Описание отменённой команды или None.
        """
        if not self.undo_stack:
            return None
        cmd = self.undo_stack.pop()
        cmd.undo()
        self.redo_stack.append(cmd)
        self._revision += 1
        return cmd.description

    def redo(self) -> Optional[str]:
        """Повторить отменённое действие.

        Returns:
            Описание повторённой команды или None.
        """
        if not self.redo_stack:
            return None
        cmd = self.redo_stack.pop()
        cmd.redo()
        self.undo_stack.append(cmd)
        self._revision += 1
        return cmd.description

    @property
    def can_undo(self) -> bool:
        return len(self.undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self.redo_stack) > 0

    @property
    def stack_depth(self) -> int:
        """Глубина undo-стека."""
        return len(self.undo_stack)

    @property
    def revision(self) -> int:
        """Монотонный счётчик мутаций. Для has_unsaved_changes."""
        return self._revision

    def clear(self):
        """Очистить оба стека."""
        self.undo_stack.clear()
        self.redo_stack.clear()
