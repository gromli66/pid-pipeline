"""
Polygon ModeHandler -- single handler for polygon editing and drawing.

State machine:
  IDLE -> click equipment with polygon    -> EDIT (vertex handles)
  IDLE -> click equipment without polygon -> DRAW (click-by-click)
  EDIT -> Delete/Backspace                -> DRAW (redraw same node)
  DRAW -> close polygon                   -> EDIT (fine-tune)
  EDIT/DRAW -> Escape / click empty       -> IDLE
  EDIT -> click other equipment           -> commit + EDIT/DRAW on new node
"""

import logging

from ui.editors.mode_handlers.base_handler import ModeHandler

logger = logging.getLogger(__name__)


class EditPolygonHandler(ModeHandler):
    """Unified handler for edit_polygon mode.

    Delegates most logic to ContourEditor methods and PolygonVertexOverlay.
    The handler's job is routing events to the right action based on phase
    and hit testing priority.
    """

    def on_enter(self, editor):
        """Mode activated -- start in idle phase (waiting for node click)."""
        # If already editing, keep it
        if editor._editing_node and editor._polygon_overlay:
            return
        editor.update_status(
            "Режим редактирования — Ctrl+Click на equipment-узле"
        )

    def on_exit(self, editor):
        """Mode deactivated -- commit and clean up."""
        editor.exit_polygon_editing()

    def on_press(self, editor, x: float, y: float, event) -> bool:
        """Ctrl+Click routing.

        Priority in EDIT phase:
          1. overlay.find_vertex_at  -> start drag (handled by _start_ctrl_drag)
          2. overlay.find_edge_at    -> add vertex
          3. find_node_at            -> switch node or ignore
          4. empty space             -> commit + idle

        Priority in DRAW phase:
          1. is_near_first_point     -> close polygon
          2. else                    -> add point

        Priority in IDLE phase:
          1. find_node_at equipment  -> enter edit/draw
          2. else                    -> status message
        """
        overlay = editor._polygon_overlay
        editing_node = editor._editing_node

        # ── IDLE phase ──
        if not overlay or not editing_node:
            return self._handle_idle_press(editor, x, y)

        phase = overlay.phase

        # ── EDIT phase ──
        if phase == "edit":
            return self._handle_edit_press(editor, x, y)

        # ── DRAW phase ──
        if phase == "draw":
            return self._handle_draw_press(editor, x, y)

        return True

    def _handle_idle_press(self, editor, x, y) -> bool:
        """IDLE: click on equipment -> enter edit/draw."""
        clicked = editor.find_node_at(x, y)
        if not clicked:
            editor.update_status(
                "Ctrl+Click на equipment-узле для редактирования"
            )
            return True

        node = editor.nodes.get(clicked)
        if not node:
            return True

        if node.get("type") != "equipment":
            editor.update_status("Редактирование полигонов — только equipment")
            return True

        editor.enter_polygon_editing(clicked)
        return True

    def _handle_edit_press(self, editor, x, y) -> bool:
        """EDIT: vertex hit -> handled by drag system, edge hit -> add vertex,
        other node -> switch, empty -> commit."""
        overlay = editor._polygon_overlay

        # 1. Vertex -- handled by _start_ctrl_drag override in ContourEditor
        #    (on_press is called for click-without-drag case only)
        vtx = overlay.find_vertex_at(x, y)
        if vtx is not None:
            # This is a click (not drag) on vertex -- no action needed
            # Drag is handled by _start_ctrl_drag/_end_ctrl_drag
            return True

        # 2. Edge -> add vertex
        edge_idx = overlay.find_edge_at(x, y)
        if edge_idx is not None:
            from ui.editors.commands.polygon_commands import AddVertexCommand

            # Project click onto edge to get exact position
            n = overlay.vertex_count
            i = edge_idx
            j = (i + 1) % n
            x1 = overlay._polygon[i * 2]
            y1 = overlay._polygon[i * 2 + 1]
            x2 = overlay._polygon[j * 2]
            y2 = overlay._polygon[j * 2 + 1]
            _, proj_x, proj_y = overlay._point_to_segment_dist(
                x, y, x1, y1, x2, y2,
            )

            insert_idx = edge_idx + 1
            cmd = AddVertexCommand(
                editor, editor._editing_node, insert_idx, proj_x, proj_y,
            )
            editor.undo_mgr.execute(cmd)

            # Refresh overlay
            seg = editor.nodes[editor._editing_node].get("segmentation")
            if seg:
                overlay.refresh_edit(seg)

            editor.update_statistics()
            editor.update_status(
                f"Добавлена вершина #{insert_idx} "
                f"({overlay.vertex_count} всего)"
            )
            return True

        # 3. Another equipment node -> switch
        clicked = editor.find_node_at(x, y)
        if clicked and clicked != editor._editing_node:
            node = editor.nodes.get(clicked)
            if node and node.get("type") == "equipment":
                editor.exit_polygon_editing()
                editor.enter_polygon_editing(clicked)
                return True

        # 4. Empty space or same node or non-equipment -> ignore
        return True

    def _handle_draw_press(self, editor, x, y) -> bool:
        """DRAW: near first point -> close, else -> add point."""
        overlay = editor._polygon_overlay

        # Close polygon if near first point and >= 3 points
        if overlay.is_near_first_point(x, y):
            editor._close_draw_polygon()
            return True

        # Add point
        overlay.add_draw_point(x, y)
        n = overlay.draw_point_count
        editor.update_status(
            f"Точка #{n} — "
            + ("ещё минимум " + str(3 - n) + " точки"
               if n < 3
               else "Click рядом с 1-й или Enter для замыкания")
        )
        return True

    # =================================================================
    # on_move -- hover updates and draw preview
    # =================================================================

    def on_move(self, editor, x: float, y: float, event) -> bool:
        """Update hover state (edit) or preview line (draw)."""
        overlay = editor._polygon_overlay
        if not overlay:
            return False

        if overlay.phase == "edit" and not overlay.is_dragging:
            overlay.update_hover(x, y)

        elif overlay.phase == "draw":
            overlay.update_draw_preview(x, y)

        return False  # don't consume -- let base handle cursor updates

    # =================================================================
    # on_release -- vertex drag finalization handled by _end_ctrl_drag
    # =================================================================

    def on_release(self, editor, x: float, y: float, event) -> bool:
        return False
