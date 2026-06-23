"""
Contour ModeHandler -- Ctrl+Click on equipment centroid to toggle SAM2 polygon.
"""

from ui.editors.mode_handlers.base_handler import ModeHandler


class ApplyContourHandler(ModeHandler):
    """Ctrl+Click on equipment node -> toggle SAM2 contour.

    - Click on equipment with SAM2 contour available -> apply polygon
    - Click on equipment with contour already applied -> remove polygon
    - Click on connector or node without ann_idx -> status message
    """

    def on_press(self, editor, x, y, event):
        clicked = editor.find_node_at(x, y)
        if not clicked:
            return True

        node = editor.nodes.get(clicked)
        if not node:
            return True

        if node.get("type") != "equipment":
            editor.update_status("Форму можно задать только для узлов оборудования")
            return True

        ann_idx = node.get("ann_idx")
        if ann_idx is None:
            editor.update_status(
                "Для этого узла нет автоматически распознанной формы"
            )
            return True

        cn = editor._ann_to_contour.get(ann_idx)
        if cn is None:
            editor.update_status(
                "Для этого узла форма не распознана"
            )
            return True

        from ui.editors.commands.contour_commands import ToggleContourCommand

        if clicked in editor._applied_nodes:
            cmd = ToggleContourCommand(editor, clicked, apply=False)
            editor.undo_mgr.execute(cmd)
            editor.update_status("Форма снята с узла")
        else:
            cmd = ToggleContourCommand(editor, clicked, apply=True)
            editor.undo_mgr.execute(cmd)
            editor.update_status("Форма применена к узлу")

        # Notify stats callback
        editor.update_statistics()

        return True


class SelectRecognizeHandler(ModeHandler):
    """Click on equipment node -> toggle selection for on-demand contour
    recognition (used before contours are computed)."""

    def on_press(self, editor, x, y, event):
        clicked = editor.find_node_at(x, y)
        if not clicked:
            return True
        node = editor.nodes.get(clicked)
        if not node:
            return True
        if node.get("type") != "equipment":
            editor.update_status("Выбирать можно только узлы оборудования")
            return True
        if node.get("ann_idx") is None:
            editor.update_status("У этого узла нет ann_idx — распознать нельзя")
            return True
        editor.toggle_recog_node(clicked)
        return True
