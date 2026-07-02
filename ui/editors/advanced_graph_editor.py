"""
Advanced Graph Editor — полный редактор графа P&ID.

Расширяет SimpleGraphEditor: routing, optimize, drag, multi-select, waypoints, auto-fix, grid.

Overrides: load_data, add_edge, _get_edge_color, _get_edge_pen,
  _before_draw_all_edges, _before_edge_draw, _reset_scene_state,
  _redraw_all, _after_statistics_update.
"""

import math
import sys
import statistics
from copy import deepcopy
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsPathItem,
    QGraphicsLineItem, QGraphicsSimpleTextItem, QGraphicsPixmapItem, QDialog,
    QVBoxLayout, QFormLayout, QLineEdit, QDialogButtonBox, QLabel,
    QToolTip,
)
from PySide6.QtGui import QColor, QBrush, QPen, QPainterPath, QFont, QPixmap, QTransform
from PySide6.QtCore import Qt

from ui.editors.simple_graph_editor import SimpleGraphEditor
from ui.editors.mode_handlers.advanced_handlers import (
    AddEdgeWithWaypointsHandler,
    OptimizeEdgeHandler, DragNodeHandler, MultiSelectHandler, EditWaypointHandler,
    EditEdgeColorHandler, EditEdgeSizeHandler, ResizeObjectsHandler,
)
from ui.editors.commands.advanced_commands import (
    OptimizeEdgeCommand, DragNodeCommand, BatchDragCommand,
    MoveWaypointCommand, AddWaypointCommand, DeleteWaypointCommand,
    AutoLRouteCommand, AutoFixCommand, SetEdgeStyleCommand,
)
from ui.editors.graph_geometry import (
    bbox_exit_side, bbox_side_midpoint, closest_bbox_side,
    project_point_to_bbox_border, project_point_to_polygon_border,
    get_node_geometry, compute_edge_perpendicularity,
    connect_bbox_bbox, connect_bbox_polygon, connect_polygon_polygon,
    connect_point_bbox, connect_point_polygon,
    global_axis_perpendicularity,
    node_orientation_by_edges,
)
from ui.editors.edge_routing import distribute_connection_points, route_edge as route_edge_v2, segment_intersects_bbox
from ui.editors.optimize_core import compute_optimized_route
from ui.editors.autofix_chains import auto_fix_graph
from ui.editors.ocr_layer_mixin import (
    OcrLayerMixin, AddOcrBlockHandler, OcrBindHandler,
)


class AdvancedGraphEditor(OcrLayerMixin, SimpleGraphEditor):
    """Полный редактор: routing, оптимизация, drag, multi-select, waypoints, auto-fix.

    Плюс OCR-слой (OcrLayerMixin): текст-блоки и их привязка к узлам/рёбрам,
    активные в состоянии display_regime == 'ocr'.
    """

    def __init__(self):
        super().__init__()
        self._init_ocr_layer()

        # ── Viewport mouse tracking для hover tooltip ──
        self.viewport().setMouseTracking(True)

        # ── B6.5: project config dir for KKS normalization ──
        self._project_config_dir: str | None = None

        # ── Переопределяем add_edge handler для waypoints ──
        self.register_mode("add_edge", AddEdgeWithWaypointsHandler())

        # ── Дополнительные режимы ──
        self.register_mode("optimize_edge", OptimizeEdgeHandler())
        self.register_mode("drag_node", DragNodeHandler())
        self.register_mode("multi_select", MultiSelectHandler())
        self.register_mode("edit_waypoint", EditWaypointHandler())
        self.register_mode("edit_edge_color", EditEdgeColorHandler())
        self.register_mode("edit_edge_size", EditEdgeSizeHandler())
        self.register_mode("resize_objects", ResizeObjectsHandler())

        # ── OCR-слой: добавление блока + резидентная привязка ──
        self.register_mode("add_ocr_block", AddOcrBlockHandler())
        self.register_mode("ocr_bind", OcrBindHandler())

        # ── Режим изменения ребра (цвет / размер) ──
        # Текущий «кисточный» цвет и размер, которыми красятся/масштабируются рёбра.
        self.edge_brush_color: QColor = QColor("#e74c3c")  # по умолчанию красный
        self.edge_brush_size: int = self.EDGE_WIDTH        # стартовый размер = базовая толщина
        # Callback в таб для синхронизации числа размера в тулбаре.
        self.edge_size_callback: Optional[callable] = None

        # ── Edge building with waypoints ──
        self._pending_waypoints: list[list] = []  # [[y, x], ...]
        self._wp_preview_items: list = []  # QGraphicsItems для preview

        # ── Perp data ──
        self.edge_perp_scores: dict[tuple[str, str], dict] = {}
        self.show_bad_edges: bool = True

        # ── Multi-select ──
        self.selected_nodes: set[str] = set()
        self.selected_edges: set[tuple[str, str]] = set()
        self._selection_highlights: dict[str, QGraphicsEllipseItem] = {}
        self._edge_selection_highlights: dict[tuple[str, str], QGraphicsPathItem] = {}
        self._rubber_band: QGraphicsRectItem | None = None
        self._rb_start_x: float = 0
        self._rb_start_y: float = 0
        self._rb_active: bool = False

        # ── Waypoint / Endpoint ──
        self.waypoint_markers: dict[tuple[str, str], list] = {}
        self.dragging_waypoint: tuple | None = None
        self.dragging_wp_start: list | None = None
        self._wp_ghost_marker: QGraphicsRectItem | None = None
        self._endpoint_markers: dict[tuple, list] = {}
        self._dragging_endpoint: tuple | None = None
        self._dragging_ep_start_side: str | None = None

        # ── Drag ──
        self.dragging_node: str | None = None
        self.drag_start_centroid: list | None = None
        self.drag_start_bbox: list = []
        self.drag_start_segmentation: list = []
        self.drag_start_edge_points: dict = {}
        self._batch_drag: bool = False
        self._batch_internal_edges: list = []
        self._batch_boundary_edges: list = []
        self._drag_prev_x: float = 0
        self._drag_prev_y: float = 0

        # ── Grid ──
        self.grid_visible: bool = False
        self.grid_size: int = 24
        self.snap_threshold: int = 12
        self.wp_snap_size: int = 4
        self._grid_items: list = []

        # ── Auto-fix ──
        self._auto_fix_result: dict | None = None
        self._auto_fix_preview_items: list = []

        # ── KKS labels ──
        self._kks_labels: dict[str, QGraphicsSimpleTextItem] = {}  # node_id → label item
        self._kks_tooltip_visible: bool = False

        # ── Режим отображения/правки: 'ocr' | 'perp' | 'style' ──
        #   ocr   — подсветка привязки (KKS-цвета узлов, красные рёбра без
        #           диаметра, KKS-подписи/подсказки, правка диаметра/KKS
        #           двойным кликом);
        #   perp  — перпендикулярность (оранжевые рёбра + утолщение);
        #   style — размер и цвет рёбер (по умолчанию белые, кисть цвет/размер);
        #   base  — базовое (нейтральное) состояние: только общие функции,
        #           без подсветок и без инструментов состояний.
        self.display_regime: str = "base"
        # Callback в таб для синхронизации кнопок-флагов режима.
        self.regime_callback: Optional[callable] = None

        # ── Режим «Размер объектов» ──
        self._resize_class: str | None = None      # выбранный класс
        self._resize_sel: set[str] = set()         # node_id экземпляров в наборе
        self._resize_frames: list = []             # QGraphicsItem жёлтых рамок
        # Базлайн для живого превью (геометрия до изменения + снимок модели для undo).
        self._resize_base: dict = {}               # node_id → исходная геометрия
        self._resize_model_base = None             # snapshot модели до превью
        # Колбэки в таб (назначаются при готовности редактора):
        self.resize_panel_show_cb: Optional[callable] = None     # (visible: bool)
        self.resize_panel_classes_cb: Optional[callable] = None  # (names, current)
        self.resize_panel_state_cb: Optional[callable] = None     # (kind, count, mw, mh)
        # Зазор при расталкивании наслоившихся боксов (px).
        self.RESIZE_SPREAD_GAP: int = 8

        # ── Правка точек полигона (базовое состояние, Ctrl+2ЛКМ по полигону) ──
        self._poly_edit_node: str | None = None   # редактируемый узел
        self._poly_overlay = None                 # PolygonVertexOverlay | None
        self._poly_op_before = None               # snapshot модели до текущей операции (undo)

        # ── Отображение FXML-скинов внутри боксов ──
        self.show_skins: bool = False
        self._skin_pixmaps: dict[str, QPixmap] = {}   # class_name → QPixmap | None (кэш)
        self._skin_items: dict[str, list] = {}        # node_id → [bg_rect, pixmap_item]

    # =================================================================
    # Overrides — Base/Simple hooks
    # =================================================================

    @property
    def _ocr_highlight(self) -> bool:
        """Back-compat: подсветка привязки активна только в режиме 'ocr'."""
        return self.display_regime == "ocr"

    def load_data(self, image_path: str, graph_path: str, coco_path: str = "") -> bool:
        """Загрузка + _compute_grid_size()."""
        result = super().load_data(image_path, graph_path, coco_path)
        if result:
            self._compute_grid_size()
        return result

    def _get_edge_color(self, edge_data: dict, key: tuple = None) -> QColor:
        """Цвет ребра — зависит от активного режима.

        style → индивидуальный цвет (render_color) или белый по умолчанию;
        perp  → оранжевый для неперпендикулярных, иначе белый;
        ocr   → красный для рёбер без диаметра, иначе белый.
        Индивидуальный цвет показывается ТОЛЬКО в режиме style
        (в FXML экспортируется всегда).
        """
        if self.display_regime == "style":
            rc = edge_data.get('render_color')
            return QColor(rc) if rc else self.COLOR_EDGE

        if self.display_regime == "perp":
            waypoints = edge_data.get('waypoints', [])
            if not waypoints and key and key in self.edge_perp_scores:
                if not self.edge_perp_scores[key].get('is_good', True):
                    return self.COLOR_EDGE_BAD
            return self.COLOR_EDGE

        # ocr — визуал как во вкладке привязки: рёбра нейтральные (без красных);
        #        вся информация о диаметрах/KKS показывается на текст-блоках.
        # base — нейтральное отображение без подсветок.
        return self.COLOR_EDGE

    def _get_equipment_brush(self, node: dict) -> QBrush:
        """Заливка equipment — нейтральная во всех состояниях.

        В состоянии «ОКР привязка» узлы рисуются как во вкладке привязки
        (нейтрально), а привязка KKS отражается на текст-блоках, а не заливкой.
        """
        return super()._get_equipment_brush(node)

    def set_edge_no_diameter_color(self, color: QColor):
        """Цвет рёбер без диаметра (подсветка привязки)."""
        self.COLOR_NO_DIAMETER = QColor(color)
        self._redraw_all()

    def set_edge_bad_color(self, color: QColor):
        """Цвет неперпендикулярных (плохих) рёбер."""
        self.COLOR_EDGE_BAD = QColor(color)
        self._redraw_all()

    def set_display_regime(self, regime: str):
        """Переключить режим отображения/правки: 'base' | 'ocr' | 'perp' | 'style'.

        Режимы взаимоисключающие — активен ровно один.
        При смене состояния активный инструмент состояния сбрасывается в idle.
        """
        if regime not in ("base", "ocr", "perp", "style"):
            return
        if self.display_regime == regime:
            return
        # Выход из правки полигона при смене состояния (с коммитом).
        if self._poly_edit_node:
            self._exit_polygon_editing_mode(commit=True)
        self.display_regime = regime
        # Сбросить инструменты, специфичные для состояний, при выходе из них.
        state_modes = ("edit_edge_color", "edit_edge_size", "optimize_edge",
                       "edit_waypoint", "add_ocr_block", "ocr_bind")
        if self._current_mode in state_modes:
            self.set_mode("idle")
        # KKS-подписи показываются только в режиме 'ocr'
        if regime != "ocr":
            self._hide_all_kks_labels()
            if hasattr(self, "_selected_ocr"):
                self._selected_ocr.clear()
        # В состоянии 'ocr' резидентный режим привязки блоков.
        if regime == "ocr" and self._current_mode in ("", "idle"):
            self.set_mode("ocr_bind")
        # OCR-слой блоков виден только в состоянии 'ocr' (если слой подключён).
        if hasattr(self, "_refresh_ocr_layer_visibility"):
            self._refresh_ocr_layer_visibility()
        self._redraw_all()
        if self.regime_callback:
            self.regime_callback(regime)

    def set_ocr_highlight(self, enabled: bool):
        """Back-compat обёртка: вкл → режим 'ocr', выкл → 'perp'."""
        self.set_display_regime("ocr" if enabled else "perp")

    def _get_edge_pen(self, edge_data: dict, key: tuple = None) -> QPen:
        """Утолщение для неперпендикулярных рёбер.

        Приоритет — индивидуальная толщина ребра (режим «Размер ребра»).
        """
        color = self._get_edge_color(edge_data, key)

        # Индивидуальная толщина — только в режиме «Размер и цвет».
        if self.display_regime == "style":
            render_width = edge_data.get('render_width')
            if render_width:
                return QPen(color, float(render_width))
            return QPen(color, self.EDGE_WIDTH)

        pen_width = self.EDGE_WIDTH
        if self.display_regime == "perp" and key and key in self.edge_perp_scores:
            if not self.edge_perp_scores[key].get('is_good', True):
                pen_width = self.EDGE_WIDTH + 1
        return QPen(color, pen_width)

    def _before_draw_all_edges(self):
        """Очистить perp scores перед перерисовкой."""
        self.edge_perp_scores.clear()

    def _before_edge_draw(self, key: tuple, edge: dict):
        """Вычислить перпендикулярность ДО рисования."""
        waypoints = edge.get('waypoints', [])
        if waypoints:
            self.edge_perp_scores[key] = {
                'is_good': True, 'score': 1.0,
                'source_angle': 0, 'target_angle': 0,
            }
        else:
            sp = edge.get('source_point')
            tp = edge.get('target_point')
            if sp and tp:
                source_geom = get_node_geometry(self.nodes[edge['source']])
                target_geom = get_node_geometry(self.nodes[edge['target']])
                perp_info = compute_edge_perpendicularity(
                    (sp[1], sp[0]), (tp[1], tp[0]), source_geom, target_geom)
                self.edge_perp_scores[key] = perp_info

    def _reset_scene_state(self):
        """Очистить Advanced dict'ы ДО Base."""
        self.waypoint_markers.clear()
        self._grid_items.clear()
        self._selection_highlights.clear()
        self._edge_selection_highlights.clear()
        self._endpoint_markers.clear()
        self._auto_fix_preview_items.clear()
        self._kks_labels.clear()
        self._wp_preview_items.clear()
        self._pending_waypoints.clear()
        self._rubber_band = None
        self._rb_active = False
        self._dragging_endpoint = None
        self.dragging_waypoint = None
        self._auto_fix_result = None
        # OCR-слой: сцена уже очищается в Base; сбросить ссылки на item'ы.
        if hasattr(self, "_ocr_block_items"):
            self._ocr_block_items.clear()
            self._ocr_hl_restore = []
        super()._reset_scene_state()

    def _redraw_all(self):
        """Восстанавливает grid после перерисовки."""
        super()._redraw_all()
        if self.grid_visible:
            self._draw_grid()
        self._redraw_skins()
        # OCR текст-блоки поверх графа (видимы только в состоянии 'ocr').
        if hasattr(self, "_ocr_block_items"):
            self.refresh_ocr_layer()

    def _after_statistics_update(self):
        """Обновить multi-select визуалы."""
        self._update_selection_visuals()

    # =================================================================
    # Override: add_edge — полный L-route
    # =================================================================

    def add_edge(self, node_a: str, node_b: str) -> bool:
        """Добавить ребро с ПЕРПЕНДИКУЛЯРНЫМ соединением + L-route.

        Логика из оригинального graph_editor.py:1380-1530.
        """
        if node_a == node_b:
            self.update_status("Нельзя соединить узел с самим собой")
            return False

        key = self.model.edge_key(node_a, node_b)
        if key in self.edges:
            self.update_status(f"Ребро уже существует: {node_a} — {node_b}")
            return False

        src = self.nodes[node_a]
        tgt = self.nodes[node_b]

        src_cx, src_cy = src['centroid'][1], src['centroid'][0]
        tgt_cx, tgt_cy = tgt['centroid'][1], tgt['centroid'][0]

        src_bbox = src.get('bbox')
        tgt_bbox = tgt.get('bbox')
        src_poly = src.get('segmentation')
        tgt_poly = tgt.get('segmentation')

        src_has_bbox = src_bbox and len(src_bbox) == 4
        tgt_has_bbox = tgt_bbox and len(tgt_bbox) == 4
        src_has_poly = src_poly and isinstance(src_poly, list) and len(src_poly) >= 6
        tgt_has_poly = tgt_poly and isinstance(tgt_poly, list) and len(tgt_poly) >= 6

        src_type = src.get('type', 'connector')
        tgt_type = tgt.get('type', 'connector')
        src_is_point = src_type == 'connector' and not src_has_bbox and not src_has_poly
        tgt_is_point = tgt_type == 'connector' and not tgt_has_bbox and not tgt_has_poly

        src_x, src_y, tgt_x, tgt_y = None, None, None, None
        connection_type = "centroid"

        if src_is_point and tgt_is_point:
            src_x, src_y = src_cx, src_cy
            tgt_x, tgt_y = tgt_cx, tgt_cy
            connection_type = "point_point"
        elif src_is_point:
            if tgt_has_poly:
                (src_x, src_y), (tgt_x, tgt_y), _ = connect_point_polygon((src_cx, src_cy), tgt_poly)
            elif tgt_has_bbox:
                (src_x, src_y), (tgt_x, tgt_y), connection_type = connect_point_bbox((src_cx, src_cy), tgt_bbox)
        elif tgt_is_point:
            if src_has_poly:
                (tgt_x, tgt_y), (src_x, src_y), _ = connect_point_polygon((tgt_cx, tgt_cy), src_poly)
            elif src_has_bbox:
                (tgt_x, tgt_y), (src_x, src_y), connection_type = connect_point_bbox((tgt_cx, tgt_cy), src_bbox)
        elif src_has_bbox and tgt_has_bbox and not src_has_poly and not tgt_has_poly:
            (src_x, src_y), (tgt_x, tgt_y), connection_type = connect_bbox_bbox(src_bbox, tgt_bbox)
        elif src_has_bbox and tgt_has_poly:
            (src_x, src_y), (tgt_x, tgt_y), _ = connect_bbox_polygon(src_bbox, tgt_poly)
        elif src_has_poly and tgt_has_bbox:
            (tgt_x, tgt_y), (src_x, src_y), _ = connect_bbox_polygon(tgt_bbox, src_poly)
        elif src_has_poly and tgt_has_poly:
            (src_x, src_y), (tgt_x, tgt_y), _ = connect_polygon_polygon(src_poly, tgt_poly)
        elif src_has_bbox and tgt_has_bbox:
            (src_x, src_y), (tgt_x, tgt_y), connection_type = connect_bbox_bbox(src_bbox, tgt_bbox)

        if src_x is None or tgt_x is None:
            tgt_x, tgt_y = self.get_connection_point(node_b, src_cx, src_cy)
            src_x, src_y = self.get_connection_point(node_a, tgt_x, tgt_y)
            connection_type = "centroid_fallback"

        edge_data = self.model.create_edge_data(
            node_a, node_b,
            source_point=[src_y, src_x],
            target_point=[tgt_y, tgt_x],
        )
        edge_data['straight_line_distance'] = math.sqrt((tgt_x - src_x)**2 + (tgt_y - src_y)**2)
        edge_data['connection_type'] = connection_type

        from ui.editors.commands.simple_commands import AddEdgeCommand
        cmd = AddEdgeCommand(self.model, self, node_a, node_b, edge_data)
        self.undo_mgr.execute(cmd)

        self.update_statistics()
        self.update_status(f"Добавлено ребро: {node_a} — {node_b} [{connection_type}]")
        return True

    def add_edge_with_waypoints(self, node_a: str, node_b: str,
                                waypoints: list[list]) -> bool:
        """Добавить ребро с пользовательскими waypoints.

        Args:
            node_a: ID стартового узла
            node_b: ID конечного узла
            waypoints: список [[y, x], ...] промежуточных точек
        """
        if node_a == node_b:
            self.update_status("Нельзя соединить узел с самим собой")
            return False

        key = self.model.edge_key(node_a, node_b)
        if key in self.edges:
            self.update_status(f"Ребро уже существует: {node_a} — {node_b}")
            return False

        src = self.nodes[node_a]
        tgt = self.nodes[node_b]

        src_cx, src_cy = src['centroid'][1], src['centroid'][0]
        tgt_cx, tgt_cy = tgt['centroid'][1], tgt['centroid'][0]

        # Connection points — к первому/последнему waypoint или к target
        if waypoints:
            first_wp_x, first_wp_y = waypoints[0][1], waypoints[0][0]
            last_wp_x, last_wp_y = waypoints[-1][1], waypoints[-1][0]
            src_x, src_y = self.get_connection_point(node_a, first_wp_x, first_wp_y)
            tgt_x, tgt_y = self.get_connection_point(node_b, last_wp_x, last_wp_y)
        else:
            # Без waypoints: сначала цель (по центроиду источника),
            # потом источник (по РЕАЛЬНОЙ точке на цели)
            tgt_x, tgt_y = self.get_connection_point(node_b, src_cx, src_cy)
            src_x, src_y = self.get_connection_point(node_a, tgt_x, tgt_y)

        edge_data = self.model.create_edge_data(
            node_a, node_b,
            source_point=[src_y, src_x],
            target_point=[tgt_y, tgt_x],
        )
        edge_data['waypoints'] = [wp.copy() for wp in waypoints]
        edge_data['straight_line_distance'] = math.sqrt(
            (tgt_x - src_x) ** 2 + (tgt_y - src_y) ** 2
        )
        edge_data['connection_type'] = "manual_waypoints"

        from ui.editors.commands.simple_commands import AddEdgeCommand
        cmd = AddEdgeCommand(self.model, self, node_a, node_b, edge_data)
        self.undo_mgr.execute(cmd)

        self.update_statistics()
        wp_count = len(waypoints)
        self.update_status(
            f"Добавлено ребро: {node_a} — {node_b} ({wp_count} waypoints)"
        )
        return True

    # =================================================================
    # Edge building preview (waypoints)
    # =================================================================

    def _update_edge_build_preview(self, mouse_x: float, mouse_y: float):
        """Preview ломаной: source → waypoints → cursor."""
        self._clear_waypoint_preview()

        if not self.selected_node:
            return

        src = self.nodes[self.selected_node]
        points = [(src['centroid'][1], src['centroid'][0])]

        # Накопленные waypoints
        for wp in self._pending_waypoints:
            points.append((wp[1], wp[0]))

        # Конечная точка — курсор или hovered node
        if self.hovered_node and self.hovered_node != self.selected_node:
            tgt = self.nodes[self.hovered_node]
            end_x, end_y = tgt['centroid'][1], tgt['centroid'][0]
            edge_exists = self.model.edge_exists(
                self.selected_node, self.hovered_node
            )
            line_color = self.COLOR_PREVIEW_NO if edge_exists else self.COLOR_PREVIEW_OK
        else:
            end_x, end_y = mouse_x, mouse_y
            line_color = self.COLOR_SELECTION

        points.append((end_x, end_y))

        # Рисуем ломаную
        path = QPainterPath()
        path.moveTo(points[0][0], points[0][1])
        for px, py in points[1:]:
            path.lineTo(px, py)

        pen = QPen(line_color, 2, Qt.PenStyle.DashLine)
        path_item = QGraphicsPathItem(path)
        path_item.setPen(pen)
        path_item.setZValue(8)
        self.scene.addItem(path_item)
        self._wp_preview_items.append(path_item)

        # Рисуем маркеры waypoints
        for wp in self._pending_waypoints:
            wx, wy = wp[1], wp[0]
            r = 4
            marker = QGraphicsEllipseItem(wx - r, wy - r, r * 2, r * 2)
            marker.setPen(QPen(self.COLOR_SELECTION, 2))
            marker.setBrush(QBrush(self.COLOR_SELECTION))
            marker.setZValue(9)
            self.scene.addItem(marker)
            self._wp_preview_items.append(marker)

        # Обновляем стандартный preview_line — убираем чтобы не дублировал
        if self.preview_line:
            self.scene.removeItem(self.preview_line)
            self.preview_line = None

    def _update_waypoint_preview(self):
        """Обновить статические маркеры waypoints (без линии к курсору)."""
        # Маркеры перерисуются при следующем on_move
        pass

    def _clear_waypoint_preview(self):
        """Убрать все preview items со сцены."""
        for item in self._wp_preview_items:
            self.scene.removeItem(item)
        self._wp_preview_items.clear()

    # =================================================================
    # Optimization
    # =================================================================

    def optimize_edge(self, node_a: str, node_b: str) -> bool:
        """Оптимизировать ребро — пересчитать точки соединения."""
        key = self.model.edge_key(node_a, node_b)
        if key not in self.edges:
            self.update_status(f"Ребро не существует: {node_a} — {node_b}")
            return False

        edge_data = self.model.find_edge_data(key)
        if not edge_data:
            return False

        original_source_id = edge_data['source']
        original_target_id = edge_data['target']

        old_sp = edge_data.get('source_point', []).copy() if edge_data.get('source_point') else None
        old_tp = edge_data.get('target_point', []).copy() if edge_data.get('target_point') else None
        old_wp = [wp.copy() for wp in edge_data.get('waypoints', [])]

        # Шаг 5 (PLAN_routing_patch): ортогональный маршрут через optimize_core
        # вместо прямой линии. Ручные точки (_manual_route) сохраняются,
        # иначе equipment цепляется за центр стороны, коннектор — за центр.
        route = compute_optimized_route(
            self.nodes, self.edges_data, edge_data,
            conn_radius=self.CONNECTOR_MARKER_RADIUS,
        )
        new_sp = route['source_point']
        new_tp = route['target_point']
        new_wp = route['waypoints']
        edge_data['_src_side'] = route['src_side']
        edge_data['_tgt_side'] = route['tgt_side']

        cmd = OptimizeEdgeCommand(
            self.model, self,
            original_source_id, original_target_id,
            old_sp, old_tp, old_wp,
            new_sp, new_tp, new_waypoints=new_wp,
        )
        self.undo_mgr.execute(cmd)

        # Перпендикулярность: ортогональный маршрут с изломами всегда «хороший»,
        # прямое ребро оцениваем по фактическому углу.
        if new_wp:
            perp_info = {'is_good': True, 'score': 1.0, 'source_angle': 0}
        else:
            source_geom = get_node_geometry(self.nodes[original_source_id])
            target_geom = get_node_geometry(self.nodes[original_target_id])
            perp_info = compute_edge_perpendicularity(
                (new_sp[1], new_sp[0]), (new_tp[1], new_tp[0]),
                source_geom, target_geom)
        self.edge_perp_scores[key] = perp_info

        # Перерисовать ребро ПОСЛЕ обновления perp_scores
        self.remove_edge_item(key)
        edge_data = self.model.find_edge_data(key)
        if edge_data:
            self.create_edge_item(key, edge_data)

        self.update_status(
            f"Оптимизировано: {original_source_id} — {original_target_id} "
            f"({len(new_wp)} waypoints)")
        return True

    def optimize_all_edges(self) -> int:
        """Оптимизировать все неперпендикулярные рёбра."""
        optimized = 0
        edges_to_optimize = [
            key for key, info in self.edge_perp_scores.items()
            if not info.get('is_good', True)
        ]
        for node_a, node_b in edges_to_optimize:
            if self.optimize_edge(node_a, node_b):
                optimized += 1
        self.update_status(f"Оптимизировано {optimized} рёбер из {len(edges_to_optimize)}")
        self.update_statistics()
        return optimized

    def get_perpendicularity_stats(self) -> dict:
        """Статистика перпендикулярности."""
        total = len(self.edge_perp_scores)
        good = sum(1 for info in self.edge_perp_scores.values() if info.get('is_good', True))
        bad = total - good
        avg_score = sum(info.get('score', 1.0) for info in self.edge_perp_scores.values()) / total if total else 1.0
        return {'total': total, 'good': good, 'bad': bad, 'avg_score': avg_score}

    def update_optimize_preview(self, mouse_x: float, mouse_y: float):
        """Подсветка ребра для оптимизации."""
        if self.edge_highlight:
            self.scene.removeItem(self.edge_highlight)
            self.edge_highlight = None

        edge_key, _ = self.find_nearest_edge(mouse_x, mouse_y, threshold=20.0)
        if edge_key:
            edge_data = self.model.find_edge_data(edge_key)
            if edge_data:
                path = self._build_edge_path(
                    edge_data.get('source_point'),
                    edge_data.get('waypoints', []),
                    edge_data.get('target_point'))
                self.edge_highlight = QGraphicsPathItem(path)
                perp_info = self.edge_perp_scores.get(edge_key, {})
                if perp_info.get('is_good', True):
                    self.edge_highlight.setPen(QPen(self.COLOR_EDGE, 4))
                else:
                    self.edge_highlight.setPen(QPen(self.COLOR_EDGE_HIGHLIGHT, 4))
                self.edge_highlight.setZValue(10)
                self.scene.addItem(self.edge_highlight)

                score = perp_info.get('score', 1.0)
                angle = perp_info.get('source_angle', 0)
                self.update_status(f"Ребро {edge_key[0]}—{edge_key[1]}: ⊥={score:.0%}, отклонение {angle:.1f}°")

    def update_drag_preview(self, mouse_x: float, mouse_y: float):
        """Подсветка узла для перетаскивания."""
        if self.connector_preview:
            self.scene.removeItem(self.connector_preview)
            self.connector_preview = None

        hovered = self.find_node_at(mouse_x, mouse_y)
        if hovered:
            node_data = self.nodes.get(hovered)
            if node_data:
                cy, cx = node_data['centroid']
                radius = 15
                self.connector_preview = QGraphicsEllipseItem(
                    cx - radius, cy - radius, radius * 2, radius * 2)
                self.connector_preview.setPen(QPen(self.COLOR_EDGE_HIGHLIGHT, 3))
                self.connector_preview.setBrush(QBrush(Qt.GlobalColor.transparent))
                self.connector_preview.setZValue(15)
                self.scene.addItem(self.connector_preview)

                degree = sum(1 for (a, b) in self.edges if a == hovered or b == hovered)
                node_type = node_data.get('type', 'node')
                self.update_status(f"{node_type} {hovered}: {degree} рёбер. Ctrl+Click и тащите.")
        else:
            self.update_status("Наведите на узел для перетаскивания")

    # =================================================================
    # Orthogonal routing
    # =================================================================

    def _get_node_bbox_for_routing(self, node_id: str):
        """Alias для _get_node_bbox (для читаемости routing кода)."""
        return self._get_node_bbox(node_id)

    def _recalculate_edge(self, edge_data: dict, moving_node_id: str | None = None,
                          keep_sides: bool = False):
        """Пересчитать connection points и waypoints для ребра.

        Полная логика из graph_editor.py:2497-2595.
        """
        # Ручной режим: точки прикрепления заданы пользователем вручную.
        # Не пересчитываем маршрут к ортогональности — только удерживаем
        # точки на границе узла (на случай, если узел подвинули).
        if edge_data.get('_manual_route'):
            self._reproject_manual_endpoints(edge_data)
            key = self.model.edge_key(edge_data['source'], edge_data['target'])
            self._update_edge_path(key)
            self.edge_perp_scores[key] = {'is_good': True, 'score': 1.0, 'source_angle': 0}
            return

        src_id, tgt_id = edge_data['source'], edge_data['target']
        src = self.nodes[src_id]
        tgt = self.nodes[tgt_id]

        src_cx, src_cy = src['centroid'][1], src['centroid'][0]
        tgt_cx, tgt_cy = tgt['centroid'][1], tgt['centroid'][0]

        src_bbox = self._get_node_bbox(src_id)
        tgt_bbox = self._get_node_bbox(tgt_id)

        if keep_sides:
            src_side = edge_data.get('_src_side') or bbox_exit_side(src_bbox, src_cx, src_cy, tgt_cx, tgt_cy)
            tgt_side = edge_data.get('_tgt_side') or bbox_exit_side(tgt_bbox, tgt_cx, tgt_cy, src_cx, src_cy)
        else:
            src_side = bbox_exit_side(src_bbox, src_cx, src_cy, tgt_cx, tgt_cy)
            tgt_side = bbox_exit_side(tgt_bbox, tgt_cx, tgt_cy, src_cx, src_cy)

        edge_data['_src_side'] = src_side
        edge_data['_tgt_side'] = tgt_side

        src_dist = distribute_connection_points(src_id, src_side, src_bbox, self.edges_data, self.nodes)
        tgt_dist = distribute_connection_points(tgt_id, tgt_side, tgt_bbox, self.edges_data, self.nodes)

        edge_id = edge_data.get('id', '')
        sx, sy = src_dist.get(edge_id, bbox_side_midpoint(src_bbox, src_side))
        tx, ty = tgt_dist.get(edge_id, bbox_side_midpoint(tgt_bbox, tgt_side))

        edge_data['source_point'] = [sy, sx]
        edge_data['target_point'] = [ty, tx]

        src_exit = 'H' if src_side in ('left', 'right') else 'V'
        tgt_exit = 'H' if tgt_side in ('left', 'right') else 'V'
        cp_dx = tx - sx
        cp_dy = ty - sy

        if src_exit == tgt_exit == 'H' and abs(cp_dy) < self.snap_threshold:
            edge_data['waypoints'] = []
        elif src_exit == tgt_exit == 'V' and abs(cp_dx) < self.snap_threshold:
            edge_data['waypoints'] = []
        elif abs(cp_dx) < 1 and abs(cp_dy) < 1:
            edge_data['waypoints'] = []
        else:
            obstacle_bboxes = [
                self._get_node_bbox(nid) for nid in self.nodes
                if nid != src_id and nid != tgt_id
            ]
            existing_paths = []
            for e in self.edges_data:
                if e is edge_data:
                    continue
                sp = e.get('source_point')
                tp = e.get('target_point')
                wps = e.get('waypoints', [])
                if sp and tp:
                    path = [(sp[1], sp[0])] + [(w[1], w[0]) for w in wps] + [(tp[1], tp[0])]
                    existing_paths.append(path)

            waypoints = route_edge_v2(
                src_conn=(sx, sy), tgt_conn=(tx, ty),
                src_side=src_side, tgt_side=tgt_side,
                src_bbox=src_bbox, tgt_bbox=tgt_bbox,
                obstacle_bboxes=obstacle_bboxes,
                existing_edge_paths=existing_paths,
            )
            edge_data['waypoints'] = waypoints

        edge_key = self.model.edge_key(src_id, tgt_id)
        self._update_edge_path(edge_key)

        if not edge_data['waypoints']:
            source_geom = get_node_geometry(self.nodes[src_id])
            target_geom = get_node_geometry(self.nodes[tgt_id])
            perp_info = compute_edge_perpendicularity(
                (sx, sy), (tx, ty), source_geom, target_geom)
            self.edge_perp_scores[edge_key] = perp_info
        else:
            self.edge_perp_scores[edge_key] = {'is_good': True, 'score': 1.0, 'source_angle': 0}

    def _reproject_manual_endpoints(self, edge_data: dict):
        """Удержать вручную заданные точки прикрепления на границе узлов.

        Вызывается при пересчёте ребра в ручном режиме (_manual_route): если
        узел сдвинули/изменили, точка перепроецируется на новый периметр, но
        маршрут остаётся прямым (waypoints не трогаем)."""
        for endpoint, point_key, side_key in (
            ('source', 'source_point', '_src_side'),
            ('target', 'target_point', '_tgt_side'),
        ):
            pt = edge_data.get(point_key)
            if not pt:
                continue
            node_id = edge_data['source'] if endpoint == 'source' else edge_data['target']
            px, py = self._project_to_node_border(node_id, pt[1], pt[0])
            edge_data[point_key] = [py, px]
            edge_data[side_key] = closest_bbox_side(self._get_node_bbox(node_id), px, py)

    # =================================================================
    # Multi-select
    # =================================================================

    def toggle_select_node(self, node_id: str):
        if node_id in self.selected_nodes:
            self.selected_nodes.discard(node_id)
        else:
            self.selected_nodes.add(node_id)
        self._update_selection_visuals()
        self.update_status(f"Выделено: {len(self.selected_nodes)} узлов, {len(self.selected_edges)} рёбер")

    def toggle_select_edge(self, edge_key: tuple):
        if edge_key in self.selected_edges:
            self.selected_edges.discard(edge_key)
        else:
            self.selected_edges.add(edge_key)
        self._update_selection_visuals()
        self.update_status(f"Выделено: {len(self.selected_nodes)} узлов, {len(self.selected_edges)} рёбер")

    def clear_multi_select(self):
        self.selected_nodes.clear()
        self.selected_edges.clear()
        self._update_selection_visuals()

    def select_all(self):
        self.selected_nodes = set(self.nodes.keys())
        self.selected_edges = set(self.edges)
        self._update_selection_visuals()
        self.update_status(f"Выделено всё: {len(self.selected_nodes)} узлов, {len(self.selected_edges)} рёбер")

    def _update_selection_visuals(self):
        """Обновить визуальные подсветки multi-select."""
        for item in self._selection_highlights.values():
            self.scene.removeItem(item)
        self._selection_highlights.clear()

        for item in self._edge_selection_highlights.values():
            self.scene.removeItem(item)
        self._edge_selection_highlights.clear()

        for node_id in self.selected_nodes:
            if node_id not in self.nodes:
                continue
            node = self.nodes[node_id]
            cx, cy = node['centroid'][1], node['centroid'][0]
            r = self.CLICK_THRESHOLD - 2
            ring = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
            ring.setPen(QPen(self.COLOR_SELECTION, 2))
            ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ring.setZValue(7)
            self.scene.addItem(ring)
            self._selection_highlights[node_id] = ring

        for edge_key in self.selected_edges:
            edge_data = self.model.find_edge_data(edge_key)
            if not edge_data:
                continue
            path = self._build_edge_path(
                edge_data.get('source_point'),
                edge_data.get('waypoints', []),
                edge_data.get('target_point'))
            # Толщину подсветки берём от фактической толщины ребра, чтобы
            # обводка была видна и поверх «толстых» кастомных рёбер.
            base_w = edge_data.get('render_width') or self.EDGE_WIDTH
            sel_pen = QPen(self.COLOR_SELECTION, float(base_w) + 5,
                           Qt.PenStyle.DashLine)
            highlight = QGraphicsPathItem(path)
            highlight.setPen(sel_pen)
            highlight.setZValue(6)
            self.scene.addItem(highlight)
            self._edge_selection_highlights[edge_key] = highlight

    def _start_rubber_band(self, x: float, y: float):
        self._rb_start_x = x
        self._rb_start_y = y
        self._rb_active = True
        self._rubber_band = QGraphicsRectItem(x, y, 0, 0)
        self._rubber_band.setPen(QPen(self.COLOR_SELECTION, 1, Qt.PenStyle.DashLine))
        self._rubber_band.setBrush(QBrush(QColor(255, 255, 0, 30)))
        self._rubber_band.setZValue(20)
        self.scene.addItem(self._rubber_band)

    def _edge_only_selection(self) -> bool:
        """В режиме «Размер и цвет» обводка работает только с рёбрами."""
        return self.display_regime == "style"

    def _on_shift_lmb_press(self, x: float, y: float):
        """Shift+ЛКМ — toggle узел/ребро или начать rubber band."""
        # Режим «Размер объектов»: Shift по экземпляру класса добавляет его,
        # по пустому месту — начинает рамку (рамка добавит экземпляры класса).
        if self._current_mode == "resize_objects":
            nid = self.find_node_at(x, y)
            if nid and self.nodes.get(nid, {}).get('class_name') == self._resize_class \
                    and self.nodes[nid].get('type') == 'equipment':
                self._resize_sel.add(nid)
                self._update_resize_panel()
                return
            self._start_rubber_band(x, y)
            return
        # Состояние «ОКР привязка»: Shift выделяет текст-блоки (не узлы/рёбра).
        if self.display_regime == "ocr" and hasattr(self, "_ocr_shift_press"):
            self._ocr_shift_press(x, y)
            return
        # В режиме style — только рёбра (узлы не трогаем)
        if not self._edge_only_selection():
            clicked = self.find_node_at(x, y)
            if clicked:
                self.toggle_select_node(clicked)
                return
        # Клик на ребро → toggle selection
        edge_key, _ = self.find_nearest_edge(x, y)
        if edge_key:
            self.toggle_select_edge(edge_key)
            return
        # Пустое место → rubber band
        self._start_rubber_band(x, y)

    def _on_shift_lmb_move(self, x: float, y: float):
        """Shift+ПКМ — обновить rubber band."""
        self._update_rubber_band(x, y)

    def _on_shift_lmb_release(self, x: float, y: float, event):
        """Shift+ЛКМ — завершить rubber band (добавляет к существующему выделению)."""
        self._rubber_band_select(self._rb_start_x, self._rb_start_y, x, y, extend=True)
        if self._rubber_band:
            self.scene.removeItem(self._rubber_band)
            self._rubber_band = None
        self._rb_active = False

    def _update_rubber_band(self, x: float, y: float):
        if self._rubber_band:
            rx = min(self._rb_start_x, x)
            ry = min(self._rb_start_y, y)
            rw = abs(x - self._rb_start_x)
            rh = abs(y - self._rb_start_y)
            self._rubber_band.setRect(rx, ry, rw, rh)

    def _finish_rubber_band(self, x: float, y: float, event):
        extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) if event else False
        self._rubber_band_select(self._rb_start_x, self._rb_start_y, x, y, extend)
        if self._rubber_band:
            self.scene.removeItem(self._rubber_band)
            self._rubber_band = None
        self._rb_active = False

    def _rubber_band_select(self, x1, y1, x2, y2, extend=False):
        rx, ry = min(x1, x2), min(y1, y2)
        rw, rh = abs(x2 - x1), abs(y2 - y1)

        # Режим «Размер объектов»: рамка добавляет в набор только экземпляры
        # выбранного класса, чьи центроиды попали внутрь.
        if self._current_mode == "resize_objects":
            for nid in self._instances_of_class(self._resize_class):
                node = self.nodes.get(nid)
                if not node:
                    continue
                cx, cy = node['centroid'][1], node['centroid'][0]
                if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                    self._resize_sel.add(nid)
            self._update_resize_panel()
            return

        # Состояние «ОКР привязка»: рамка выделяет текст-блоки.
        if self.display_regime == "ocr" and hasattr(self, "_ocr_rubber_select"):
            self._ocr_rubber_select(x1, y1, x2, y2, extend)
            return

        if not extend:
            self.selected_nodes.clear()
            self.selected_edges.clear()

        edge_only = self._edge_only_selection()

        # В режиме «Размер и цвет» обводка выделяет только рёбра, не узлы.
        if not edge_only:
            for node_id, node in self.nodes.items():
                cx, cy = node['centroid'][1], node['centroid'][0]
                if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                    self.selected_nodes.add(node_id)

        rect_bbox = [rx, ry, rx + rw, ry + rh]
        for edge in self.edges_data:
            sp, tp = edge.get('source_point'), edge.get('target_point')
            if not sp or not tp:
                continue
            key = self.model.edge_key(edge['source'], edge['target'])
            if edge_only:
                # Рамка «касается» ребра: любой сегмент пересекает рамку
                # или конец внутри неё.
                hit = False
                for (ax, ay), (bx, by) in self._get_edge_segments(edge):
                    if ((rx <= ax <= rx + rw and ry <= ay <= ry + rh) or
                            (rx <= bx <= rx + rw and ry <= by <= ry + rh) or
                            segment_intersects_bbox(ax, ay, bx, by, rect_bbox)):
                        hit = True
                        break
                if hit:
                    self.selected_edges.add(key)
            else:
                sx, sy = sp[1], sp[0]
                tx, ty = tp[1], tp[0]
                if (rx <= sx <= rx + rw and ry <= sy <= ry + rh and
                        rx <= tx <= rx + rw and ry <= ty <= ry + rh):
                    self.selected_edges.add(key)

        self._update_selection_visuals()
        self.update_status(f"Выделено: {len(self.selected_nodes)} узлов, {len(self.selected_edges)} рёбер")

    # =================================================================
    # Изменение ребра: цвет / размер
    # =================================================================

    def set_edge_brush_color(self, color: QColor):
        """Установить текущий цвет кисти рёбер (из палитры)."""
        self.edge_brush_color = QColor(color)

    def set_edge_brush_size(self, size: int):
        """Установить текущий размер кисти рёбер и уведомить тулбар."""
        self.edge_brush_size = max(1, int(size))
        if self.edge_size_callback:
            self.edge_size_callback(self.edge_brush_size)

    def _edges_to_style(self, edge_key: tuple) -> list:
        """Какие рёбра менять при клике по edge_key.

        Если ребро входит в обводку (selected_edges) — вся пачка,
        иначе — только это ребро.
        """
        if edge_key in self.selected_edges:
            return list(self.selected_edges)
        return [edge_key]

    def apply_edge_style_at(self, edge_key: tuple, kind: str):
        """Применить текущий цвет ('color') или размер ('size') к ребру/обводке."""
        keys = self._edges_to_style(edge_key)
        if not keys:
            return

        desc = "Цвет рёбер" if kind == "color" else "Размер рёбер"
        cmd = SetEdgeStyleCommand(self.model, self._redraw_all, description=desc)
        cmd.execute()  # snapshot before

        changed = 0
        for k in keys:
            edge_data = self.model.find_edge_data(k)
            if not edge_data:
                continue
            if kind == "color":
                edge_data['render_color'] = self.edge_brush_color.name()
            else:
                edge_data['render_width'] = int(self.edge_brush_size)
            changed += 1

        cmd.finalize()  # snapshot after
        self.undo_mgr.push_executed(cmd)

        self._redraw_all()
        # _redraw_all не восстанавливает подсветку обводки — вернуть её
        self._update_selection_visuals()

        if kind == "color":
            self.update_status(
                f"Цвет {self.edge_brush_color.name()} применён к {changed} рёбрам"
            )
        else:
            self.update_status(
                f"Размер {self.edge_brush_size} применён к {changed} рёбрам"
            )

    def update_edge_style_preview(self, mouse_x: float, mouse_y: float):
        """Превью ребра под курсором кистью текущего режима (не оранжевым).

        Цвет → ребро показывается выбранным цветом;
        размер → ребро показывается белым выбранной толщины.
        """
        if self.edge_highlight:
            self.scene.removeItem(self.edge_highlight)
            self.edge_highlight = None

        edge_key, _ = self.find_nearest_edge(mouse_x, mouse_y, threshold=20.0)
        if not edge_key:
            return
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return
        path = self._build_edge_path(
            edge_data.get('source_point'),
            edge_data.get('waypoints', []),
            edge_data.get('target_point'))

        if self._current_mode == "edit_edge_size":
            pen = QPen(QColor(255, 255, 255), float(self.edge_brush_size))
        else:  # edit_edge_color
            pen = QPen(QColor(self.edge_brush_color), self.EDGE_WIDTH + 2)

        self.edge_highlight = QGraphicsPathItem(path)
        self.edge_highlight.setPen(pen)
        self.edge_highlight.setZValue(10)
        self.scene.addItem(self.edge_highlight)

    def wheelEvent(self, event):
        """Ctrl+колесо в режиме «Размер ребра» — менять размер кисти, не зумить."""
        if (self._current_mode == "edit_edge_size"
                and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            step = 1 if event.angleDelta().y() > 0 else -1
            self.set_edge_brush_size(self.edge_brush_size + step)
            self.update_status(f"Размер ребра: {self.edge_brush_size}")
            event.accept()
            return
        super().wheelEvent(event)

    def _ctrl_right_click_delete(self, x: float, y: float):
        """Ctrl+ПКМ — удалить под курсором. Если элемент выделен → удалить всю пачку.

        В режимах изменения ребра Ctrl+ПКМ НЕ удаляет, а убирает ребро из обводки.
        """
        # Правка полигона: Ctrl+ПКМ по вершине удаляет вершину (минимум 3).
        # Удаление узла/ребра в этом режиме ЗАБЛОКИРОВАНО — чтобы случайно не
        # снести весь узел или ребро при работе с точками полигона.
        if self._poly_edit_node and self._poly_overlay:
            vtx = self._poly_overlay.find_vertex_at(x, y)
            if vtx is not None:
                if self._poly_overlay.vertex_count <= 3:
                    self.update_status("Минимум 3 вершины")
                    return
                before = self.model.snapshot()
                self._poly_overlay.remove_vertex(vtx)
                self._poly_write_node()
                self._poly_push(before, "Удалить вершину")
                self.update_status(
                    f"Удалена вершина (осталось {self._poly_overlay.vertex_count})"
                )
            return

        # Режим «Размер объектов»: Ctrl+ПКМ убирает экземпляр из набора (не удаляет узел).
        if self._current_mode == "resize_objects":
            nid = self.find_node_at(x, y)
            if nid and nid in self._resize_sel:
                self._resize_sel.discard(nid)
                self._update_resize_panel()
                self.update_status(f"Убран {nid} (в наборе {len(self._resize_sel)})")
            else:
                self.update_status("Под курсором нет экземпляра из набора")
            return

        # Состояние «ОКР привязка»: Ctrl+ПКМ по блоку — отвязать (если привязан)
        # или удалить блок.
        if self.display_regime == "ocr" and hasattr(self, "_ocr_block_at"):
            bid = self._ocr_block_at(x, y)
            if bid is not None:
                if getattr(self, "_selected_ocr", None) and bid in self._selected_ocr:
                    self._delete_selected_ocr_blocks()
                    self.update_status("Выделенные блоки удалены")
                elif self.model.find_binding(bid) is not None:
                    self.unbind_ocr_block(bid)
                    self.update_status("Блок отвязан")
                else:
                    self.delete_ocr_block(bid)
                    self.update_status("Блок удалён")
                return

        if self._current_mode in ("edit_edge_color", "edit_edge_size"):
            edge_key, _ = self.find_nearest_edge(x, y, threshold=20.0)
            if edge_key and edge_key in self.selected_edges:
                self.selected_edges.discard(edge_key)
                self._update_selection_visuals()
                self.update_status(
                    f"Ребро убрано из обводки (осталось {len(self.selected_edges)})"
                )
            else:
                self.update_status("Нет обведённого ребра под курсором")
            return

        node_id = self.find_node_at(x, y)
        if node_id:
            if node_id in self.selected_nodes:
                # Удалить всю выделенную пачку
                self.batch_delete()
                return
            # Удалить только этот узел
            snap_cmd = AutoFixCommand(self.model, self._redraw_all)
            snap_cmd.description = f"Удалить узел {node_id}"
            snap_cmd.execute()
            self._delete_node_internal(node_id)
            snap_cmd.finalize()
            self.undo_mgr.push_executed(snap_cmd)
            self.model.rebuild_edge_data_index()
            self.update_statistics()
            self.update_status(f"Удалён узел {node_id}")
            return
        edge_key, _ = self.find_nearest_edge(x, y)
        if edge_key:
            if edge_key in self.selected_edges:
                # Удалить всю выделенную пачку
                self.batch_delete()
                return
            # Удалить только это ребро
            snap_cmd = AutoFixCommand(self.model, self._redraw_all)
            snap_cmd.description = f"Удалить ребро {edge_key[0]}—{edge_key[1]}"
            snap_cmd.execute()
            self._remove_edge_internal(edge_key)
            snap_cmd.finalize()
            self.undo_mgr.push_executed(snap_cmd)
            self.model.rebuild_edge_data_index()
            self.update_statistics()
            self.update_status(f"Удалено ребро {edge_key[0]} — {edge_key[1]}")
            return
        self.update_status("Нет узла или ребра под курсором")

    def batch_delete(self):
        """Удалить все выделенные узлы и рёбра (один snapshot undo)."""
        if not self.selected_nodes and not self.selected_edges:
            self.update_status("Нечего удалять")
            return

        snap_cmd = AutoFixCommand(self.model, self._redraw_all)
        snap_cmd.description = "Удалить выделенное"
        snap_cmd.execute()

        count_nodes = len(self.selected_nodes)
        count_edges = len(self.selected_edges)

        for edge_key in list(self.selected_edges):
            self._remove_edge_internal(edge_key)

        for node_id in list(self.selected_nodes):
            self._delete_node_internal(node_id)

        snap_cmd.finalize()
        self.undo_mgr.push_executed(snap_cmd)

        self.selected_nodes.clear()
        self.selected_edges.clear()
        self._update_selection_visuals()
        self.model.rebuild_edge_data_index()
        self.update_statistics()
        self.update_status(f"Удалено: {count_nodes} узлов, {count_edges} рёбер")

    def _remove_edge_internal(self, edge_key: tuple):
        """Удалить ребро без undo."""
        self.model.remove_edge(edge_key)
        self.remove_edge_item(edge_key)
        for node_id in edge_key:
            if node_id in self.nodes:
                self.update_node_color(node_id)

    def _delete_node_internal(self, node_id: str):
        """Удалить узел + каскадные рёбра без undo."""
        if node_id not in self.nodes:
            return
        for key in list(self.edges):
            if node_id in key:
                self._remove_edge_internal(key)
        self.model.remove_node(node_id)
        self.remove_node_items(node_id)

    # =================================================================
    # Drag
    # =================================================================

    def start_drag_node(self, node_id: str):
        """Начать перетаскивание."""
        if node_id not in self.nodes:
            return
        node_data = self.nodes[node_id]
        self.dragging_node = node_id
        self.drag_start_centroid = node_data['centroid'].copy()
        self.drag_start_bbox = (node_data.get('bbox') or []).copy()
        self.drag_start_segmentation = (node_data.get('segmentation') or []).copy()
        self._drag_prev_x = node_data['centroid'][1]
        self._drag_prev_y = node_data['centroid'][0]

        self._batch_drag = (node_id in self.selected_nodes and len(self.selected_nodes) > 1)

        if self._batch_drag:
            self._batch_snap_cmd = BatchDragCommand(self.model, self._redraw_all)
            self._batch_snap_cmd.execute()

            # Precompute edge sets for fast batch drag
            sel = self.selected_nodes
            self._batch_internal_edges = []  # both ends in selection
            self._batch_boundary_edges = []  # one end in selection
            seen = set()
            for e in self.edges_data:
                eid = e.get('id', '')
                if eid in seen:
                    continue
                sid, tid = e['source'], e['target']
                if sid not in sel and tid not in sel:
                    continue
                seen.add(eid)
                if sid in sel and tid in sel:
                    self._batch_internal_edges.append(e)
                else:
                    self._batch_boundary_edges.append(e)
        else:
            self.drag_start_edge_points = {}
            for key in self.model.get_connected_edges(node_id):
                edge_data = self.model.find_edge_data(key)
                if edge_data:
                    self.drag_start_edge_points[key] = {
                        'source_point': (edge_data.get('source_point') or []).copy(),
                        'target_point': (edge_data.get('target_point') or []).copy(),
                    }

    def drag_node_to(self, x: float, y: float):
        """Переместить узел/группу."""
        if not self.dragging_node:
            return
        if self._batch_drag:
            dx = x - self._drag_prev_x
            dy = y - self._drag_prev_y
            self._drag_prev_x = x
            self._drag_prev_y = y
            if abs(dx) < 0.1 and abs(dy) < 0.1:
                return
            self._batch_move_fast(dx, dy)
        else:
            self._move_single_node(self.dragging_node, x, y)
        self._update_selection_visuals()

    def _batch_move_fast(self, dx: float, dy: float):
        """Быстрое перемещение группы узлов — без routing.

        Стратегия для рёбер:
        - Internal (оба конца в выделении): сдвигаем source_point/target_point/waypoints на dx,dy
        - Boundary (один конец в выделении): простой пересчёт connection point (bbox midpoint),
          без distribute_connection_points и без route_edge_v2.
        """
        sel = self.selected_nodes

        # Pass 1: сдвинуть все выделенные узлы (geometry + visuals)
        for nid in sel:
            node = self.nodes.get(nid)
            if not node:
                continue
            old_cx, old_cy = node['centroid'][1], node['centroid'][0]
            nx, ny = old_cx + dx, old_cy + dy
            node['centroid'] = [ny, nx]

            node_type = node.get('type', 'connector')
            if node_type == 'equipment':
                bbox = node.get('bbox')
                if bbox and len(bbox) == 4:
                    node['bbox'] = [bbox[0]+dx, bbox[1]+dy, bbox[2]+dx, bbox[3]+dy]
                    if nid in self.bbox_items:
                        nb = node['bbox']
                        self.bbox_items[nid].setRect(nb[0], nb[1], nb[2]-nb[0], nb[3]-nb[1])
                seg = node.get('segmentation')
                if seg and isinstance(seg, list) and len(seg) >= 6:
                    for i in range(0, len(seg), 2):
                        seg[i] += dx
                        seg[i+1] += dy
                    if nid in self.polygon_items:
                        path = QPainterPath()
                        path.moveTo(seg[0], seg[1])
                        for i in range(2, len(seg), 2):
                            path.lineTo(seg[i], seg[i+1])
                        path.closeSubpath()
                        self.polygon_items[nid].setPath(path)

            if nid in self.node_items:
                r = self.EQUIPMENT_MARKER_RADIUS if node_type == 'equipment' else self.CONNECTOR_MARKER_RADIUS
                self.node_items[nid].setRect(nx-r, ny-r, r*2, r*2)

        # Pass 2: обновить рёбра (precomputed в start_drag_node)
        for e in self._batch_internal_edges:
            # Internal edge: shift everything
            sp = e.get('source_point')
            tp = e.get('target_point')
            if sp:
                e['source_point'] = [sp[0]+dy, sp[1]+dx]
            if tp:
                e['target_point'] = [tp[0]+dy, tp[1]+dx]
            for wp in e.get('waypoints', []):
                wp[0] += dy
                wp[1] += dx
            edge_key = self.model.edge_key(e['source'], e['target'])
            self._update_edge_path(edge_key)

        for e in self._batch_boundary_edges:
            # Boundary edge: lightweight recalc
            self._recalculate_edge_fast(e)
            edge_key = self.model.edge_key(e['source'], e['target'])
            self._update_edge_path(edge_key)

    def _recalculate_edge_fast(self, edge_data: dict):
        """Быстрый пересчёт ребра — без routing, без distribute.

        Для drag: пересчитывает только connection points (bbox side midpoint).
        Waypoints очищаются (рёбра рисуются прямыми).
        """
        src_id, tgt_id = edge_data['source'], edge_data['target']
        src = self.nodes.get(src_id)
        tgt = self.nodes.get(tgt_id)
        if not src or not tgt:
            return

        src_cx, src_cy = src['centroid'][1], src['centroid'][0]
        tgt_cx, tgt_cy = tgt['centroid'][1], tgt['centroid'][0]

        src_bbox = self._get_node_bbox(src_id)
        tgt_bbox = self._get_node_bbox(tgt_id)

        src_side = bbox_exit_side(src_bbox, src_cx, src_cy, tgt_cx, tgt_cy)
        tgt_side = bbox_exit_side(tgt_bbox, tgt_cx, tgt_cy, src_cx, src_cy)

        sx, sy = bbox_side_midpoint(src_bbox, src_side)
        tx, ty = bbox_side_midpoint(tgt_bbox, tgt_side)

        edge_data['source_point'] = [sy, sx]
        edge_data['target_point'] = [ty, tx]
        edge_data['waypoints'] = []

    def end_drag_node(self):
        """Завершить перетаскивание."""
        if not self.dragging_node:
            return
        node_id = self.dragging_node

        if self._batch_drag:
            # Пересчитать boundary edges нормально
            self._batch_recalculate_boundary_edges()

            self._batch_snap_cmd.finalize()
            self.undo_mgr.push_executed(self._batch_snap_cmd)
            self._batch_snap_cmd = None
        else:
            cmd = DragNodeCommand(
                self.model, self, node_id,
                self.drag_start_centroid, self.drag_start_bbox,
                self.drag_start_segmentation, self.drag_start_edge_points,
            )
            cmd.capture_new_state()
            self.undo_mgr.push_executed(cmd)

        self.dragging_node = None
        self._batch_drag = False
        self._batch_internal_edges = []
        self._batch_boundary_edges = []
        self.drag_start_centroid = None
        self.drag_start_bbox = []
        self.drag_start_segmentation = []
        self.drag_start_edge_points = {}

    def _batch_recalculate_boundary_edges(self):
        """Полный пересчёт boundary edges после завершения batch drag."""
        for e in self._batch_boundary_edges:
            self._recalculate_edge(e, keep_sides=False)

    def _move_single_node(self, node_id: str, x: float, y: float):
        """Переместить один узел и пересчитать рёбра.

        Из graph_editor.py:3650-3710.
        """
        node_data = self.nodes.get(node_id)
        if not node_data:
            return

        old_cx, old_cy = node_data['centroid'][1], node_data['centroid'][0]
        dx = x - old_cx
        dy = y - old_cy

        node_data['centroid'] = [y, x]

        node_type = node_data.get('type', 'connector')
        if node_type == 'equipment':
            bbox = node_data.get('bbox')
            if bbox and len(bbox) == 4:
                node_data['bbox'] = [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy]
                if node_id in self.bbox_items:
                    new_bbox = node_data['bbox']
                    self.bbox_items[node_id].setRect(
                        new_bbox[0], new_bbox[1],
                        new_bbox[2] - new_bbox[0], new_bbox[3] - new_bbox[1])

            seg = node_data.get('segmentation')
            if seg and isinstance(seg, list) and len(seg) >= 6:
                for i in range(0, len(seg), 2):
                    seg[i] += dx
                    seg[i + 1] += dy
                if node_id in self.polygon_items:
                    path = QPainterPath()
                    path.moveTo(seg[0], seg[1])
                    for i in range(2, len(seg), 2):
                        path.lineTo(seg[i], seg[i + 1])
                    path.closeSubpath()
                    self.polygon_items[node_id].setPath(path)

        if node_id in self.node_items:
            r = self.EQUIPMENT_MARKER_RADIUS if node_type == 'equipment' else self.CONNECTOR_MARKER_RADIUS
            self.node_items[node_id].setRect(x - r, y - r, r * 2, r * 2)

        # Пересчитать рёбра — двухпроходный
        affected = [e for e in self.edges_data if e['source'] == node_id or e['target'] == node_id]

        for e in affected:
            if e.get('_manual_route'):
                continue  # ручные точки прикрепления не пересчитываем
            sid, tid = e['source'], e['target']
            s, t = self.nodes[sid], self.nodes[tid]
            s_cx, s_cy = s['centroid'][1], s['centroid'][0]
            t_cx, t_cy = t['centroid'][1], t['centroid'][0]
            s_bbox = self._get_node_bbox(sid)
            t_bbox = self._get_node_bbox(tid)
            e['_src_side'] = bbox_exit_side(s_bbox, s_cx, s_cy, t_cx, t_cy)
            e['_tgt_side'] = bbox_exit_side(t_bbox, t_cx, t_cy, s_cx, s_cy)

        for e in affected:
            self._recalculate_edge(e, moving_node_id=node_id, keep_sides=True)

    # =================================================================
    # Waypoints
    # =================================================================

    def _show_waypoint_markers(self):
        self._hide_waypoint_markers()
        for edge in self.edges_data:
            waypoints = edge.get('waypoints', [])
            if not waypoints:
                continue
            key = self.model.edge_key(edge['source'], edge['target'])
            markers = []
            for wp in waypoints:
                wx, wy = wp[1], wp[0]
                size = 6
                rect = QGraphicsRectItem(wx - size / 2, wy - size / 2, size, size)
                rect.setPen(QPen(QColor(40, 40, 40), 1.5))
                rect.setBrush(QBrush(QColor(255, 255, 255, 220)))
                rect.setZValue(5)
                self.scene.addItem(rect)
                markers.append(rect)
            self.waypoint_markers[key] = markers

    def _hide_waypoint_markers(self):
        for markers in self.waypoint_markers.values():
            for m in markers:
                self.scene.removeItem(m)
        self.waypoint_markers.clear()

    def _refresh_waypoint_markers_for_edge(self, edge_key: tuple):
        old_markers = self.waypoint_markers.pop(edge_key, [])
        for m in old_markers:
            self.scene.removeItem(m)
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return
        waypoints = edge_data.get('waypoints', [])
        if not waypoints:
            return
        markers = []
        for wp in waypoints:
            wx, wy = wp[1], wp[0]
            size = 6
            rect = QGraphicsRectItem(wx - size / 2, wy - size / 2, size, size)
            rect.setPen(QPen(QColor(40, 40, 40), 1.5))
            rect.setBrush(QBrush(QColor(255, 255, 255, 220)))
            rect.setZValue(5)
            self.scene.addItem(rect)
            markers.append(rect)
        self.waypoint_markers[edge_key] = markers

    def find_waypoint_at(self, x: float, y: float, threshold: float = 10.0):
        best = None
        best_dist = threshold
        for edge in self.edges_data:
            waypoints = edge.get('waypoints', [])
            if not waypoints:
                continue
            key = self.model.edge_key(edge['source'], edge['target'])
            for i, wp in enumerate(waypoints):
                wx, wy = wp[1], wp[0]
                dist = ((x - wx) ** 2 + (y - wy) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best = (key, i)
        return best

    def _start_waypoint_drag(self, wp_hit):
        self.dragging_waypoint = wp_hit
        edge_data = self.model.find_edge_data(wp_hit[0])
        if edge_data:
            self.dragging_wp_start = edge_data['waypoints'][wp_hit[1]].copy()

    def _drag_waypoint_to(self, x: float, y: float):
        if not self.dragging_waypoint:
            return
        edge_key, wp_idx = self.dragging_waypoint
        edge_data = self.model.find_edge_data(edge_key)
        if edge_data:
            snapped_x, snapped_y = self.snap_to_wp_grid(x, y)
            edge_data['waypoints'][wp_idx] = [snapped_y, snapped_x]
            self._update_edge_path(edge_key)
            markers = self.waypoint_markers.get(edge_key, [])
            if wp_idx < len(markers):
                size = 6
                markers[wp_idx].setRect(snapped_x - size / 2, snapped_y - size / 2, size, size)

    def _end_waypoint_drag(self):
        if not self.dragging_waypoint:
            return
        edge_key, wp_idx = self.dragging_waypoint
        edge_data = self.model.find_edge_data(edge_key)
        if edge_data and self.dragging_wp_start:
            new_wp = edge_data['waypoints'][wp_idx].copy()
            cmd = MoveWaypointCommand(self.model, self, edge_key, wp_idx,
                                      self.dragging_wp_start, new_wp)
            self.undo_mgr.push_executed(cmd)
        self.dragging_waypoint = None
        self.dragging_wp_start = None
        self.update_status("Waypoint перемещён")

    def _add_waypoint_on_segment(self, edge_key: tuple, segment_index: int, x: float, y: float):
        snapped_x, snapped_y = self.snap_to_grid(x, y)
        new_wp = [snapped_y, snapped_x]
        cmd = AddWaypointCommand(self.model, self, edge_key, segment_index, new_wp)
        self.undo_mgr.execute(cmd)
        self._refresh_waypoint_markers_for_edge(edge_key)
        self.update_status(f"Waypoint добавлен на {edge_key}")

    def _delete_waypoint(self, edge_key: tuple, wp_idx: int):
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return
        waypoints = edge_data.get('waypoints', [])
        if wp_idx >= len(waypoints):
            return
        old_wp = waypoints[wp_idx].copy()
        cmd = DeleteWaypointCommand(self.model, self, edge_key, wp_idx, old_wp)
        self.undo_mgr.execute(cmd)
        self._refresh_waypoint_markers_for_edge(edge_key)

    def _cycle_node_sides(self, node_id: str):
        """Ctrl+Click на узел: цикл стороны."""
        sides_cycle = ['top', 'right', 'bottom', 'left']
        affected = []
        for edge in self.edges_data:
            key = self.model.edge_key(edge['source'], edge['target'])
            if edge['source'] == node_id:
                affected.append((key, edge, 'source'))
            elif edge['target'] == node_id:
                affected.append((key, edge, 'target'))

        if not affected:
            self.update_status("У узла нет рёбер")
            return

        snap_cmd = AutoFixCommand(self.model, self._redraw_all)
        snap_cmd.description = "Цикл стороны"
        snap_cmd.execute()

        for key, edge_data, endpoint in affected:
            side_key = '_src_side' if endpoint == 'source' else '_tgt_side'
            current_side = edge_data.get(side_key, 'right')
            idx = sides_cycle.index(current_side) if current_side in sides_cycle else 0
            new_side = sides_cycle[(idx + 1) % 4]
            edge_data[side_key] = new_side
            edge_data.pop('_manual_route', None)  # явный пересчёт сбрасывает ручной режим
            self._recalculate_edge(edge_data, keep_sides=True)
            self._refresh_waypoint_markers_for_edge(key)

        snap_cmd.finalize()
        self.undo_mgr.push_executed(snap_cmd)
        self._refresh_endpoint_markers()
        self.update_status(f"Сторона узла {node_id} переключена ({len(affected)} рёбер)")

    def _auto_l_route_edge(self, edge_key: tuple):
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return
        old_wp = [wp.copy() for wp in edge_data.get('waypoints', [])]
        edge_data.pop('_manual_route', None)  # явный авто-роутинг сбрасывает ручной режим
        self._recalculate_edge(edge_data)

        cmd = AutoLRouteCommand(self.model, self, edge_key, old_wp)
        cmd.capture_new_waypoints()
        self.undo_mgr.push_executed(cmd)
        self._refresh_waypoint_markers_for_edge(edge_key)
        self.update_status(f"L-route: {edge_key}")

    # ── Endpoint markers ──

    def _show_endpoint_markers(self):
        self._hide_endpoint_markers()
        EP_SIZE = 10
        for edge in self.edges_data:
            key = self.model.edge_key(edge['source'], edge['target'])
            sp, tp = edge.get('source_point'), edge.get('target_point')
            markers = []
            for point, label in [(sp, 'source'), (tp, 'target')]:
                if point:
                    px, py = point[1], point[0]
                    rect = QGraphicsRectItem(px - EP_SIZE / 2, py - EP_SIZE / 2, EP_SIZE, EP_SIZE)
                    rect.setPen(QPen(QColor(0, 188, 212), 2))
                    rect.setBrush(QBrush(QColor(0, 188, 212, 120)))
                    rect.setZValue(8)
                    self.scene.addItem(rect)
                    markers.append((label, rect))
            if markers:
                self._endpoint_markers[key] = markers

    def _hide_endpoint_markers(self):
        for markers in self._endpoint_markers.values():
            for _, item in markers:
                self.scene.removeItem(item)
        self._endpoint_markers.clear()

    def _refresh_endpoint_markers(self):
        if self._current_mode == "edit_waypoint":
            self._show_endpoint_markers()

    def _find_endpoint_at(self, x, y, threshold=12.0):
        best = None
        best_dist = threshold
        for edge in self.edges_data:
            key = self.model.edge_key(edge['source'], edge['target'])
            sp, tp = edge.get('source_point'), edge.get('target_point')
            if sp:
                d = ((x - sp[1]) ** 2 + (y - sp[0]) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = (key, 'source')
            if tp:
                d = ((x - tp[1]) ** 2 + (y - tp[0]) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = (key, 'target')
        return best

    def _start_endpoint_drag(self, ep_hit):
        edge_key, endpoint = ep_hit
        edge_data = self.model.find_edge_data(edge_key)
        if edge_data:
            side_key = '_src_side' if endpoint == 'source' else '_tgt_side'
            self._dragging_endpoint = ep_hit
            self._dragging_ep_start_side = edge_data.get(side_key, 'right')
            snap_cmd = AutoFixCommand(self.model, self._redraw_all)
            snap_cmd.description = "Перемещение endpoint"
            snap_cmd.execute()
            self._ep_snap_cmd = snap_cmd

    def _project_to_node_border(self, node_id: str, x: float, y: float):
        """Спроецировать точку (x, y) на периметр узла (полигон, иначе bbox).

        Используется для свободного перемещения точки прикрепления вдоль
        границы узла, не ограничиваясь центрами сторон.
        """
        node = self.nodes.get(node_id)
        geom = get_node_geometry(node) if node else None
        if geom and geom['type'] == 'polygon':
            return project_point_to_polygon_border(geom['data'], x, y)
        bbox = self._get_node_bbox(node_id)
        return project_point_to_bbox_border(bbox, x, y)

    def _drag_endpoint_to(self, x: float, y: float):
        """Ручное перемещение точки прикрепления.

        Точка свободно скользит по периметру узла (bbox/полигон). Линия НЕ
        пересчитывается к ортогональности: промежуточные waypoints убираются,
        остаётся прямой отрезок до другого конца. Ставится флаг _manual_route,
        чтобы последующие пересчёты не возвращали точку в центр стороны.
        """
        if not self._dragging_endpoint:
            return
        edge_key, endpoint = self._dragging_endpoint
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return

        node_id = edge_data['source'] if endpoint == 'source' else edge_data['target']
        px, py = self._project_to_node_border(node_id, x, y)

        point_key = 'source_point' if endpoint == 'source' else 'target_point'
        side_key = '_src_side' if endpoint == 'source' else '_tgt_side'

        edge_data[point_key] = [py, px]  # формат [y, x]
        # сторону держим в синхроне для прочей логики, но маршрут не считаем
        edge_data[side_key] = closest_bbox_side(self._get_node_bbox(node_id), px, py)
        edge_data['waypoints'] = []          # ручной режим → прямая линия
        edge_data['_manual_route'] = True

        key = self.model.edge_key(edge_data['source'], edge_data['target'])
        self._update_edge_path(key)
        self.edge_perp_scores[key] = {'is_good': True, 'score': 1.0, 'source_angle': 0}
        self._refresh_waypoint_markers_for_edge(key)
        self._refresh_endpoint_markers()

    def _end_endpoint_drag(self):
        if hasattr(self, '_ep_snap_cmd') and self._ep_snap_cmd:
            self._ep_snap_cmd.finalize()
            self.undo_mgr.push_executed(self._ep_snap_cmd)
            self._ep_snap_cmd = None
        self._dragging_endpoint = None
        self._dragging_ep_start_side = None
        self.update_status("Точка прикрепления перемещена")

    # =================================================================
    # Grid
    # =================================================================

    def toggle_grid(self):
        self.grid_visible = not self.grid_visible
        if self.grid_visible:
            self._draw_grid()
        else:
            self._remove_grid()

    def snap_to_grid(self, x: float, y: float) -> tuple[float, float]:
        gs = self.grid_size
        return (round(x / gs) * gs, round(y / gs) * gs)

    def snap_to_wp_grid(self, x: float, y: float) -> tuple[float, float]:
        gs = self.wp_snap_size
        return (round(x / gs) * gs, round(y / gs) * gs)

    def _draw_grid(self):
        self._remove_grid()
        gs = self.grid_size
        if gs <= 0:
            return
        pen = QPen(QColor(255, 255, 255, 38), 0.5)
        for gx in range(0, self.img_width + 1, gs):
            line = self.scene.addLine(gx, 0, gx, self.img_height, pen)
            line.setZValue(0.5)
            self._grid_items.append(line)
        for gy in range(0, self.img_height + 1, gs):
            line = self.scene.addLine(0, gy, self.img_width, gy, pen)
            line.setZValue(0.5)
            self._grid_items.append(line)

    def _remove_grid(self):
        for item in self._grid_items:
            self.scene.removeItem(item)
        self._grid_items.clear()

    def _compute_grid_size(self):
        bbox_widths = []
        for node in self.nodes.values():
            bbox = node.get('bbox')
            if bbox and len(bbox) == 4:
                w = bbox[2] - bbox[0]
                if w > 0:
                    bbox_widths.append(w)
        if bbox_widths:
            self.grid_size = max(8, int(statistics.median(bbox_widths) / 2))
        else:
            self.grid_size = 24
        self.snap_threshold = self.grid_size // 2

    # =================================================================
    # Auto-fix: global alignment optimization
    # Moving nodes is ALWAYS cheaper than L-route.
    # L-route is last resort only when constraints conflict.
    # =================================================================

    EQUIP_MAX_SHIFT = 30.0
    STRAIGHT_TOL = 3.0        # px — tolerance for "straight enough"
    CONN_MAX_SHIFT = 80.0     # px — max connector shift

    def auto_fix(self):
        """Graph-aware chain alignment: выровнять узлы по H/V цепочкам."""
        from ui.editors.undo_manager import SnapshotCommand

        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd.execute()
        cmd.description = "Auto-Fix (chains)"

        stats = auto_fix_graph(
            self.nodes,
            self.edges_data,
            equip_max_shift=self.EQUIP_MAX_SHIFT,
            conn_max_shift=self.CONN_MAX_SHIFT,
        )

        self._redraw_all()
        self.model.rebuild_edge_data_index()

        cmd.finalize()
        self.undo_mgr.push_executed(cmd)

        self.update_status(
            f"Auto-Fix: {stats['h_chains']}H + {stats['v_chains']}V цепочек, "
            f"{stats['nodes_moved']} сдвинуто ({stats['total_shift_px']:.0f}px), "
            f"{stats['edges_straightened']} прямых, "
            f"{stats['overlaps_fixed']} overlaps fixed"
        )
        self.update_statistics()

    # =================================================================
    # Режим «Размер объектов» — массовое изменение размеров класса
    # =================================================================

    @staticmethod
    def _node_geom_kind(node: dict) -> str:
        """'poly' если у узла есть полигон (segmentation), иначе 'box'."""
        seg = node.get('segmentation')
        if seg and isinstance(seg, list) and len(seg) >= 6:
            return 'poly'
        return 'box'

    def get_present_equipment_classes(self) -> list:
        """Классы equipment, реально присутствующие на схеме (отсортировано)."""
        names = {
            n.get('class_name') for n in self.nodes.values()
            if n.get('type') == 'equipment' and n.get('class_name')
        }
        return sorted(names)

    def _instances_of_class(self, name: str | None) -> list:
        """node_id всех equipment-экземпляров класса name."""
        if not name:
            return []
        return [
            nid for nid, n in self.nodes.items()
            if n.get('type') == 'equipment' and n.get('class_name') == name
        ]

    # ── вход/выход в режим ──

    def _enter_resize_objects(self):
        classes = self.get_present_equipment_classes()
        if self._resize_class not in classes:
            self._resize_class = classes[0] if classes else None
        if callable(self.resize_panel_classes_cb):
            self.resize_panel_classes_cb(classes, self._resize_class)
        if callable(self.resize_panel_show_cb):
            self.resize_panel_show_cb(True)
        if self._resize_class:
            self.resize_select_all()
        else:
            self._update_resize_panel()
            self.update_status("На схеме нет узлов оборудования")

    def _exit_resize_objects(self):
        self._clear_resize_frames()
        self._resize_sel = set()
        if callable(self.resize_panel_show_cb):
            self.resize_panel_show_cb(False)

    # ── управление набором (вызывается из панели / жестами) ──

    def set_resize_class(self, name: str):
        """Выбран класс в панели → по умолчанию выделить все его экземпляры."""
        self._resize_class = name
        self.resize_select_all()

    def resize_select_all(self):
        self._resize_sel = set(self._instances_of_class(self._resize_class))
        self._update_resize_panel()
        self.update_status(
            f"Класс «{self._resize_class}»: в наборе {len(self._resize_sel)}"
        )

    def resize_select_one_mode(self):
        """Сброс набора — дальше добавлять Ctrl+ЛКМ / Shift+рамкой."""
        self._resize_sel = set()
        self._update_resize_panel()
        self.update_status("Набор пуст — добавляйте Ctrl+ЛКМ или Shift+рамкой")

    def resize_filter(self, kind: str):
        """Оставить в наборе только боксы ('box') или только полигоны ('poly')."""
        keep = 'poly' if kind == 'poly' else 'box'
        self._resize_sel = {
            nid for nid in self._resize_sel
            if self.nodes.get(nid) and self._node_geom_kind(self.nodes[nid]) == keep
        }
        self._update_resize_panel()
        self.update_status(f"Оставлены только {'полигоны' if keep == 'poly' else 'боксы'}: "
                           f"{len(self._resize_sel)}")

    def _resize_handle_ctrl_click(self, x: float, y: float):
        """Ctrl+ЛКМ — добавить экземпляр класса под курсором в набор."""
        nid = self.find_node_at(x, y)
        if not nid:
            return
        node = self.nodes.get(nid, {})
        if node.get('type') == 'equipment' and node.get('class_name') == self._resize_class:
            self._resize_sel.add(nid)
            self._update_resize_panel()
            self.update_status(f"Добавлен {nid} (в наборе {len(self._resize_sel)})")
        else:
            self.update_status("Это не экземпляр выбранного класса")

    # ── геометрия набора / медианы / панель ──

    def _resize_kind(self) -> str:
        """Геометрия текущего набора: 'box' | 'poly' | 'mixed' | 'empty'."""
        if not self._resize_sel:
            return 'empty'
        has_box = has_poly = False
        for nid in self._resize_sel:
            n = self.nodes.get(nid)
            if not n:
                continue
            if self._node_geom_kind(n) == 'poly':
                has_poly = True
            else:
                has_box = True
        if has_box and has_poly:
            return 'mixed'
        return 'poly' if has_poly else 'box'

    def _resize_medians(self):
        """Медианы по боксам набора в ориентационно-нормированных осях.

        Возвращает (short, long): short → поле «Ширина», long → поле «Высота».
        Длинная/короткая ось берётся от текущих размеров каждого бокса, чтобы
        смесь вертикальных и горизонтальных экземпляров не «усреднялась» в кашу.
        """
        shorts, longs = [], []
        for nid in self._resize_sel:
            n = self.nodes.get(nid)
            if not n or self._node_geom_kind(n) == 'poly':
                continue
            bb = n.get('bbox')
            if bb and len(bb) == 4:
                w, h = bb[2] - bb[0], bb[3] - bb[1]
                shorts.append(min(w, h))
                longs.append(max(w, h))
        if not shorts:
            return (None, None)
        return (int(round(statistics.median(shorts))),
                int(round(statistics.median(longs))))

    def _update_resize_panel(self):
        """Перерисовать рамки + обновить контролы панели под текущий набор."""
        # Набор изменился → базлайн пересоберётся при следующем превью.
        self._resize_model_base = None
        self._resize_base = {}
        self._redraw_resize_frames()
        if not callable(self.resize_panel_state_cb):
            return
        kind = self._resize_kind()
        mw, mh = self._resize_medians() if kind == 'box' else (None, None)
        self.resize_panel_state_cb(kind, len(self._resize_sel), mw, mh)

    # ── живое превью размеров ──

    def _capture_resize_base(self):
        """Зафиксировать базовую геометрию набора + снимок модели (для undo)."""
        self._resize_model_base = self.model.snapshot()
        self._resize_base = {}
        for nid in self._resize_sel:
            n = self.nodes.get(nid)
            if not n:
                continue
            self._resize_base[nid] = {
                'centroid': list(n['centroid']),
                'bbox': list(n['bbox']) if n.get('bbox') else None,
                'segmentation': list(n['segmentation']) if n.get('segmentation') else None,
                'area': n.get('area'),
            }

    def _apply_sizes_from_base(self, width, height, scale, kind):
        """Применить размеры к набору, отталкиваясь от зафиксированного базлайна.

        Идемпотентно: повторные вызовы (живой бегунок) не накапливают масштаб.
        """
        for nid, base in self._resize_base.items():
            n = self.nodes.get(nid)
            if not n:
                continue
            if kind == 'box' and width and height:
                n['centroid'] = list(base['centroid'])
                if base['bbox']:
                    n['bbox'] = list(base['bbox'])
                self._resize_node_box(n, float(width), float(height))
            elif kind == 'poly' and scale and base['segmentation']:
                n['centroid'] = list(base['centroid'])
                n['segmentation'] = list(base['segmentation'])
                n['area'] = base['area']
                self._resize_node_poly(n, float(scale))

    def _refresh_node_visual(self, node_id: str):
        """Обновить визуал узла (bbox/полигон/маркер) по текущим данным."""
        node = self.nodes.get(node_id)
        if not node:
            return
        bb = node.get('bbox')
        if node_id in self.bbox_items and bb and len(bb) == 4:
            self.bbox_items[node_id].setRect(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1])
        seg = node.get('segmentation')
        if node_id in self.polygon_items and seg and len(seg) >= 6:
            path = QPainterPath()
            path.moveTo(seg[0], seg[1])
            for i in range(2, len(seg) - 1, 2):
                path.lineTo(seg[i], seg[i + 1])
            path.closeSubpath()
            self.polygon_items[node_id].setPath(path)
        if node_id in self.node_items:
            cx, cy = node['centroid'][1], node['centroid'][0]
            r = (self.EQUIPMENT_MARKER_RADIUS if node.get('type') == 'equipment'
                 else self.CONNECTOR_MARKER_RADIUS)
            self.node_items[node_id].setRect(cx - r, cy - r, r * 2, r * 2)
        # подогнать скин под новый размер (живой резайз)
        if self.show_skins and node_id in self._skin_items:
            self._update_node_skin(node_id)

    def preview_resize(self, width=None, height=None, scale=None):
        """Живое превью: визуально меняет размеры набора без перестройки связей.

        Связи/соседи пересчитываются только по кнопке «Применить» (apply_resize).
        """
        if not self._resize_sel:
            return
        kind = self._resize_kind()
        if kind in ('mixed', 'empty'):
            return
        if self._resize_model_base is None:
            self._capture_resize_base()
        self._apply_sizes_from_base(width, height, scale, kind)
        for nid in self._resize_sel:
            self._refresh_node_visual(nid)
        self._redraw_resize_frames()

    # ── жёлтые рамки ──

    def _clear_resize_frames(self):
        for it in self._resize_frames:
            try:
                self.scene.removeItem(it)
            except Exception:
                pass
        self._resize_frames.clear()

    def _redraw_resize_frames(self):
        self._clear_resize_frames()
        pen = QPen(QColor(255, 215, 0), 2.5)
        for nid in self._resize_sel:
            node = self.nodes.get(nid)
            if not node:
                continue
            seg = node.get('segmentation')
            if seg and isinstance(seg, list) and len(seg) >= 6:
                path = QPainterPath()
                path.moveTo(seg[0], seg[1])
                for i in range(2, len(seg) - 1, 2):
                    path.lineTo(seg[i], seg[i + 1])
                path.closeSubpath()
                item = QGraphicsPathItem(path)
            else:
                bb = node.get('bbox')
                if not bb or len(bb) != 4:
                    cx, cy = node['centroid'][1], node['centroid'][0]
                    r = self.EQUIPMENT_MARKER_RADIUS
                    bb = [cx - r, cy - r, cx + r, cy + r]
                item = QGraphicsRectItem(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1])
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(9)
            self.scene.addItem(item)
            self._resize_frames.append(item)

    # ── применение размеров ──

    def _resize_node_box(self, node: dict, short: float, long_: float):
        """Задать боксу размер вокруг центроида с сохранением ориентации.

        short — короткая ось (поле «Ширина»), long_ — длинная (поле «Высота»).
        Длинное значение кладётся на ту экранную ось, что у бокса сейчас длиннее:
        вертикальный/квадрат → long_ по высоте; горизонтальный → long_ по ширине.
        """
        cy, cx = node['centroid'][0], node['centroid'][1]
        bb = node.get('bbox')
        cur_w = (bb[2] - bb[0]) if (bb and len(bb) == 4) else 0
        cur_h = (bb[3] - bb[1]) if (bb and len(bb) == 4) else 0
        # Ориентация по рёбрам (Часть 1); рёбер нет → по текущему размеру.
        orient = self._node_orientation(node.get('id')) or (
            'VERTICAL' if cur_h >= cur_w else 'HORIZONTAL')
        if orient == 'HORIZONTAL':   # длинная сторона вдоль потока → по ширине
            new_w, new_h = long_, short
        else:                        # вертикальный/квадрат → длинная по высоте
            new_w, new_h = short, long_
        node['bbox'] = [cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2]
        node['area'] = new_w * new_h

    def _resize_node_poly(self, node: dict, scale: float):
        """Масштабировать полигон вокруг центроида с сохранением формы."""
        seg = node.get('segmentation')
        if not seg or len(seg) < 6:
            return
        cy, cx = node['centroid'][0], node['centroid'][1]
        new, xs, ys = [], [], []
        for i in range(0, len(seg) - 1, 2):
            nx = cx + (seg[i] - cx) * scale
            ny = cy + (seg[i + 1] - cy) * scale
            new.extend((nx, ny))
            xs.append(nx)
            ys.append(ny)
        node['segmentation'] = new
        node['bbox'] = [min(xs), min(ys), max(xs), max(ys)]
        if node.get('area'):
            node['area'] = node['area'] * (scale * scale)

    def _move_node_geom(self, node_id: str, dx: float, dy: float):
        """Сдвинуть узел (centroid + bbox + segmentation) на (dx, dy)."""
        node = self.nodes.get(node_id)
        if not node:
            return
        node['centroid'] = [node['centroid'][0] + dy, node['centroid'][1] + dx]
        bb = node.get('bbox')
        if bb and len(bb) == 4:
            node['bbox'] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
        seg = node.get('segmentation')
        if seg and len(seg) >= 6:
            node['segmentation'] = [
                seg[i] + (dx if i % 2 == 0 else dy) for i in range(len(seg))
            ]

    def _spread_overlaps(self, grower_ids: list):
        """Расталкивание наслоений: соседи изменённых боксов «отплывают».

        Направление — по доминирующей оси взаимного расположения центроидов
        (сосед снизу → вниз, справа → вправо). Сдвиг = глубина пересечения по
        этой оси + зазор. Растущие узлы — якоря (не двигаются). Каскадно.
        """
        gap = self.RESIZE_SPREAD_GAP
        growers = set(grower_ids)

        def real_bbox(nid):
            n = self.nodes.get(nid)
            bb = n.get('bbox') if n else None
            return bb if (bb and len(bb) == 4) else None

        active = list(grower_ids)
        MAX_PASSES = 50
        for _ in range(MAX_PASSES):
            any_push = False
            for gid in list(active):
                gb = real_bbox(gid)
                if not gb:
                    continue
                gcx, gcy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
                for nid, node in self.nodes.items():
                    if nid == gid or nid in growers:
                        continue  # якоря не двигаем
                    nb = real_bbox(nid)
                    if not nb:
                        continue
                    ox = min(gb[2], nb[2]) - max(gb[0], nb[0])
                    oy = min(gb[3], nb[3]) - max(gb[1], nb[1])
                    if ox <= 0 or oy <= 0:
                        continue  # нет наслоения
                    ncx, ncy = (nb[0] + nb[2]) / 2, (nb[1] + nb[3]) / 2
                    ddx, ddy = ncx - gcx, ncy - gcy
                    if abs(ddy) >= abs(ddx):
                        sign = 1 if ddy >= 0 else -1
                        self._move_node_geom(nid, 0, sign * (oy + gap))
                    else:
                        sign = 1 if ddx >= 0 else -1
                        self._move_node_geom(nid, sign * (ox + gap), 0)
                    if nid not in active:
                        active.append(nid)
                    any_push = True
            if not any_push:
                break

    def apply_resize(self, width=None, height=None, scale=None):
        """Применить размеры к набору + расталкивание + auto_fix рёбер."""
        if not self._resize_sel:
            self.update_status("Набор пуст")
            return
        kind = self._resize_kind()
        if kind == 'mixed':
            self.update_status("Смешанный набор — оставьте только боксы или только полигоны")
            return
        if kind == 'empty':
            return

        from ui.editors.undo_manager import SnapshotCommand
        # Базлайн = состояние ДО живого превью (чтобы Ctrl+Z вернул и размеры тоже).
        if self._resize_model_base is None:
            self._capture_resize_base()
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd._before = self._resize_model_base
        cmd.description = "Размер объектов"

        # Применить размеры из базлайна (идемпотентно — итог совпадает с превью).
        self._apply_sizes_from_base(width, height, scale, kind)
        grower_ids = [nid for nid in self._resize_sel if self.nodes.get(nid)]

        # развести наслоившихся соседей, затем довести рёбра до ортогональности
        self._spread_overlaps(grower_ids)
        auto_fix_graph(
            self.nodes, self.edges_data,
            equip_max_shift=self.EQUIP_MAX_SHIFT,
            conn_max_shift=self.CONN_MAX_SHIFT,
        )

        self._redraw_all()
        self.model.rebuild_edge_data_index()
        cmd.finalize()
        self.undo_mgr.push_executed(cmd)

        # Сбросить базлайн и обновить панель/рамки от нового состояния.
        self._update_resize_panel()
        self.update_statistics()
        if kind == 'box':
            self.update_status(
                f"Размер применён к {len(grower_ids)} ({int(width)}×{int(height)})"
            )
        else:
            self.update_status(
                f"Масштаб ×{scale:.2f} применён к {len(grower_ids)} полигонам"
            )

    # =================================================================
    # Отображение FXML-скинов внутри боксов
    # =================================================================

    def _skins_dir(self) -> Path:
        """Папка с PNG-скинами (ui/resources/skins), с учётом PyInstaller."""
        base = getattr(sys, "_MEIPASS", None)
        if base:
            p = Path(base) / "ui" / "resources" / "skins"
            if p.is_dir():
                return p
        return Path(__file__).resolve().parents[1] / "resources" / "skins"

    def _skin_pixmap_for(self, class_name: str | None):
        """QPixmap скина для класса (по имени файла) или None. Кэшируется."""
        if class_name in self._skin_pixmaps:
            return self._skin_pixmaps[class_name]
        pm = None
        if class_name:
            f = self._skins_dir() / f"{class_name}.png"
            if f.is_file():
                img = QPixmap(str(f))
                if not img.isNull():
                    pm = img
        self._skin_pixmaps[class_name] = pm
        return pm

    def set_show_skins(self, on: bool):
        """Включить/выключить отрисовку скинов внутри боксов."""
        self.show_skins = bool(on)
        self._redraw_skins()
        self.update_status("Скины " + ("показаны" if self.show_skins else "скрыты"))

    def _clear_skin_items(self):
        for items in self._skin_items.values():
            for it in items:
                try:
                    self.scene.removeItem(it)
                except Exception:
                    pass
        self._skin_items.clear()

    def _fit_pixmap(self, item: QGraphicsPixmapItem, pm: QPixmap,
                    x1: float, y1: float, w: float, h: float):
        """Вписать пиксмап в bbox (с сохранением пропорций, по центру)."""
        pw, ph = pm.width(), pm.height()
        if pw <= 0 or ph <= 0 or w <= 0 or h <= 0:
            return
        s = min(w / pw, h / ph)
        item.setScale(s)
        item.setPos(x1 + (w - pw * s) / 2.0, y1 + (h - ph * s) / 2.0)

    def _skin_bg_color(self) -> QColor:
        """Цвет фона-«тайла» скина: белый, затемнённый общим фактором листа."""
        g = max(0, min(255, int(round(255 * (1.0 - self._bg_darkness)))))
        return QColor(g, g, g)

    def _node_orientation(self, node_id):
        """Ориентация узла для скина/размера — по рёбрам (Часть 1).

        Датчик всегда горизонтальный (как FORCE_HORIZONTAL в FXML).
        None → рёбер нет; вызывающий код решает по размеру бокса.
        """
        node = self.nodes.get(node_id)
        if node is not None and node.get('class_name') == 'datchik':
            return 'HORIZONTAL'
        return node_orientation_by_edges(node_id, self.nodes, self.edges_data)

    def _oriented_skin_pixmap(self, base_pm: QPixmap, orientation: str) -> QPixmap:
        """База PNG — вертикальная (кол.1). Горизонталь → поворот −90° CCW (кол.3)."""
        if orientation == 'HORIZONTAL':
            return base_pm.transformed(QTransform().rotate(-90),
                                       Qt.TransformationMode.SmoothTransformation)
        return base_pm

    def _apply_skin_geometry(self, node_id: str):
        """Обновить фон-тайл (цвет затемнения) + ориентацию/вписывание скина."""
        items = self._skin_items.get(node_id)
        node = self.nodes.get(node_id)
        if not items or not node:
            return
        bg, item = items
        bb = node.get("bbox")
        if not bb or len(bb) != 4:
            return
        x1, y1, x2, y2 = bb
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            return
        bg.setRect(x1, y1, w, h)
        bg.setBrush(QBrush(self._skin_bg_color()))
        base = self._skin_pixmap_for(node.get("class_name"))
        if base is None:
            return
        orient = self._node_orientation(node_id) or ('HORIZONTAL' if w > h else 'VERTICAL')
        disp = self._oriented_skin_pixmap(base, orient)
        item.setPixmap(disp)
        self._fit_pixmap(item, disp, x1, y1, w, h)

    def _add_node_skin(self, node_id: str, node: dict, base_pm: QPixmap):
        bb = node.get("bbox")
        if not bb or len(bb) != 4:
            return
        x1, y1, x2, y2 = bb
        if (x2 - x1) <= 0 or (y2 - y1) <= 0:
            return
        # непрозрачный фон-«тайл» (затемняется общим ползунком вместе с листом)
        bg = QGraphicsRectItem(0, 0, 1, 1)
        bg.setPen(QPen(Qt.PenStyle.NoPen))
        bg.setZValue(4.0)
        self.scene.addItem(bg)
        # сам скин (прозрачный фон у PNG → виден затемнённый тайл)
        item = QGraphicsPixmapItem()
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(4.1)
        self.scene.addItem(item)
        self._skin_items[node_id] = [bg, item]
        self._apply_skin_geometry(node_id)

    def _redraw_skins(self):
        """Перерисовать все скины по текущему состоянию (вызывается в _redraw_all)."""
        self._clear_skin_items()
        if not self.show_skins:
            return
        for nid, node in self.nodes.items():
            if node.get("type") != "equipment":
                continue
            pm = self._skin_pixmap_for(node.get("class_name"))
            if pm is None:
                continue
            self._add_node_skin(nid, node, pm)

    def _update_node_skin(self, node_id: str):
        """Подогнать скин узла под текущий bbox (для живого резайза)."""
        self._apply_skin_geometry(node_id)

    def set_background_darkness(self, darkness: float):
        """Затемнение фона: применяется и к листу, и к фону скинов (общий ползунок)."""
        super().set_background_darkness(darkness)
        if self.show_skins and self._skin_items:
            col = QBrush(self._skin_bg_color())
            for items in self._skin_items.values():
                items[0].setBrush(col)

    # =================================================================
    # Static helpers for obstacle avoidance
    # =================================================================

    @staticmethod
    def _bboxes_overlap(a, b):
        return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])

    @staticmethod
    def _path_hits_obstacles(path_points, obstacles):
        for i in range(len(path_points) - 1):
            ax, ay = path_points[i]
            bx, by = path_points[i + 1]
            for bbox in obstacles:
                if segment_intersects_bbox(ax, ay, bx, by, bbox):
                    return True
        return False

    # =================================================================
    # Keys
    # =================================================================

    def save_graph(self, path: str = "") -> bool:
        """Перед сохранением перенести привязки блоков в узлы/рёбра (для FXML)."""
        if hasattr(self, "_sync_bindings_to_graph"):
            self._sync_bindings_to_graph()
        return super().save_graph(path)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._handle_escape()
            return
        elif event.key() == Qt.Key.Key_Delete:
            self.batch_delete()
            return
        elif event.key() == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return
        elif event.key() == Qt.Key.Key_G:
            self.toggle_grid()
            return
        super().keyPressEvent(event)

    def _node_drag_allowed(self) -> bool:
        """Перетаскивание узлов — только когда не активен инструмент (базовое/idle).

        В любом активном инструменте (добавить ребро/перекрёсток/узел,
        оптимизировать, точки изгиба, размеры, ocr) Ctrl+ЛКМ идёт в инструмент,
        а не двигает узел/коннектор.
        """
        return self._current_mode in ("", "idle")

    def _handle_escape(self):
        """Двухступенчатый Esc.

        1-я ступень: если активен инструмент состояния (mode ≠ idle) или есть
                     выделение — выключить инструмент/снять выделение,
                     оставаясь в текущем состоянии.
        2-я ступень: если инструмент не активен и снимать нечего —
                     вернуться в базовое состояние.
        """
        # Правка полигона имеет собственный выход (с коммитом изменений).
        if self._poly_edit_node:
            self._exit_polygon_editing_mode(commit=True)
            return

        # resize_node имеет собственный корректный выход
        if self._current_mode == "resize_node" and hasattr(self, "_stop_resize"):
            self._stop_resize()
            return

        # ocr_bind — резидентный режим состояния 'ocr', не считается инструментом
        resting_modes = ("", "idle", "ocr_bind")
        active_tool = self._current_mode not in resting_modes
        had_selection = bool(self.selected_nodes or self.selected_edges) \
            or bool(getattr(self, "_selected_ocr", None))

        # Снять любые выделения/превью
        self.clear_multi_select()
        self.clear_selection()
        if getattr(self, "_selected_ocr", None):
            self._selected_ocr.clear()
            self.refresh_ocr_layer()

        if active_tool:
            # 1-я ступень — выключить активный инструмент, состояние сохраняем.
            self.set_mode("idle")
            # В состоянии 'ocr' вернуть резидентную привязку блоков.
            if self.display_regime == "ocr":
                self.set_mode("ocr_bind")
            return
        if had_selection:
            # был только выбор без инструмента — уже сняли, остаёмся в состоянии
            return
        # 2-я ступень — вернуться в базовое состояние
        if self.display_regime != "base":
            self.set_display_regime("base")

    # =================================================================
    # Ctrl+ЛКМ: клик → KKS/handler, drag → перетаскивание
    # =================================================================

    def _on_ctrl_lmb_click(self, x: float, y: float, node_id: str):
        """Ctrl+ЛКМ клик (без drag) на узле."""
        # Правка полигона: одиночный Ctrl+ЛКМ по вершине (без drag) — ничего.
        if self._poly_edit_node:
            return
        # Режим «Размер объектов»: Ctrl+ЛКМ добавляет экземпляр в набор.
        if self._current_mode == "resize_objects":
            self._resize_handle_ctrl_click(x, y)
            return
        # В режиме «Размер и цвет» клик по узлу ничего не красит — узлы только тащим.
        if self._current_mode in ("edit_edge_color", "edit_edge_size"):
            return
        # Ctrl+клик идёт в handler (add_edge, add_connector, delete_node и т.п.)
        if self._current_handler:
            self._current_handler.on_press(self, x, y, None)

    def _start_ctrl_drag(self, node_id: str):
        """Ctrl+ЛКМ drag → начать перетаскивание узла.

        Перетаскивание доступно во всех режимах, кроме «Размер объектов»
        (там Ctrl+ЛКМ только набирает экземпляры, узлы двигать нельзя).
        """
        # Правка полигона: Ctrl+ЛКМ по вершине → тянем вершину, а не узел.
        if (self._poly_edit_node and self._poly_overlay
                and self._poly_overlay.phase == "edit"):
            vtx = self._poly_overlay.find_vertex_at(
                self._ctrl_lmb_start_x, self._ctrl_lmb_start_y)
            if vtx is not None:
                self._poly_op_before = self.model.snapshot()
                self._poly_overlay.start_drag(vtx)
                return
        if self._current_mode == "resize_objects":
            return
        self.start_drag_node(node_id)

    def _update_ctrl_drag(self, x: float, y: float):
        """Ctrl+ЛКМ drag → обновить позицию."""
        # Правка полигона: тянем вершину через оверлей.
        if (self._poly_edit_node and self._poly_overlay
                and self._poly_overlay.is_dragging):
            self._poly_overlay.drag_to(x, y)
            return
        if self.dragging_node:
            self.drag_node_to(x, y)

    def _end_ctrl_drag(self):
        """Ctrl+ЛКМ drag → завершить перетаскивание."""
        # Правка полигона: завершить перетаскивание вершины + шаг undo (если сдвинули).
        if (self._poly_edit_node and self._poly_overlay
                and self._poly_overlay.is_dragging):
            idx = self._poly_overlay._dragging_idx
            result = self._poly_overlay.end_drag()
            moved = False
            if result is not None and idx is not None:
                old_x, old_y = result
                poly = self._poly_overlay.get_polygon()
                if idx * 2 + 1 < len(poly):
                    moved = (abs(old_x - poly[idx * 2]) > 0.5
                             or abs(old_y - poly[idx * 2 + 1]) > 0.5)
            if moved:
                self._poly_write_node()
                self._poly_push(self._poly_op_before, "Двигать вершину")
            self._poly_op_before = None
            return
        if self.dragging_node:
            self.end_drag_node()

    # =================================================================
    # Правка точек полигона (базовое состояние, Ctrl+2ЛКМ по полигону)
    # =================================================================

    def mousePressEvent(self, event):
        """В режиме правки полигона перехватываем Ctrl+ЛКМ по вершине/ребру ДО
        базовой логики (иначе клик ушёл бы в перетаскивание узла)."""
        if (self._poly_edit_node and self._poly_overlay
                and self.ctrl_pressed
                and event.button() == Qt.MouseButton.LeftButton
                and self._poly_overlay.phase == "edit"):
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            # Вершина → отложенный клик/drag (решение по движению — в базовом классе).
            if self._poly_overlay.find_vertex_at(x, y) is not None:
                self._ctrl_lmb_pending = True
                self._ctrl_lmb_start_x = x
                self._ctrl_lmb_start_y = y
                self._ctrl_lmb_node = self._poly_edit_node
                self._ctrl_lmb_dragging = False
                event.accept()
                return
            # Ребро → добавить вершину сразу.
            edge_idx = self._poly_overlay.find_edge_at(x, y)
            if edge_idx is not None:
                self._poly_add_vertex(edge_idx, x, y)
                event.accept()
                return
            # Мимо вершин/рёбер → выйти из правки (с коммитом).
            self._exit_polygon_editing_mode(commit=True)
            event.accept()
            return
        super().mousePressEvent(event)

    def _enter_polygon_editing_mode(self, node_id: str):
        """Войти в правку точек полигона узла (Ctrl+2ЛКМ по полигону в базовом).

        Каждая операция (сдвиг/добавление/удаление вершины) — отдельный шаг undo.
        """
        node = self.nodes.get(node_id)
        seg = node.get('segmentation') if node else None
        if not (seg and isinstance(seg, list) and len(seg) >= 6):
            return
        self._exit_polygon_editing_mode()
        from ui.editors.polygon_overlay import PolygonVertexOverlay
        self._poly_op_before = None
        self._poly_edit_node = node_id
        self._poly_overlay = PolygonVertexOverlay(self.scene, node_id)
        self._poly_overlay.show_edit(seg)
        self.update_status(
            f"Правка полигона {node_id}: тяните вершины · Ctrl+ЛКМ по ребру — "
            "добавить · Ctrl+ПКМ по вершине — удалить · Ctrl+Z — отмена · Esc — выход"
        )

    def _poly_write_node(self):
        """Записать полигон из оверлея в узел + пересчитать bbox/центроид/визуал."""
        if not (self._poly_edit_node and self._poly_overlay):
            return
        seg = self._poly_overlay.get_polygon()
        node = self.nodes.get(self._poly_edit_node)
        if not node or not seg or len(seg) < 6:
            return
        node['segmentation'] = list(seg)
        xs = seg[0::2]
        ys = seg[1::2]
        node['bbox'] = [min(xs), min(ys), max(xs), max(ys)]
        node['centroid'] = [sum(ys) / len(ys), sum(xs) / len(xs)]  # [y, x]
        node['area'] = (node['bbox'][2] - node['bbox'][0]) * \
                       (node['bbox'][3] - node['bbox'][1])
        self._refresh_node_visual(self._poly_edit_node)

    def _poly_push(self, before_snap, desc: str):
        """Зафиксировать операцию правки полигона отдельным шагом undo."""
        if before_snap is None:
            return
        from ui.editors.undo_manager import SnapshotCommand
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd._before = before_snap
        cmd.description = desc
        cmd.finalize()
        self.model.rebuild_edge_data_index()
        self.undo_mgr.push_executed(cmd)
        self.update_statistics()

    def _poly_add_vertex(self, edge_idx: int, x: float, y: float):
        """Вставить вершину на ребре полигона под курсором (шаг undo)."""
        if not self._poly_overlay:
            return
        before = self.model.snapshot()
        self._poly_overlay.insert_vertex(edge_idx, x, y)
        self._poly_write_node()
        self._poly_push(before, "Добавить вершину")
        self.update_status(
            f"Добавлена вершина (всего {self._poly_overlay.vertex_count})")

    def _poly_resync_overlay(self):
        """После undo/redo пересоздать оверлей на восстановленной геометрии узла."""
        if not self._poly_edit_node:
            return
        node = self.nodes.get(self._poly_edit_node)
        seg = node.get('segmentation') if node else None
        if self._poly_overlay:
            self._poly_overlay.hide()
            self._poly_overlay = None
        if seg and isinstance(seg, list) and len(seg) >= 6:
            from ui.editors.polygon_overlay import PolygonVertexOverlay
            self._poly_overlay = PolygonVertexOverlay(
                self.scene, self._poly_edit_node)
            self._poly_overlay.show_edit(seg)
        else:
            # полигон исчез после undo/redo — выйти из правки
            self._poly_edit_node = None
            self._poly_op_before = None

    def undo(self):
        super().undo()
        self._poly_resync_overlay()

    def redo(self):
        super().redo()
        self._poly_resync_overlay()

    def _exit_polygon_editing_mode(self, commit: bool = True):
        """Выйти из правки полигона. Все операции уже зафиксированы пошагово как
        undo-шаги, поэтому commit не используется (параметр оставлен для
        совместимости существующих вызовов)."""
        if not self._poly_edit_node:
            return
        if self._poly_overlay:
            self._poly_overlay.hide()
        self._poly_edit_node = None
        self._poly_overlay = None
        self._poly_op_before = None
        self._redraw_all()

    def mouseMoveEvent(self, event):
        """Override: KKS hover tooltip при наведении на equipment."""
        pos = self.mapToScene(event.pos())
        x, y = pos.x(), pos.y()

        # KKS hover tooltip — ищем equipment по bbox (а не только по centroid).
        # KKS берём из graph.bindings (единый источник), не из node.kks_full.
        hovered_kks = None
        if self._ocr_highlight:
            kks_nodes = {
                b.get("node_id"): (b.get("text") or "").strip()
                for b in self.model.bindings
                if b.get("node_id") and (b.get("text") or "").strip()
            }
            for node_id, node in self.nodes.items():
                if node.get('type') != 'equipment' or node_id not in kks_nodes:
                    continue
                bbox = node.get('bbox')
                if bbox and len(bbox) == 4:
                    x1, y1, x2, y2 = bbox
                    if x1 <= x <= x2 and y1 <= y <= y2:
                        hovered_kks = kks_nodes[node_id]
                        break

        if hovered_kks:
            # mapToGlobal через viewport — гарантированно работает в PySide6
            global_pos = self.viewport().mapToGlobal(event.pos())
            QToolTip.showText(global_pos, f"KKS: {hovered_kks}", self.viewport())
        elif getattr(self, '_kks_tooltip_visible', False):
            QToolTip.hideText()

        self._kks_tooltip_visible = bool(hovered_kks)

        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Ctrl+двойной клик — единый жест правки, зависящий от состояния:

          • базовое      — по боксу: ручки размера; по полигону: правка точек;
          • ОКР привязка — только KKS бокса / диаметр ребра / текст блока;
          • перпендикулярность и линии — ничего.

        Простой двойной клик (без Ctrl) ничего не делает.
        """
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return

        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier) \
            or self.ctrl_pressed
        if not ctrl:
            event.accept()
            return

        pos = self.mapToScene(event.pos())
        x, y = pos.x(), pos.y()
        regime = self.display_regime

        if regime == "ocr":
            # Текст-блок → правка текста.
            if hasattr(self, "_ocr_block_at"):
                bid = self._ocr_block_at(x, y)
                if bid is not None:
                    self.edit_ocr_block_text(bid)
                    return
            # Equipment → KKS.
            clicked = self.find_node_at(x, y)
            if clicked:
                node = self.nodes.get(clicked)
                if node and node.get('type') == 'equipment':
                    self._open_kks_edit_dialog(clicked)
                    return
            # Ребро → диаметр.
            edge_key, _ = self.find_nearest_edge(x, y, threshold=15.0)
            if edge_key:
                self._open_diameter_edit_dialog(edge_key)
            return

        if regime == "base":
            clicked = self.find_node_at(x, y)
            if clicked:
                node = self.nodes.get(clicked)
                if node and node.get('type') == 'equipment':
                    # Полигон → правка точек; бокс → ручки размера.
                    if self._node_geom_kind(node) == 'poly':
                        self._enter_polygon_editing_mode(clicked)
                    elif node.get('bbox'):
                        self._enter_resize_mode(clicked)
                    return
            return

        # perp / style → ничего.
        event.accept()

    def _open_diameter_edit_dialog(self, edge_key: tuple):
        """Диалог редактирования диаметра ребра."""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QFormLayout, QLineEdit, QDialogButtonBox, QLabel

        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return

        edge_id = edge_data.get('id', f'{edge_key[0]}|{edge_key[1]}')
        current_text = edge_data.get('diameter_text', '')
        current_value = edge_data.get('diameter_value', 0)

        dialog = QDialog(self)
        dialog.setWindowTitle("Диаметр ребра")
        dialog.setMinimumWidth(300)
        layout = QVBoxLayout(dialog)

        header = QLabel(f"Ребро: {edge_id}")
        layout.addWidget(header)

        form = QFormLayout()
        text_edit = QLineEdit(str(current_text))
        text_edit.setFont(QFont("monospace", 12))
        text_edit.setPlaceholderText("200")
        text_edit.selectAll()
        form.addRow("Диаметр:", text_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        text_edit.setFocus()

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_text = text_edit.text().strip()

            if new_text:
                import re
                # Чистое число → диаметр без префикса
                if re.fullmatch(r'\d+', new_text):
                    new_value = int(new_text)
                    new_prefix = ''
                    new_suffix = ''
                else:
                    # Полный формат (Dy200, DN50) — парсить как раньше
                    m = re.search(r'(\d{2,4})', new_text)
                    new_value = int(m.group(1)) if m else 0
                    prefix_m = re.match(r'([A-Za-zА-Яа-я]+)', new_text)
                    new_prefix = prefix_m.group(1) if prefix_m else ''
                    suffix_m = re.search(r'\d([A-Za-zА-Яа-я]+)$', new_text)
                    new_suffix = suffix_m.group(1) if suffix_m else ''

                edge_data['diameter_text'] = new_text
                edge_data['diameter_value'] = new_value
                edge_data['diameter_prefix'] = new_prefix
                edge_data['diameter_suffix'] = new_suffix
                edge_data['diameter_confidence'] = 1.0
                edge_data['diameter_propagated'] = False  # ручное — источник
            else:
                # Очистить диаметр
                for k in ('diameter_text', 'diameter_value', 'diameter_prefix',
                          'diameter_suffix', 'diameter_confidence',
                          'diameter_propagated'):
                    edge_data.pop(k, None)

            # Распространить диаметры по трубам
            prop_count = self._propagate_all_diameters()

            # Refresh visual (edge color may change)
            self._redraw_all()
            msg = f"Диаметр: {new_text}" if new_text else "Диаметр удалён"
            if prop_count:
                msg += f" (распространено на {prop_count} рёбер)"
            self.update_status(msg)

    def _propagate_all_diameters(self) -> int:
        """Распространить диаметры по трубам от рёбер-источников.

        Алгоритм:
        1. Очистить все propagated рёбра (diameter_propagated=True)
        2. Собрать source рёбра (не propagated, имеют diameter_text)
        3. Propagate от sources
        4. Записать с diameter_propagated=True

        Returns:
            Количество распространённых рёбер.
        """
        try:
            from modules.text_binding.binder import TextBinder, DiameterBinding
            from modules.text_binding.config import TextRecognitionConfig

            edges = self.model.edges_data

            # 1. Очистить все propagated рёбра
            for edge in edges:
                if edge.get('diameter_propagated'):
                    for k in ('diameter_text', 'diameter_value', 'diameter_prefix',
                              'diameter_suffix', 'diameter_confidence',
                              'diameter_propagated'):
                        edge.pop(k, None)

            # 2. Собрать source bindings (не propagated, с diameter_text)
            nodes = list(self.model.nodes.values())
            bindings = []
            for idx, edge in enumerate(edges):
                dtext = edge.get('diameter_text')
                if not dtext:
                    continue
                src = edge.get('source', '')
                tgt = edge.get('target', '')
                bindings.append(DiameterBinding(
                    ocr_block_idx=-1,
                    edge_idx=idx,
                    edge_id=edge.get('id', ''),
                    edge_key=f"{src}|{tgt}",
                    text=dtext,
                    prefix=edge.get('diameter_prefix', ''),
                    diameter=edge.get('diameter_value', 0),
                    suffix=edge.get('diameter_suffix', ''),
                    confidence=edge.get('diameter_confidence', 1.0),
                    distance=0.0,
                ))

            if not bindings:
                return 0

            # 3. Propagate
            cfg = TextRecognitionConfig()
            binder = TextBinder(cfg)
            report = binder.propagate_diameters(nodes, edges, bindings)

            # 4. Записать propagated с пометкой
            propagated_count = 0
            for pd in report.propagated:
                if pd.edge_idx < len(edges):
                    edge = edges[pd.edge_idx]
                    if edge.get('diameter_text'):
                        continue  # source — не трогать
                    edge['diameter_text'] = pd.text
                    edge['diameter_value'] = pd.diameter
                    edge['diameter_prefix'] = pd.prefix
                    edge['diameter_suffix'] = pd.suffix
                    edge['diameter_confidence'] = pd.confidence
                    edge['diameter_propagated'] = True
                    propagated_count += 1

            return propagated_count

        except ImportError:
            return 0
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Diameter propagation failed: %s", e)
            return 0

    def _toggle_kks_label(self, node_id: str):
        """Toggle KKS label above the equipment bbox."""
        if node_id in self._kks_labels:
            self._hide_kks_label(node_id)
        else:
            self._show_kks_label(node_id)

    def _show_kks_label(self, node_id: str):
        """Show KKS label at the top edge of the equipment bbox."""
        if not self._ocr_highlight:
            return
        node = self.nodes.get(node_id)
        if not node:
            return
        kks = self._node_kks(node_id)
        if not kks:
            return
        bbox = node.get('bbox')
        cx, cy = node['centroid'][1], node['centroid'][0]

        # Position: above bbox top edge, centered
        if bbox and len(bbox) == 4:
            label_x = (bbox[0] + bbox[2]) / 2
            label_y = bbox[1] - 5  # slightly above top edge
        else:
            label_x = cx
            label_y = cy - 15

        text_item = QGraphicsSimpleTextItem(kks)
        font = QFont("monospace", 10)
        font.setBold(True)
        text_item.setFont(font)
        text_item.setBrush(QBrush(QColor(46, 204, 113)))  # green text
        text_item.setZValue(20)

        # Center the text
        br = text_item.boundingRect()
        text_item.setPos(label_x - br.width() / 2, label_y - br.height())

        self.scene.addItem(text_item)
        self._kks_labels[node_id] = text_item

    def _hide_kks_label(self, node_id: str):
        """Hide KKS label for a node."""
        item = self._kks_labels.pop(node_id, None)
        if item:
            self.scene.removeItem(item)

    def _hide_all_kks_labels(self):
        """Hide all KKS labels (Escape)."""
        for item in self._kks_labels.values():
            self.scene.removeItem(item)
        self._kks_labels.clear()

    def _node_kks(self, node_id: str) -> str:
        """KKS узла — из graph.bindings (единый источник истины). '' если нет."""
        for b in self.model.bindings:
            if b.get("node_id") == node_id:
                t = (b.get("text") or "").strip()
                if t:
                    return t
        return ""

    def _set_node_kks(self, node_id: str, kks: str):
        """Записать ручной KKS узла как привязку в graph.bindings.

        Один узел — один KKS: убираем любые прежние привязки этого узла
        (OCR или ручные) и ставим ручную. Пустой kks — просто снятие.
        """
        self.model.bindings = [
            b for b in self.model.bindings if b.get("node_id") != node_id
        ]
        if kks:
            self.model.set_binding({
                "block_id": f"nodekks_{node_id}",
                "node_id": node_id,
                "kind": "node",
                "text": kks,
                "source": "manual_kks",
            })

    def _open_kks_edit_dialog(self, node_id: str):
        """Open dialog to edit KKS of an equipment node — single text field."""
        node = self.nodes.get(node_id)
        if not node:
            return

        dialog = QDialog()
        dialog.setWindowTitle(f"KKS — {node_id} ({node.get('class_name', '')})")
        dialog.setMinimumWidth(300)

        layout = QVBoxLayout(dialog)

        # Header
        header = QLabel(f"Узел: {node_id}  |  Класс: {node.get('class_name', '?')}")
        layout.addWidget(header)

        # Single KKS field
        form = QFormLayout()
        kks_edit = QLineEdit(self._node_kks(node_id))
        kks_edit.setFont(QFont("monospace", 12))
        kks_edit.selectAll()
        form.addRow("KKS:", kks_edit)
        layout.addLayout(form)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        kks_edit.setFocus()

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_kks = kks_edit.text().strip()

            # B6.5: нормализация через KksMatcher (если доступен config)
            final_kks = new_kks
            if new_kks and self._project_config_dir:
                try:
                    from pathlib import Path
                    from modules.kks_binding.matcher import KksMatcher
                    from modules.kks_binding.config import KksConfig, ClassToKksConfig
                    from PySide6.QtWidgets import QMessageBox

                    kks_cfg_path = Path(self._project_config_dir) / "kks_config.yaml"
                    if kks_cfg_path.exists():
                        kks_cfg = KksConfig.from_yaml(str(kks_cfg_path))
                        matcher = KksMatcher(kks_cfg)
                        km = matcher.match(new_kks)

                        if km:
                            final_kks = km.full
                            # Валидация unit↔class (предупреждение, не блокирует)
                            cls_cfg_path = Path(self._project_config_dir) / "class_to_kks_config.yaml"
                            if cls_cfg_path.exists():
                                cls_cfg = ClassToKksConfig.from_yaml(str(cls_cfg_path))
                                rule = cls_cfg.class_to_kks.get(node.get('class_name', ''))
                                if rule and rule.expected_units and km.unit not in rule.expected_units:
                                    QMessageBox.warning(self, "Предупреждение",
                                        f"Unit '{km.unit}' не ожидается для '{node.get('class_name')}'.\n"
                                        f"Ожидаемые: {rule.expected_units}")
                        else:
                            QMessageBox.warning(self, "Предупреждение",
                                f"'{new_kks}' не распознан как KKS. Сохранено без нормализации.")
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("KKS normalization failed: %s", exc)

            # Единый источник истины — graph.bindings (node.kks_full не пишем).
            if final_kks != self._node_kks(node_id):
                cmd = self._ocr_push_snapshot("Ручной KKS")
                self._set_node_kks(node_id, final_kks)
                self._ocr_commit(cmd)

            saved_kks = final_kks

            # Update label if visible
            if node_id in self._kks_labels:
                self._hide_kks_label(node_id)
                if saved_kks:
                    self._show_kks_label(node_id)

            # Refresh visual (fill color may change)
            self._redraw_all()
            self.refresh_ocr_layer()
            self.update_status(f"KKS обновлён: {saved_kks}" if saved_kks else "KKS удалён")
