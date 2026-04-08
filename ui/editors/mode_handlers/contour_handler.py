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
            editor.update_status("Контуры только для equipment-узлов")
            return True

        ann_idx = node.get("ann_idx")
        if ann_idx is None:
            editor.update_status(
                f"{clicked}: нет ann_idx (ручной или unknown узел)"
            )
            return True

        cn = editor._ann_to_contour.get(ann_idx)
        if cn is None:
            editor.update_status(
                f"{clicked}: SAM2 контур не найден (skip_class?)"
            )
            return True

        from ui.editors.commands.contour_commands import ToggleContourCommand

        if clicked in editor._applied_nodes:
            cmd = ToggleContourCommand(editor, clicked, apply=False)
            editor.undo_mgr.execute(cmd)
            editor.update_status(f"Контур снят: {clicked}")
        else:
            conf = cn.get("confidence", 0)
            cmd = ToggleContourCommand(editor, clicked, apply=True)
            editor.undo_mgr.execute(cmd)
            editor.update_status(
                f"Контур применён: {clicked} (conf={conf:.2f}, "
                f"{cn.get('n_points', '?')} pts)"
            )

        # Notify stats callback
        editor.update_statistics()

        return True
