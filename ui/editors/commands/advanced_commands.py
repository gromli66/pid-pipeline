"""
Advanced Commands — 8 команд для AdvancedGraphEditor.

Включает granular и snapshot-based команды.
"""

from ui.editors.undo_manager import Command, SnapshotCommand
from ui.editors.graph_data import GraphDataModel


class OptimizeEdgeCommand(Command):
    """Оптимизировать перпендикулярность ребра."""

    def __init__(self, model: GraphDataModel, editor,
                 source_id: str, target_id: str,
                 old_source_point: list | None,
                 old_target_point: list | None,
                 old_waypoints: list,
                 new_source_point: list,
                 new_target_point: list):
        self._model = model
        self._editor = editor
        self._source_id = source_id
        self._target_id = target_id
        self._old_sp = old_source_point
        self._old_tp = old_target_point
        self._old_wp = old_waypoints
        self._new_sp = new_source_point
        self._new_tp = new_target_point
        self._key = model.edge_key(source_id, target_id)

    def execute(self):
        edge_data = self._model.find_edge_data(self._key)
        if edge_data:
            edge_data['source_point'] = self._new_sp
            edge_data['target_point'] = self._new_tp
            edge_data['waypoints'] = []
            self._editor._update_edge_path(self._key)

    def undo(self):
        edge_data = self._model.find_edge_data(self._key)
        if edge_data:
            if self._old_sp:
                edge_data['source_point'] = self._old_sp
            if self._old_tp:
                edge_data['target_point'] = self._old_tp
            edge_data['waypoints'] = self._old_wp
            self._editor._update_edge_path(self._key)

    @property
    def description(self):
        return f"Оптимизировать {self._source_id}—{self._target_id}"


class DragNodeCommand(Command):
    """Перемещение одного узла (не batch)."""

    def __init__(self, model: GraphDataModel, editor, node_id: str,
                 old_centroid: list, old_bbox: list, old_segmentation: list,
                 old_edge_points: dict, old_block_bboxes: dict | None = None):
        self._model = model
        self._editor = editor
        self._node_id = node_id
        self._old_centroid = old_centroid
        self._old_bbox = old_bbox
        self._old_seg = old_segmentation
        self._old_edge_points = old_edge_points
        # Э3: привязанные текст-блоки едут за узлом — undo возвращает и их
        self._old_block_bboxes = old_block_bboxes or {}
        # new state captured at finalize
        self._new_centroid: list | None = None
        self._new_bbox: list | None = None
        self._new_seg: list | None = None
        self._new_edge_points: dict | None = None
        self._new_block_bboxes: dict | None = None

    def capture_new_state(self):
        """Вызвать после завершения drag для сохранения нового состояния."""
        node = self._model.nodes.get(self._node_id)
        if node:
            self._new_centroid = node['centroid'].copy()
            self._new_bbox = (node.get('bbox') or []).copy()
            self._new_seg = (node.get('segmentation') or []).copy()
        # Edge points — текущее состояние всех связанных рёбер ПЛЮС
        # «уступивших» неинцидентных (Дефект 2: их пре-жестовое состояние
        # редактор дописывает в drag_start_edge_points при первом касании —
        # undo обязан вернуть и их побайтово)
        self._new_edge_points = {}
        keys = list(self._old_edge_points)
        keys += [k for k in self._model.get_connected_edges(self._node_id)
                 if k not in self._old_edge_points]
        for key in keys:
            edge_data = self._model.find_edge_data(key)
            if edge_data:
                self._new_edge_points[key] = {
                    'source_point': (edge_data.get('source_point') or []).copy(),
                    'target_point': (edge_data.get('target_point') or []).copy(),
                    'waypoints': [wp.copy() for wp in edge_data.get('waypoints', [])],
                    '_src_side': edge_data.get('_src_side'),
                    '_tgt_side': edge_data.get('_tgt_side'),
                }
        # Текст-блоки — те же id, что сняты на старте drag (привязка блока
        # во время drag измениться не может)
        self._new_block_bboxes = {}
        for bid in self._old_block_bboxes:
            blk = self._model.find_text_block(bid)
            if blk and blk.get('bbox'):
                self._new_block_bboxes[bid] = list(blk['bbox'])

    def execute(self):
        # execute используется только при redo
        self._apply_state(self._new_centroid, self._new_bbox, self._new_seg,
                          self._new_edge_points, self._new_block_bboxes)

    def undo(self):
        self._apply_state(self._old_centroid, self._old_bbox, self._old_seg,
                          self._old_edge_points, self._old_block_bboxes)

    def redo(self):
        self._apply_state(self._new_centroid, self._new_bbox, self._new_seg,
                          self._new_edge_points, self._new_block_bboxes)

    def _apply_state(self, centroid, bbox, seg, edge_points, block_bboxes=None):
        node = self._model.nodes.get(self._node_id)
        if not node:
            return
        if centroid:
            node['centroid'] = centroid.copy()
        if bbox:
            node['bbox'] = bbox.copy()
        if seg:
            node['segmentation'] = seg.copy()

        if edge_points:
            for key, points in edge_points.items():
                edge_data = self._model.find_edge_data(key)
                if edge_data:
                    if 'source_point' in points:
                        edge_data['source_point'] = points['source_point'].copy()
                    if 'target_point' in points:
                        edge_data['target_point'] = points['target_point'].copy()
                    if 'waypoints' in points:
                        edge_data['waypoints'] = [wp.copy() for wp in points['waypoints']]
                    for side_key in ('_src_side', '_tgt_side'):
                        if side_key in points:
                            if points[side_key] is None:
                                edge_data.pop(side_key, None)
                            else:
                                edge_data[side_key] = points[side_key]

        if block_bboxes:
            for bid, bb in block_bboxes.items():
                blk = self._model.find_text_block(bid)
                if blk is not None:
                    blk['bbox'] = list(bb)

        self._editor._redraw_all()

    @property
    def description(self):
        return f"Переместить {self._node_id}"


class BatchDragCommand(SnapshotCommand):
    """Batch drag группы узлов — snapshot."""

    def __init__(self, model, redraw_callback):
        super().__init__(model, redraw_callback)
        self.description = "Переместить группу"


class MoveWaypointCommand(Command):
    """Перемещение waypoint."""

    def __init__(self, model: GraphDataModel, editor,
                 edge_key: tuple, wp_idx: int,
                 old_wp: list, new_wp: list):
        self._model = model
        self._editor = editor
        self._edge_key = edge_key
        self._wp_idx = wp_idx
        self._old_wp = old_wp
        self._new_wp = new_wp

    def execute(self):
        self._set_wp(self._new_wp)

    def undo(self):
        self._set_wp(self._old_wp)

    def redo(self):
        self._set_wp(self._new_wp)

    def _set_wp(self, wp_value):
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data and self._wp_idx < len(edge_data.get('waypoints', [])):
            edge_data['waypoints'][self._wp_idx] = wp_value.copy()
            self._editor._update_edge_path(self._edge_key)

    @property
    def description(self):
        return "Переместить waypoint"


class AddWaypointCommand(Command):
    """Добавить waypoint на сегменте ребра."""

    def __init__(self, model: GraphDataModel, editor,
                 edge_key: tuple, wp_idx: int, wp_value: list):
        self._model = model
        self._editor = editor
        self._edge_key = edge_key
        self._wp_idx = wp_idx
        self._wp_value = wp_value

    def execute(self):
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data:
            edge_data.setdefault('waypoints', []).insert(self._wp_idx, self._wp_value.copy())
            self._editor._update_edge_path(self._edge_key)

    def undo(self):
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data:
            waypoints = edge_data.get('waypoints', [])
            if self._wp_idx < len(waypoints):
                waypoints.pop(self._wp_idx)
            self._editor._update_edge_path(self._edge_key)

    @property
    def description(self):
        return "Добавить waypoint"


class DeleteWaypointCommand(Command):
    """Удалить waypoint."""

    def __init__(self, model: GraphDataModel, editor,
                 edge_key: tuple, wp_idx: int, old_wp: list):
        self._model = model
        self._editor = editor
        self._edge_key = edge_key
        self._wp_idx = wp_idx
        self._old_wp = old_wp

    def execute(self):
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data:
            waypoints = edge_data.get('waypoints', [])
            if self._wp_idx < len(waypoints):
                waypoints.pop(self._wp_idx)
            self._editor._update_edge_path(self._edge_key)

    def undo(self):
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data:
            edge_data.setdefault('waypoints', []).insert(self._wp_idx, self._old_wp.copy())
            self._editor._update_edge_path(self._edge_key)

    @property
    def description(self):
        return "Удалить waypoint"


class AutoLRouteCommand(Command):
    """Автоматический L-route для ребра."""

    def __init__(self, model: GraphDataModel, editor,
                 edge_key: tuple, old_waypoints: list):
        self._model = model
        self._editor = editor
        self._edge_key = edge_key
        self._old_waypoints = old_waypoints
        self._new_waypoints: list | None = None

    def capture_new_waypoints(self):
        """Вызвать после _recalculate_edge."""
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data:
            self._new_waypoints = [wp.copy() for wp in edge_data.get('waypoints', [])]

    def execute(self):
        if self._new_waypoints is not None:
            edge_data = self._model.find_edge_data(self._edge_key)
            if edge_data:
                edge_data['waypoints'] = [wp.copy() for wp in self._new_waypoints]
                self._editor._update_edge_path(self._edge_key)

    def undo(self):
        edge_data = self._model.find_edge_data(self._edge_key)
        if edge_data:
            edge_data['waypoints'] = [wp.copy() for wp in self._old_waypoints]
            self._editor._update_edge_path(self._edge_key)

    def redo(self):
        self.execute()

    @property
    def description(self):
        return f"L-route {self._edge_key}"


class AutoFixCommand(SnapshotCommand):
    """Auto-fix всего графа — snapshot."""

    def __init__(self, model, redraw_callback):
        super().__init__(model, redraw_callback)
        self.description = "Auto-Fix"


class SetEdgeStyleCommand(SnapshotCommand):
    """Изменение цвета/толщины/пунктира одного или нескольких рёбер — snapshot.

    Цвет, толщина и флаг пунктира хранятся прямо в edge_data
    ('render_color' / 'render_width' / 'dashed'). Snapshot модели — глубокая
    копия edges_data целиком, поэтому все эти поля (включая 'dashed')
    корректно отменяются/повторяются без перечисления ключей.
    """

    def __init__(self, model, redraw_callback, description: str = "Стиль рёбер"):
        super().__init__(model, redraw_callback)
        self.description = description
