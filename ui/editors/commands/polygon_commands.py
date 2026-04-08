"""
Polygon Commands -- undo/redo for polygon vertex editing and drawing.

All commands call editor.update_polygon_in_node() which runs the full
mutation chain: segmentation -> centroid -> bbox -> redraw -> edges.
"""

from ui.editors.undo_manager import Command


class MoveVertexCommand(Command):
    """Move a single polygon vertex."""

    def __init__(self, editor, node_id: str, vertex_idx: int,
                 old_x: float, old_y: float, new_x: float, new_y: float):
        self._editor = editor
        self._node_id = node_id
        self._idx = vertex_idx
        self._old_x, self._old_y = old_x, old_y
        self._new_x, self._new_y = new_x, new_y

    def execute(self):
        self._set_vertex(self._new_x, self._new_y)

    def undo(self):
        self._set_vertex(self._old_x, self._old_y)

    def _set_vertex(self, x, y):
        node = self._editor.nodes.get(self._node_id)
        if not node or not node.get("segmentation"):
            return
        seg = node["segmentation"]
        i2 = self._idx * 2
        if i2 + 1 < len(seg):
            seg[i2] = x
            seg[i2 + 1] = y
            self._editor.update_polygon_in_node(self._node_id, seg)
            self._editor.mark_was_edited(self._node_id)

    @property
    def description(self) -> str:
        return f"Move vertex #{self._idx}: {self._node_id}"


class AddVertexCommand(Command):
    """Insert a new vertex on a polygon edge."""

    def __init__(self, editor, node_id: str, insert_idx: int,
                 x: float, y: float):
        self._editor = editor
        self._node_id = node_id
        self._insert_idx = insert_idx
        self._x = x
        self._y = y

    def execute(self):
        node = self._editor.nodes.get(self._node_id)
        if not node or not node.get("segmentation"):
            return
        seg = node["segmentation"]
        i2 = self._insert_idx * 2
        seg.insert(i2, self._x)
        seg.insert(i2 + 1, self._y)
        self._editor.update_polygon_in_node(self._node_id, seg)
        self._editor.mark_was_edited(self._node_id)

    def undo(self):
        node = self._editor.nodes.get(self._node_id)
        if not node or not node.get("segmentation"):
            return
        seg = node["segmentation"]
        i2 = self._insert_idx * 2
        if i2 + 1 < len(seg):
            seg.pop(i2 + 1)  # y (higher index first)
            seg.pop(i2)      # x
            self._editor.update_polygon_in_node(self._node_id, seg)

    @property
    def description(self) -> str:
        return f"Add vertex #{self._insert_idx}: {self._node_id}"


class DeleteVertexCommand(Command):
    """Delete a single vertex from polygon (min 3 enforced by caller)."""

    def __init__(self, editor, node_id: str, vertex_idx: int):
        self._editor = editor
        self._node_id = node_id
        self._idx = vertex_idx

        seg = editor.nodes[node_id]["segmentation"]
        self._deleted_x = seg[vertex_idx * 2]
        self._deleted_y = seg[vertex_idx * 2 + 1]

    def execute(self):
        node = self._editor.nodes.get(self._node_id)
        if not node or not node.get("segmentation"):
            return
        seg = node["segmentation"]
        i2 = self._idx * 2
        if i2 + 1 < len(seg):
            seg.pop(i2 + 1)  # y
            seg.pop(i2)      # x
            self._editor.update_polygon_in_node(self._node_id, seg)
            self._editor.mark_was_edited(self._node_id)

    def undo(self):
        node = self._editor.nodes.get(self._node_id)
        if not node or not node.get("segmentation"):
            return
        seg = node["segmentation"]
        i2 = self._idx * 2
        seg.insert(i2, self._deleted_x)
        seg.insert(i2 + 1, self._deleted_y)
        self._editor.update_polygon_in_node(self._node_id, seg)

    @property
    def description(self) -> str:
        return f"Delete vertex #{self._idx}: {self._node_id}"


class DrawPolygonCommand(Command):
    """Draw a new polygon for a node (from click-by-click points)."""

    def __init__(self, editor, node_id: str, new_polygon: list):
        self._editor = editor
        self._node_id = node_id
        self._new_polygon = new_polygon[:]

        node = editor.nodes[node_id]
        self._old_seg = node.get("segmentation")
        if self._old_seg and isinstance(self._old_seg, list):
            self._old_seg = self._old_seg[:]
        self._old_centroid = node["centroid"][:]
        self._old_bbox = node["bbox"][:] if node.get("bbox") else None
        self._was_applied = node_id in editor._applied_nodes

    def execute(self):
        self._editor.update_polygon_in_node(self._node_id, self._new_polygon)
        self._editor._applied_nodes.add(self._node_id)
        self._editor.mark_was_edited(self._node_id)

    def undo(self):
        node = self._editor.nodes.get(self._node_id)
        if not node:
            return
        node["segmentation"] = self._old_seg
        node["centroid"] = self._old_centroid[:]
        node["bbox"] = self._old_bbox[:] if self._old_bbox else None
        if not self._was_applied:
            self._editor._applied_nodes.discard(self._node_id)
        self._editor.remove_node_items(self._node_id)
        self._editor._draw_single_node(self._node_id)
        self._editor._recalculate_edges_for_node(self._node_id)

    @property
    def description(self) -> str:
        n = len(self._new_polygon) // 2
        return f"Draw polygon ({n} pts): {self._node_id}"


class DeletePolygonCommand(Command):
    """Delete entire polygon from a node (Delete/Backspace in edit phase)."""

    def __init__(self, editor, node_id: str):
        self._editor = editor
        self._node_id = node_id

        node = editor.nodes[node_id]
        self._old_seg = node["segmentation"][:]
        self._old_centroid = node["centroid"][:]
        self._old_bbox = node["bbox"][:] if node.get("bbox") else None

        # Restore original bbox (pre-SAM2) if available
        self._restore_bbox = editor._original_bbox.get(node_id)
        if self._restore_bbox:
            self._restore_bbox = self._restore_bbox[:]
        self._restore_centroid = editor._original_centroid.get(node_id)
        if self._restore_centroid:
            self._restore_centroid = self._restore_centroid[:]

    def execute(self):
        node = self._editor.nodes.get(self._node_id)
        if not node:
            return
        node["segmentation"] = None
        # Restore pre-SAM2 bbox/centroid if available
        if self._restore_bbox:
            node["bbox"] = self._restore_bbox[:]
        if self._restore_centroid:
            node["centroid"] = self._restore_centroid[:]
        self._editor._applied_nodes.discard(self._node_id)
        self._editor.remove_node_items(self._node_id)
        self._editor._draw_single_node(self._node_id)
        self._editor._recalculate_edges_for_node(self._node_id)

    def undo(self):
        node = self._editor.nodes.get(self._node_id)
        if not node:
            return
        node["segmentation"] = self._old_seg[:]
        node["centroid"] = self._old_centroid[:]
        node["bbox"] = self._old_bbox[:] if self._old_bbox else None
        self._editor._applied_nodes.add(self._node_id)
        self._editor.remove_node_items(self._node_id)
        self._editor._draw_single_node(self._node_id)
        self._editor._recalculate_edges_for_node(self._node_id)

    @property
    def description(self) -> str:
        return f"Delete polygon: {self._node_id}"
