"""
Contour Commands -- undo/redo for SAM2 contour toggle.
"""

from ui.editors.undo_manager import Command


class ToggleContourCommand(Command):
    """Toggle SAM2 contour on/off for one equipment node.

    execute(): apply or remove contour (depending on self.apply flag)
    undo(): reverse action
    """

    def __init__(self, editor, node_id: str, apply: bool):
        self._editor = editor
        self._node_id = node_id
        self._apply = apply

    def execute(self):
        if self._apply:
            self._editor.apply_contour(self._node_id)
        else:
            self._editor.remove_contour(self._node_id)

    def undo(self):
        if self._apply:
            self._editor.remove_contour(self._node_id)
        else:
            self._editor.apply_contour(self._node_id)

    @property
    def description(self) -> str:
        action = "Apply" if self._apply else "Remove"
        return f"{action} contour: {self._node_id}"
