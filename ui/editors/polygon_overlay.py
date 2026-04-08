"""
Polygon Vertex Overlay -- interactive vertex handles for polygon editing.

Two phases:
  EDIT: vertex handles on existing polygon (drag, add, delete vertices)
  DRAW: click-by-click polygon drawing with preview line

Pattern: analogous to ResizableNodeOverlay (handles on scene, callbacks).
"""

import math
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsPathItem,
    QGraphicsScene,
)
from PySide6.QtGui import QColor, QPen, QBrush, QPainterPath
from PySide6.QtCore import Qt


class PolygonVertexOverlay:
    """Interactive polygon vertex handles on QGraphicsScene.

    Usage (EDIT):
        overlay = PolygonVertexOverlay(scene, node_id)
        overlay.show_edit(polygon)
        # In mousePressEvent:
        vtx = overlay.find_vertex_at(x, y)
        if vtx is not None: overlay.start_drag(vtx)
        # In mouseMoveEvent:
        overlay.drag_to(x, y)
        # In mouseReleaseEvent:
        old_x, old_y = overlay.end_drag()

    Usage (DRAW):
        overlay = PolygonVertexOverlay(scene, node_id)
        overlay.show_draw()
        overlay.add_draw_point(x, y)
        overlay.update_draw_preview(cursor_x, cursor_y)
        if overlay.is_near_first_point(x, y): ...
    """

    # Sizes
    VERTEX_RADIUS = 5
    DRAW_POINT_RADIUS = 4
    EDGE_HIT_THRESHOLD = 8
    SNAP_THRESHOLD = 12

    # Z-values (above everything)
    VERTEX_Z = 21
    EDGE_LINE_Z = 20
    PREVIEW_Z = 19

    # Colors -- EDIT
    COLOR_VERTEX = QColor(241, 196, 15)           # yellow
    COLOR_VERTEX_FILL = QColor(241, 196, 15, 80)
    COLOR_VERTEX_HOVER = QColor(241, 196, 15, 200)
    COLOR_VERTEX_DRAG = QColor(255, 255, 255, 220)
    COLOR_EDGE_LINE = QColor(241, 196, 15, 120)
    COLOR_GHOST = QColor(200, 200, 200, 140)

    # Colors -- DRAW
    COLOR_DRAW_POINT = QColor(46, 204, 113)       # green
    COLOR_DRAW_FILL = QColor(46, 204, 113, 80)
    COLOR_DRAW_LINE = QColor(46, 204, 113, 180)
    COLOR_DRAW_PREVIEW = QColor(46, 204, 113, 100)
    COLOR_SNAP_RING = QColor(46, 204, 113, 200)

    def __init__(self, scene: QGraphicsScene, node_id: str):
        self._scene = scene
        self._node_id = node_id

        # Phase
        self._phase: str = "idle"  # "idle" | "edit" | "draw"

        # EDIT state
        self._polygon: list = []                  # flat [x1,y1,x2,y2,...]
        self._vertex_items: list[QGraphicsEllipseItem] = []
        self._edge_line_items: list[QGraphicsLineItem] = []
        self._ghost_item: QGraphicsEllipseItem | None = None
        self._hovered_vertex: int | None = None
        self._hovered_edge: int | None = None
        self._dragging_idx: int | None = None
        self._drag_start_x: float = 0
        self._drag_start_y: float = 0

        # DRAW state
        self._draw_points: list[tuple[float, float]] = []
        self._draw_point_items: list[QGraphicsEllipseItem] = []
        self._draw_line_items: list[QGraphicsLineItem] = []
        self._draw_preview_line: QGraphicsLineItem | None = None
        self._draw_snap_ring: QGraphicsEllipseItem | None = None

    # =================================================================
    # Properties
    # =================================================================

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def vertex_count(self) -> int:
        return len(self._polygon) // 2

    @property
    def draw_point_count(self) -> int:
        return len(self._draw_points)

    @property
    def is_dragging(self) -> bool:
        return self._dragging_idx is not None

    @property
    def node_id(self) -> str:
        return self._node_id

    def get_polygon(self) -> list:
        """Current polygon as flat list [x1,y1,x2,y2,...]."""
        return self._polygon[:]

    def get_draw_points(self) -> list[tuple[float, float]]:
        return self._draw_points[:]

    # =================================================================
    # EDIT phase -- show / hide
    # =================================================================

    def show_edit(self, polygon: list):
        """Show vertex handles for existing polygon."""
        self.hide()
        self._phase = "edit"
        self._polygon = polygon[:]
        self._rebuild_edit_items()

    def _rebuild_edit_items(self):
        """Create/recreate all vertex handles and edge lines."""
        self._clear_edit_items()

        n = self.vertex_count
        if n < 3:
            return

        # Edge lines (below vertices)
        for i in range(n):
            x1 = self._polygon[i * 2]
            y1 = self._polygon[i * 2 + 1]
            j = (i + 1) % n
            x2 = self._polygon[j * 2]
            y2 = self._polygon[j * 2 + 1]

            line = QGraphicsLineItem(x1, y1, x2, y2)
            line.setPen(QPen(self.COLOR_EDGE_LINE, 2))
            line.setZValue(self.EDGE_LINE_Z)
            self._scene.addItem(line)
            self._edge_line_items.append(line)

        # Vertex handles
        r = self.VERTEX_RADIUS
        pen = QPen(self.COLOR_VERTEX, 2)
        brush = QBrush(self.COLOR_VERTEX_FILL)

        for i in range(n):
            x = self._polygon[i * 2]
            y = self._polygon[i * 2 + 1]
            item = QGraphicsEllipseItem(x - r, y - r, r * 2, r * 2)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(self.VERTEX_Z)
            self._scene.addItem(item)
            self._vertex_items.append(item)

    def _clear_edit_items(self):
        for item in self._vertex_items:
            self._scene.removeItem(item)
        self._vertex_items.clear()

        for item in self._edge_line_items:
            self._scene.removeItem(item)
        self._edge_line_items.clear()

        if self._ghost_item:
            self._scene.removeItem(self._ghost_item)
            self._ghost_item = None

        self._hovered_vertex = None
        self._hovered_edge = None

    # =================================================================
    # EDIT -- hit testing
    # =================================================================

    def find_vertex_at(self, x: float, y: float) -> int | None:
        """Find vertex index at (x, y). Returns None if not found."""
        r = self.VERTEX_RADIUS + 4
        best_idx = None
        best_dist = r

        n = self.vertex_count
        for i in range(n):
            vx = self._polygon[i * 2]
            vy = self._polygon[i * 2 + 1]
            dist = math.hypot(x - vx, y - vy)
            if dist < best_dist:
                best_dist = dist
                best_idx = i

        return best_idx

    def find_edge_at(self, x: float, y: float) -> int | None:
        """Find polygon edge index at (x, y). Returns None if not found."""
        n = self.vertex_count
        if n < 2:
            return None

        best_idx = None
        best_dist = self.EDGE_HIT_THRESHOLD

        for i in range(n):
            x1 = self._polygon[i * 2]
            y1 = self._polygon[i * 2 + 1]
            j = (i + 1) % n
            x2 = self._polygon[j * 2]
            y2 = self._polygon[j * 2 + 1]

            dist, _, _ = self._point_to_segment_dist(x, y, x1, y1, x2, y2)
            if dist < best_dist:
                best_dist = dist
                best_idx = i

        return best_idx

    @staticmethod
    def _point_to_segment_dist(
        px: float, py: float,
        ax: float, ay: float, bx: float, by: float,
    ) -> tuple[float, float, float]:
        """Distance from point to segment. Returns (dist, proj_x, proj_y)."""
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-9:
            d = math.hypot(px - ax, py - ay)
            return d, ax, ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        proj_x = ax + t * dx
        proj_y = ay + t * dy
        return math.hypot(px - proj_x, py - proj_y), proj_x, proj_y

    # =================================================================
    # EDIT -- hover
    # =================================================================

    def update_hover(self, x: float, y: float):
        """Update hover state: highlight vertex or show ghost on edge."""
        if self._phase != "edit" or self.is_dragging:
            return

        # Priority 1: vertex hover
        vtx = self.find_vertex_at(x, y)
        if vtx != self._hovered_vertex:
            # Unhover old
            if self._hovered_vertex is not None and self._hovered_vertex < len(self._vertex_items):
                self._vertex_items[self._hovered_vertex].setBrush(
                    QBrush(self.COLOR_VERTEX_FILL)
                )
            # Hover new
            if vtx is not None and vtx < len(self._vertex_items):
                self._vertex_items[vtx].setBrush(
                    QBrush(self.COLOR_VERTEX_HOVER)
                )
            self._hovered_vertex = vtx

        # Priority 2: edge hover (ghost point) -- only if no vertex hovered
        if vtx is not None:
            self._hide_ghost()
            self._hovered_edge = None
            return

        edge = self.find_edge_at(x, y)
        if edge != self._hovered_edge:
            self._hovered_edge = edge
            if edge is not None:
                self._show_ghost(x, y, edge)
            else:
                self._hide_ghost()
        elif edge is not None:
            # Update ghost position
            self._move_ghost(x, y, edge)

    def _show_ghost(self, x: float, y: float, edge_idx: int):
        """Show ghost vertex on polygon edge (insert preview)."""
        i = edge_idx
        j = (i + 1) % self.vertex_count
        x1 = self._polygon[i * 2]
        y1 = self._polygon[i * 2 + 1]
        x2 = self._polygon[j * 2]
        y2 = self._polygon[j * 2 + 1]

        _, proj_x, proj_y = self._point_to_segment_dist(x, y, x1, y1, x2, y2)

        r = self.VERTEX_RADIUS
        if not self._ghost_item:
            self._ghost_item = QGraphicsEllipseItem(0, 0, r * 2, r * 2)
            self._ghost_item.setPen(QPen(self.COLOR_GHOST, 2))
            self._ghost_item.setBrush(QBrush(self.COLOR_GHOST))
            self._ghost_item.setZValue(self.VERTEX_Z)
            self._scene.addItem(self._ghost_item)

        self._ghost_item.setRect(proj_x - r, proj_y - r, r * 2, r * 2)

    def _move_ghost(self, x: float, y: float, edge_idx: int):
        """Move ghost to projection on edge."""
        if not self._ghost_item:
            self._show_ghost(x, y, edge_idx)
            return

        i = edge_idx
        j = (i + 1) % self.vertex_count
        x1 = self._polygon[i * 2]
        y1 = self._polygon[i * 2 + 1]
        x2 = self._polygon[j * 2]
        y2 = self._polygon[j * 2 + 1]

        _, proj_x, proj_y = self._point_to_segment_dist(x, y, x1, y1, x2, y2)
        r = self.VERTEX_RADIUS
        self._ghost_item.setRect(proj_x - r, proj_y - r, r * 2, r * 2)

    def _hide_ghost(self):
        if self._ghost_item:
            self._scene.removeItem(self._ghost_item)
            self._ghost_item = None

    # =================================================================
    # EDIT -- vertex drag
    # =================================================================

    def start_drag(self, vertex_idx: int):
        """Start dragging a vertex."""
        self._dragging_idx = vertex_idx
        self._drag_start_x = self._polygon[vertex_idx * 2]
        self._drag_start_y = self._polygon[vertex_idx * 2 + 1]

        if vertex_idx < len(self._vertex_items):
            self._vertex_items[vertex_idx].setBrush(
                QBrush(self.COLOR_VERTEX_DRAG)
            )

    def drag_to(self, x: float, y: float):
        """Move dragged vertex to (x, y). Updates overlay visuals only."""
        if self._dragging_idx is None:
            return

        idx = self._dragging_idx
        self._polygon[idx * 2] = x
        self._polygon[idx * 2 + 1] = y

        # Update vertex handle position
        r = self.VERTEX_RADIUS
        if idx < len(self._vertex_items):
            self._vertex_items[idx].setRect(x - r, y - r, r * 2, r * 2)

        # Update adjacent edge lines
        n = self.vertex_count
        prev_i = (idx - 1) % n
        next_i = (idx + 1) % n

        # Edge from prev to idx
        if prev_i < len(self._edge_line_items):
            px = self._polygon[prev_i * 2]
            py = self._polygon[prev_i * 2 + 1]
            self._edge_line_items[prev_i].setLine(px, py, x, y)

        # Edge from idx to next
        if idx < len(self._edge_line_items):
            nx = self._polygon[next_i * 2]
            ny = self._polygon[next_i * 2 + 1]
            self._edge_line_items[idx].setLine(x, y, nx, ny)

    def end_drag(self) -> tuple[float, float] | None:
        """End drag. Returns (old_x, old_y) for undo command, or None."""
        if self._dragging_idx is None:
            return None

        idx = self._dragging_idx
        old_x, old_y = self._drag_start_x, self._drag_start_y

        # Reset visual
        if idx < len(self._vertex_items):
            self._vertex_items[idx].setBrush(QBrush(self.COLOR_VERTEX_FILL))

        self._dragging_idx = None
        return old_x, old_y

    # =================================================================
    # EDIT -- insert / remove vertex
    # =================================================================

    def insert_vertex(self, edge_idx: int, x: float, y: float) -> int:
        """Insert vertex on edge. Returns new vertex index.

        Insert AFTER edge_idx-th vertex (between edge_idx and edge_idx+1).
        """
        insert_idx = edge_idx + 1
        self._polygon.insert(insert_idx * 2, x)
        self._polygon.insert(insert_idx * 2 + 1, y)
        self._rebuild_edit_items()
        return insert_idx

    def remove_vertex(self, vertex_idx: int) -> bool:
        """Remove vertex. Returns False if vertex_count <= 3."""
        if self.vertex_count <= 3:
            return False

        self._polygon.pop(vertex_idx * 2 + 1)  # y first (higher index)
        self._polygon.pop(vertex_idx * 2)       # then x
        self._rebuild_edit_items()
        return True

    # =================================================================
    # DRAW phase -- show / points
    # =================================================================

    def show_draw(self):
        """Initialize draw phase (empty canvas)."""
        self.hide()
        self._phase = "draw"
        self._draw_points.clear()

    def add_draw_point(self, x: float, y: float):
        """Add a point to the drawing polygon."""
        self._draw_points.append((x, y))

        # Draw point marker
        r = self.DRAW_POINT_RADIUS
        item = QGraphicsEllipseItem(x - r, y - r, r * 2, r * 2)
        item.setPen(QPen(self.COLOR_DRAW_POINT, 2))
        item.setBrush(QBrush(self.COLOR_DRAW_FILL))
        item.setZValue(self.VERTEX_Z)
        self._scene.addItem(item)
        self._draw_point_items.append(item)

        # Draw edge from previous point
        n = len(self._draw_points)
        if n >= 2:
            px, py = self._draw_points[-2]
            line = QGraphicsLineItem(px, py, x, y)
            line.setPen(QPen(self.COLOR_DRAW_LINE, 2))
            line.setZValue(self.EDGE_LINE_Z)
            self._scene.addItem(line)
            self._draw_line_items.append(line)

        # Show snap ring on first point when >= 3 points
        if n >= 3:
            self._show_snap_ring()

    def undo_last_draw_point(self):
        """Remove the last drawn point."""
        if not self._draw_points:
            return

        self._draw_points.pop()

        # Remove point marker
        if self._draw_point_items:
            item = self._draw_point_items.pop()
            self._scene.removeItem(item)

        # Remove edge line
        if self._draw_line_items:
            item = self._draw_line_items.pop()
            self._scene.removeItem(item)

        # Update snap ring
        if len(self._draw_points) < 3:
            self._hide_snap_ring()

        # Remove preview line
        if self._draw_preview_line:
            self._scene.removeItem(self._draw_preview_line)
            self._draw_preview_line = None

    def update_draw_preview(self, x: float, y: float):
        """Update dashed preview line from last point to cursor."""
        if self._phase != "draw" or not self._draw_points:
            return

        lx, ly = self._draw_points[-1]

        if not self._draw_preview_line:
            self._draw_preview_line = QGraphicsLineItem(lx, ly, x, y)
            pen = QPen(self.COLOR_DRAW_PREVIEW, 2, Qt.PenStyle.DashLine)
            self._draw_preview_line.setPen(pen)
            self._draw_preview_line.setZValue(self.PREVIEW_Z)
            self._scene.addItem(self._draw_preview_line)
        else:
            self._draw_preview_line.setLine(lx, ly, x, y)

    def is_near_first_point(self, x: float, y: float) -> bool:
        """True if (x,y) is within snap threshold of first point and >= 3 points."""
        if len(self._draw_points) < 3:
            return False
        fx, fy = self._draw_points[0]
        return math.hypot(x - fx, y - fy) < self.SNAP_THRESHOLD

    def _show_snap_ring(self):
        """Show enlarged ring around first point (close hint)."""
        if not self._draw_points:
            return

        fx, fy = self._draw_points[0]
        r = self.SNAP_THRESHOLD

        if not self._draw_snap_ring:
            self._draw_snap_ring = QGraphicsEllipseItem(
                fx - r, fy - r, r * 2, r * 2
            )
            self._draw_snap_ring.setPen(QPen(self.COLOR_SNAP_RING, 1, Qt.PenStyle.DashLine))
            self._draw_snap_ring.setBrush(QBrush(QColor(0, 0, 0, 0)))
            self._draw_snap_ring.setZValue(self.PREVIEW_Z)
            self._scene.addItem(self._draw_snap_ring)
        else:
            self._draw_snap_ring.setRect(fx - r, fy - r, r * 2, r * 2)

    def _hide_snap_ring(self):
        if self._draw_snap_ring:
            self._scene.removeItem(self._draw_snap_ring)
            self._draw_snap_ring = None

    def _clear_draw_items(self):
        for item in self._draw_point_items:
            self._scene.removeItem(item)
        self._draw_point_items.clear()

        for item in self._draw_line_items:
            self._scene.removeItem(item)
        self._draw_line_items.clear()

        if self._draw_preview_line:
            self._scene.removeItem(self._draw_preview_line)
            self._draw_preview_line = None

        self._hide_snap_ring()
        self._draw_points.clear()

    # =================================================================
    # Common
    # =================================================================

    def hide(self):
        """Remove all overlay items from scene."""
        self._clear_edit_items()
        self._clear_draw_items()
        self._dragging_idx = None
        self._phase = "idle"

    def refresh_edit(self, polygon: list):
        """Refresh overlay after external polygon change (undo/redo)."""
        if self._phase == "edit":
            self._polygon = polygon[:]
            self._rebuild_edit_items()
