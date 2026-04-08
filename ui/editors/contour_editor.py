"""
Contour Editor -- apply/remove SAM2 contours on equipment nodes.

Extends SimpleGraphEditor with:
- Contour data loading (contours_auto.json)
- Apply/remove contour polygon per node
- Centroid recalculation from polygon
- Edge connection point recalculation
- Confidence-based color overlay
- Polygon vertex editing (drag, add, delete vertices)
- Polygon drawing from scratch (click-by-click)
"""

import json
import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush

from ui.editors.simple_graph_editor import SimpleGraphEditor
from ui.editors.mode_handlers.contour_handler import ApplyContourHandler
from ui.editors.mode_handlers.polygon_handlers import EditPolygonHandler
from ui.editors.polygon_overlay import PolygonVertexOverlay
from ui.editors.graph_geometry import (
    connect_bbox_bbox, connect_bbox_polygon,
    connect_polygon_polygon, connect_point_bbox,
    connect_point_polygon,
)

logger = logging.getLogger(__name__)


class ContourEditor(SimpleGraphEditor):
    """Editor for toggling SAM2 contours on equipment nodes.

    Adds contour mode: Ctrl+Click on equipment centroid to apply/remove
    SAM2 polygon. Recalculates centroid and edge connection points.
    """

    # Contour-specific colors
    COLOR_CONTOUR_APPLIED = QColor(46, 204, 113, 140)    # green -- applied
    COLOR_CONTOUR_REVIEW = QColor(241, 196, 15, 140)     # yellow -- low confidence
    COLOR_CONTOUR_AVAILABLE = QColor(52, 152, 219, 60)   # light blue -- has contour
    COLOR_CONTOUR_NONE = QColor(149, 165, 166, 80)       # gray -- no contour

    def __init__(self):
        super().__init__()

        # Contour data
        self._contour_data: dict = {}
        self._ann_to_contour: dict[int, dict] = {}
        self._applied_nodes: set[str] = set()
        self._original_seg: dict[str, Optional[list]] = {}
        self._original_centroid: dict[str, list] = {}
        self._original_bbox: dict[str, Optional[list]] = {}

        # ann_idx -> node_id reverse index
        self._ann_to_node: dict[int, str] = {}

        # Contour stats
        self._contour_stats: dict = {}

        # Polygon editing state
        self._editing_node: str | None = None
        self._polygon_overlay: PolygonVertexOverlay | None = None

        # Register contour modes
        self.register_mode("apply_contour", ApplyContourHandler())
        self.register_mode("edit_polygon", EditPolygonHandler())

    # =================================================================
    # Loading
    # =================================================================

    def load_contours(self, contours_path: str) -> bool:
        """Load contours_auto.json and build index."""
        try:
            with open(contours_path, "r", encoding="utf-8") as f:
                self._contour_data = json.load(f)

            self._ann_to_contour.clear()
            for cn in self._contour_data.get("nodes", []):
                ann_id = cn.get("ann_id")
                if ann_id is not None:
                    self._ann_to_contour[ann_id] = cn

            # Build ann_idx -> node_id from graph nodes
            self._ann_to_node.clear()
            for node_id, node in self.nodes.items():
                ann_idx = node.get("ann_idx")
                if ann_idx is not None:
                    self._ann_to_node[ann_idx] = node_id

            self._contour_stats = self._contour_data.get("stats", {})

            logger.info(
                "Loaded %d contours, %d graph nodes with ann_idx",
                len(self._ann_to_contour),
                len(self._ann_to_node),
            )
            return True
        except Exception as exc:
            logger.error("Failed to load contours: %s", exc)
            return False

    def load_applied_state(self, validated_path: str) -> bool:
        """Restore applied state from contours_validated.json.

        Marks nodes that have polygon_validated as already applied.
        Does NOT visually apply -- caller must call apply_contour() for each.
        """
        try:
            with open(validated_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            restored = 0
            for cn in data.get("nodes", []):
                if cn.get("polygon_validated"):
                    ann_id = cn.get("ann_id")
                    node_id = self._ann_to_node.get(ann_id)
                    if node_id:
                        # Pre-populate the set so apply_contour knows
                        # NOT to push this into original_seg again
                        self._applied_nodes.add(node_id)
                        restored += 1

            logger.info("Restored %d applied contours from validated", restored)
            return True
        except Exception as exc:
            logger.warning("Failed to load applied state: %s", exc)
            return False

    # =================================================================
    # Apply / Remove
    # =================================================================

    def apply_contour(self, node_id: str) -> bool:
        """Apply SAM2 polygon to graph node. Recalculate centroid, bbox, edges."""
        node = self.nodes.get(node_id)
        if not node:
            return False

        ann_idx = node.get("ann_idx")
        cn = self._ann_to_contour.get(ann_idx)
        if not cn or not cn.get("polygon_auto"):
            return False

        # Save original (only first time)
        if node_id not in self._original_seg:
            self._original_seg[node_id] = (
                node.get("segmentation")[:] if node.get("segmentation") else None
            )
            self._original_centroid[node_id] = node["centroid"][:]
            self._original_bbox[node_id] = (
                node["bbox"][:] if node.get("bbox") else None
            )

        # Apply polygon
        polygon = cn["polygon_auto"]
        node["segmentation"] = polygon
        self._applied_nodes.add(node_id)

        # Recalculate centroid from polygon
        self._recalculate_centroid(node_id, polygon)

        # Recalculate bbox from polygon extent
        self._recalculate_bbox(node_id, polygon)

        # Redraw node (removes old bbox/polygon, draws new)
        self.remove_node_items(node_id)
        self._draw_single_node(node_id)

        # Recalculate edges connected to this node
        self._recalculate_edges_for_node(node_id)

        return True

    def remove_contour(self, node_id: str) -> bool:
        """Remove SAM2 polygon, restore original segmentation, centroid, bbox."""
        node = self.nodes.get(node_id)
        if not node or node_id not in self._applied_nodes:
            return False

        # Restore originals
        node["segmentation"] = self._original_seg.get(node_id)
        node["centroid"] = self._original_centroid.get(
            node_id, node["centroid"]
        )
        node["bbox"] = self._original_bbox.get(node_id, node.get("bbox"))
        self._applied_nodes.discard(node_id)

        # Redraw
        self.remove_node_items(node_id)
        self._draw_single_node(node_id)

        # Recalculate edges
        self._recalculate_edges_for_node(node_id)

        return True

    # =================================================================
    # Recalculation
    # =================================================================

    def _recalculate_centroid(self, node_id: str, polygon: list):
        """Recalculate centroid using Shoelace polygon centroid formula."""
        from modules.graph.core.tracing import _polygon_centroid

        node = self.nodes[node_id]
        xs = polygon[0::2]
        ys = polygon[1::2]
        if len(xs) < 3:
            return

        cx, cy = _polygon_centroid(xs, ys)
        if cx is not None and cy is not None:
            # centroid format: [y, x] (row, col)
            node["centroid"] = [cy, cx]

    def _recalculate_bbox(self, node_id: str, polygon: list):
        """Recalculate bbox as bounding box of polygon vertices.

        bbox format: [x1, y1, x2, y2]
        """
        node = self.nodes[node_id]
        xs = polygon[0::2]
        ys = polygon[1::2]
        if xs and ys:
            node["bbox"] = [
                min(xs), min(ys),
                max(xs), max(ys),
            ]

    def _recalculate_edges_for_node(self, node_id: str):
        """Recalculate connection points for all edges touching node_id.

        Uses graph_geometry.connect_* functions to compute new
        source_point / target_point, then updates the visual.
        """
        connected = self.model.get_connected_edges(node_id)

        for edge_key in connected:
            edge_data = self.model.find_edge_data(edge_key)
            if not edge_data:
                continue

            src_id = edge_data["source"]
            tgt_id = edge_data["target"]
            src_node = self.nodes.get(src_id)
            tgt_node = self.nodes.get(tgt_id)
            if not src_node or not tgt_node:
                continue

            src_seg = src_node.get("segmentation")
            tgt_seg = tgt_node.get("segmentation")
            src_bbox = self._get_node_bbox(src_id)
            tgt_bbox = self._get_node_bbox(tgt_id)

            src_type = src_node.get("type", "connector")
            tgt_type = tgt_node.get("type", "connector")

            src_has_poly = (
                src_type == "equipment"
                and src_seg
                and isinstance(src_seg, list)
                and len(src_seg) >= 6
            )
            tgt_has_poly = (
                tgt_type == "equipment"
                and tgt_seg
                and isinstance(tgt_seg, list)
                and len(tgt_seg) >= 6
            )

            # Connector nodes: use point-based connection
            src_is_connector = src_type == "connector"
            tgt_is_connector = tgt_type == "connector"

            try:
                if src_is_connector and tgt_has_poly:
                    pt = (src_node["centroid"][1], src_node["centroid"][0])
                    p1, p2, _ = connect_point_polygon(pt, tgt_seg)
                elif tgt_is_connector and src_has_poly:
                    pt = (tgt_node["centroid"][1], tgt_node["centroid"][0])
                    p2, p1, _ = connect_point_polygon(pt, src_seg)
                elif src_is_connector:
                    pt = (src_node["centroid"][1], src_node["centroid"][0])
                    p1, p2, _ = connect_point_bbox(pt, tgt_bbox)
                elif tgt_is_connector:
                    pt = (tgt_node["centroid"][1], tgt_node["centroid"][0])
                    p2, p1, _ = connect_point_bbox(pt, src_bbox)
                elif src_has_poly and tgt_has_poly:
                    p1, p2, _ = connect_polygon_polygon(src_seg, tgt_seg)
                elif src_has_poly:
                    p2, p1, _ = connect_bbox_polygon(tgt_bbox, src_seg)
                elif tgt_has_poly:
                    p1, p2, _ = connect_bbox_polygon(src_bbox, tgt_seg)
                else:
                    p1, p2, _ = connect_bbox_bbox(src_bbox, tgt_bbox)

                # source_point/target_point format: [y, x]
                edge_data["source_point"] = [p1[1], p1[0]]
                edge_data["target_point"] = [p2[1], p2[0]]

            except Exception as exc:
                logger.debug(
                    "Edge recalc failed for %s-%s: %s", src_id, tgt_id, exc
                )
                # Leave old connection points

            # Update edge visual
            self._update_edge_path(edge_key)

    # =================================================================
    # Visual override -- confidence-based coloring
    # =================================================================

    def _get_equipment_brush(self, node: dict) -> QBrush:
        """Color equipment nodes by contour status.

        Only in apply_contour mode:
        - Green: contour applied
        - Yellow: low confidence (< 0.85), needs review
        - Light blue: contour available, not yet applied
        - Gray: no SAM2 contour (skip_class, no ann_idx, etc.)

        In edit_polygon mode: transparent (normal graph colors).
        """
        if self._current_mode != "apply_contour":
            return QBrush(QColor(0, 0, 0, 0))

        # Need node_id to check applied status
        node_id = node.get("id")
        ann_idx = node.get("ann_idx")

        if node_id and node_id in self._applied_nodes:
            return QBrush(self.COLOR_CONTOUR_APPLIED)

        if ann_idx is not None:
            cn = self._ann_to_contour.get(ann_idx)
            if cn and cn.get("polygon_auto"):
                if cn.get("confidence", 0) < 0.85:
                    return QBrush(self.COLOR_CONTOUR_REVIEW)
                return QBrush(self.COLOR_CONTOUR_AVAILABLE)

        return QBrush(self.COLOR_CONTOUR_NONE)

    # =================================================================
    # Stats
    # =================================================================

    def get_contour_stats(self) -> dict:
        """Contour-specific statistics."""
        total_equipment = sum(
            1 for n in self.nodes.values() if n.get("type") == "equipment"
        )
        with_ann_idx = sum(
            1 for n in self.nodes.values()
            if n.get("ann_idx") is not None
        )
        has_contour = sum(
            1 for n in self.nodes.values()
            if n.get("ann_idx") in self._ann_to_contour
        )
        applied = len(self._applied_nodes)

        return {
            "total_equipment": total_equipment,
            "with_ann_idx": with_ann_idx,
            "has_contour": has_contour,
            "applied": applied,
            "remaining": has_contour - applied,
        }

    # =================================================================
    # Polygon editing -- enter / exit
    # =================================================================

    def enter_polygon_editing(self, node_id: str):
        """Enter polygon editing/drawing for a node.

        If node has polygon -> EDIT phase (vertex handles).
        If node has no polygon -> DRAW phase (click-by-click).
        """
        self.exit_polygon_editing()

        self._editing_node = node_id
        node = self.nodes.get(node_id)
        if not node:
            return

        seg = node.get("segmentation")
        has_polygon = seg and isinstance(seg, list) and len(seg) >= 6

        self._polygon_overlay = PolygonVertexOverlay(self.scene, node_id)

        if has_polygon:
            self._polygon_overlay.show_edit(seg)
            self.update_status(
                f"Редактирование: {node_id} ({len(seg)//2} вершин) "
                "— Drag=перемещение, Click ребро=добавить, RMB=удалить"
            )
        else:
            self._polygon_overlay.show_draw()
            self.update_status(
                f"Рисование: {node_id} — Click=точка, "
                "RMB/Enter=замкнуть, Escape=отмена"
            )

    def exit_polygon_editing(self):
        """Exit polygon editing for current node."""
        if self._polygon_overlay:
            self._polygon_overlay.hide()
            self._polygon_overlay = None
        self._editing_node = None

    def set_mode(self, name: str):
        """Override: refresh equipment brush colors when switching modes.

        SAM2 confidence coloring only in apply_contour mode.
        """
        old_mode = self._current_mode
        super().set_mode(name)
        # Refresh brushes if switching to/from apply_contour
        if (old_mode == "apply_contour") != (name == "apply_contour"):
            self._refresh_equipment_brushes()

    def _refresh_equipment_brushes(self):
        """Update fill brush on all equipment polygon/bbox items."""
        for node_id, node in self.nodes.items():
            if node.get("type") != "equipment":
                continue
            brush = self._get_equipment_brush(node)
            if node_id in self.polygon_items:
                self.polygon_items[node_id].setBrush(brush)
            elif node_id in self.bbox_items:
                self.bbox_items[node_id].setBrush(brush)

    # =================================================================
    # Polygon editing -- shared mutation
    # =================================================================

    def update_polygon_in_node(self, node_id: str, polygon: list):
        """Update polygon in node + full recalculation chain.

        Mutation chain:
          1. node["segmentation"] = polygon
          2. _recalculate_centroid (Shoelace)
          3. _recalculate_bbox (extent)
          4. remove_node_items + _draw_single_node (redraw)
          5. _recalculate_edges_for_node (all connected edges)
        """
        node = self.nodes.get(node_id)
        if not node:
            return
        node["segmentation"] = polygon
        self._recalculate_centroid(node_id, polygon)
        self._recalculate_bbox(node_id, polygon)
        self.remove_node_items(node_id)
        self._draw_single_node(node_id)
        self._recalculate_edges_for_node(node_id)

    def mark_was_edited(self, node_id: str):
        """Mark was_edited=true in contour_data for training data."""
        ann_idx = self.nodes.get(node_id, {}).get("ann_idx")
        cn = self._ann_to_contour.get(ann_idx)
        if cn:
            cn["was_edited"] = True

    # =================================================================
    # Overrides -- mousePressEvent, Ctrl+RMB, Ctrl+Drag, undo/redo, keyPress
    # =================================================================

    def mousePressEvent(self, event):
        """In edit_polygon mode: intercept vertex/edge/draw hits BEFORE
        base class find_node_at, so all polygon interactions work reliably.
        """
        if (self.ctrl_pressed
                and event.button() == Qt.MouseButton.LeftButton
                and self._current_mode == "edit_polygon"
                and self._polygon_overlay):
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()

            if self._polygon_overlay.phase == "edit":
                # Vertex hit -> set up drag pending
                vtx = self._polygon_overlay.find_vertex_at(x, y)
                if vtx is not None:
                    self._ctrl_lmb_pending = True
                    self._ctrl_lmb_start_x = x
                    self._ctrl_lmb_start_y = y
                    self._ctrl_lmb_node = self._editing_node
                    self._ctrl_lmb_dragging = False
                    event.accept()
                    return

                # Edge hit -> add vertex immediately
                edge_idx = self._polygon_overlay.find_edge_at(x, y)
                if edge_idx is not None:
                    self._current_handler.on_press(self, x, y, event)
                    event.accept()
                    return

            elif self._polygon_overlay.phase == "draw":
                # Draw phase: route directly to handler
                # (bypass _ctrl_lmb_pending so points land at exact click)
                self._current_handler.on_press(self, x, y, event)
                event.accept()
                return

        super().mousePressEvent(event)

    def _ctrl_right_click_delete(self, x: float, y: float):
        """Ctrl+RMB in edit_polygon mode.

        Priority:
          1. DRAW phase: undo last draw point
          2. EDIT phase + vertex under cursor: delete vertex
          3. Fallthrough: exit overlay if on editing node, then
             super() deletes node+edges or edge (standard behavior)

        Disabled in apply_contour mode.
        """
        if self._current_mode != "edit_polygon":
            return

        # DRAW phase: undo last point
        if (self._polygon_overlay
                and self._polygon_overlay.phase == "draw"):
            if self._polygon_overlay.draw_point_count > 0:
                self._polygon_overlay.undo_last_draw_point()
            else:
                self.exit_polygon_editing()
            return

        # EDIT phase: check vertex first
        if (self._polygon_overlay
                and self._polygon_overlay.phase == "edit"):
            vtx = self._polygon_overlay.find_vertex_at(x, y)
            if vtx is not None:
                if self._editing_node not in self.nodes:
                    self.exit_polygon_editing()
                    return
                if self._polygon_overlay.vertex_count <= 3:
                    self.update_status("Минимум 3 вершины")
                    return
                from ui.editors.commands.polygon_commands import (
                    DeleteVertexCommand,
                )
                cmd = DeleteVertexCommand(
                    self, self._editing_node, vtx,
                )
                self.undo_mgr.execute(cmd)
                node = self.nodes.get(self._editing_node)
                seg = node.get("segmentation") if node else None
                if seg:
                    self._polygon_overlay.refresh_edit(seg)
                self.update_statistics()
                self.update_status(
                    f"Удалена вершина #{vtx} "
                    f"({self._polygon_overlay.vertex_count} осталось)"
                )
                return

        # No vertex hit → check what's under cursor
        clicked = self.find_node_at(x, y)

        # Editing node centroid → delete POLYGON (not node), switch to DRAW
        if clicked == self._editing_node:
            from ui.editors.commands.polygon_commands import (
                DeletePolygonCommand,
            )
            cmd = DeletePolygonCommand(self, self._editing_node)
            self.undo_mgr.execute(cmd)
            self.update_statistics()
            self._polygon_overlay.show_draw()
            self.update_status(
                f"Полигон удалён: {self._editing_node} — "
                "Ctrl+Click для новых точек, Enter = замкнуть"
            )
            return

        # Other node or edge → standard deletion (inherited)
        super()._ctrl_right_click_delete(x, y)

    def _start_ctrl_drag(self, node_id: str):
        """In edit_polygon: check vertex first, then fallback."""
        if (self._current_mode == "edit_polygon"
                and self._polygon_overlay
                and self._polygon_overlay.phase == "edit"):
            vtx = self._polygon_overlay.find_vertex_at(
                self._ctrl_lmb_start_x, self._ctrl_lmb_start_y,
            )
            if vtx is not None:
                self._polygon_overlay.start_drag(vtx)
                return
        super()._start_ctrl_drag(node_id)

    def _update_ctrl_drag(self, x: float, y: float):
        """In edit_polygon: route to overlay drag."""
        if (self._current_mode == "edit_polygon"
                and self._polygon_overlay
                and self._polygon_overlay.is_dragging):
            self._polygon_overlay.drag_to(x, y)
            return
        super()._update_ctrl_drag(x, y)

    def _end_ctrl_drag(self):
        """In edit_polygon: finalize vertex drag -> MoveVertexCommand."""
        if (self._current_mode == "edit_polygon"
                and self._polygon_overlay
                and self._polygon_overlay.is_dragging):
            idx = self._polygon_overlay._dragging_idx
            new_x = self._polygon_overlay._polygon[idx * 2]
            new_y = self._polygon_overlay._polygon[idx * 2 + 1]
            result = self._polygon_overlay.end_drag()

            if result and self._editing_node:
                old_x, old_y = result
                # Only create command if actually moved
                if abs(old_x - new_x) > 0.5 or abs(old_y - new_y) > 0.5:
                    if self._editing_node not in self.nodes:
                        self.exit_polygon_editing()
                        return
                    from ui.editors.commands.polygon_commands import (
                        MoveVertexCommand,
                    )
                    cmd = MoveVertexCommand(
                        self, self._editing_node, idx,
                        old_x, old_y, new_x, new_y,
                    )
                    # Already moved visually; just push + run full recalc
                    self.undo_mgr.push_executed(cmd)
                    seg = self._polygon_overlay.get_polygon()
                    self.update_polygon_in_node(self._editing_node, seg)
                    self.mark_was_edited(self._editing_node)
                    # Refresh overlay to match recalculated polygon
                    node = self.nodes.get(self._editing_node)
                    new_seg = node.get("segmentation") if node else None
                    if new_seg:
                        self._polygon_overlay.refresh_edit(new_seg)
                    self.update_statistics()
            return
        super()._end_ctrl_drag()

    def keyPressEvent(self, event):
        """Handle Delete/Backspace (delete polygon) and Enter (close draw)."""
        if self._current_mode == "edit_polygon" and self._editing_node:
            key = event.key()

            # Delete/Backspace in EDIT -> delete entire polygon -> DRAW
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                if (self._polygon_overlay
                        and self._polygon_overlay.phase == "edit"):
                    from ui.editors.commands.polygon_commands import (
                        DeletePolygonCommand,
                    )
                    cmd = DeletePolygonCommand(self, self._editing_node)
                    self.undo_mgr.execute(cmd)
                    self.update_statistics()
                    # Switch overlay to draw phase
                    self._polygon_overlay.show_draw()
                    self.update_status(
                        f"Полигон удален: {self._editing_node} — "
                        "рисуйте новый"
                    )
                    return

            # Enter in DRAW -> close polygon
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if (self._polygon_overlay
                        and self._polygon_overlay.phase == "draw"
                        and self._polygon_overlay.draw_point_count >= 3):
                    self._close_draw_polygon()
                    return

            # Escape -> exit editing
            if key == Qt.Key.Key_Escape:
                if (self._polygon_overlay
                        and self._polygon_overlay.phase == "draw"):
                    # Cancel drawing
                    self._polygon_overlay.show_draw()  # clears points
                self.exit_polygon_editing()
                self.set_mode("apply_contour")
                return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """If Ctrl released during vertex drag -> finalize."""
        if event.key() == Qt.Key.Key_Control:
            if (self._polygon_overlay
                    and self._polygon_overlay.is_dragging):
                self._end_ctrl_drag()
        super().keyReleaseEvent(event)

    def undo(self):
        """After undo, sync overlay with node state."""
        super().undo()
        self._sync_overlay_after_undo_redo()

    def redo(self):
        """After redo, sync overlay with node state."""
        super().redo()
        self._sync_overlay_after_undo_redo()

    def _sync_overlay_after_undo_redo(self):
        """Sync polygon overlay with actual node state after undo/redo."""
        if not self._editing_node or not self._polygon_overlay:
            return

        node = self.nodes.get(self._editing_node)
        if not node:
            self.exit_polygon_editing()
            return

        seg = node.get("segmentation")
        has_poly = seg and isinstance(seg, list) and len(seg) >= 6

        if has_poly and self._polygon_overlay.phase == "edit":
            self._polygon_overlay.refresh_edit(seg)
        elif has_poly and self._polygon_overlay.phase == "draw":
            # Polygon reappeared (redo after delete) -> switch to edit
            self._polygon_overlay.show_edit(seg)
        elif not has_poly and self._polygon_overlay.phase == "edit":
            # Polygon disappeared (undo draw) -> switch to draw
            self._polygon_overlay.show_draw()

    # =================================================================
    # Polygon drawing -- close helper
    # =================================================================

    def _close_draw_polygon(self):
        """Close the drawn polygon and create DrawPolygonCommand."""
        if not self._polygon_overlay or not self._editing_node:
            return

        points = self._polygon_overlay.get_draw_points()
        if len(points) < 3:
            return

        # Build flat polygon
        polygon = []
        for x, y in points:
            polygon.append(x)
            polygon.append(y)

        from ui.editors.commands.polygon_commands import DrawPolygonCommand

        cmd = DrawPolygonCommand(self, self._editing_node, polygon)
        self.undo_mgr.execute(cmd)
        self.update_statistics()

        # Switch to edit phase for fine-tuning
        seg = self.nodes[self._editing_node].get("segmentation")
        if seg:
            self._polygon_overlay.show_edit(seg)
            self.update_status(
                f"Полигон нарисован: {self._editing_node} "
                f"({len(points)} вершин) — правьте вершины"
            )
