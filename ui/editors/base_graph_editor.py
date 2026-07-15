"""
Base Graph Editor — базовый класс редактора графа P&ID.

Рендеринг, zoom, hit testing, selection UI, event delegation.
Не содержит CRUD, оптимизацию, drag, multi-select — это в потомках.

Принцип: Base НЕ обращается к атрибутам потомков.
Вся расширяемость — через виртуальные методы и хуки.
"""

import math
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem,
    QGraphicsLineItem, QGraphicsRectItem, QGraphicsPathItem,
    QGraphicsPixmapItem, QGraphicsSimpleTextItem, QFileDialog,
)
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QBrush, QPen,
    QPainterPath, QWheelEvent, QFont,
)
from PySide6.QtCore import Qt, QRectF

from ui.editors.graph_data import GraphDataModel
from ui.editors.undo_manager import UndoManager
from ui.editors.mode_handlers.base_handler import ModeHandler
from ui.editors.graph_geometry import bbox_exit_side, bbox_side_midpoint


class BaseGraphEditor(QGraphicsView):
    """Базовый редактор графа — рендеринг, навигация, hit testing.

    Потомки: SimpleGraphEditor, AdvancedGraphEditor.
    """

    # ── Цвета ──
    COLOR_EQUIPMENT = QColor("#3498db")
    COLOR_CONNECTOR = QColor("#2ecc71")
    COLOR_ISOLATED = QColor("#e74c3c")
    COLOR_EDGE = QColor(255, 255, 255, 150)
    COLOR_EDGE_BAD = QColor("#e67e22")
    COLOR_SELECTION = QColor("#f1c40f")
    COLOR_HOVER = QColor("#9b59b6")
    COLOR_PREVIEW_OK = QColor("#2ecc71")
    COLOR_PREVIEW_NO = QColor("#7f8c8d")
    COLOR_PREVIEW_DELETE = QColor("#e74c3c")
    COLOR_EDGE_HIGHLIGHT = QColor("#f39c12")
    COLOR_CONNECTOR_PREVIEW = QColor("#f1c40f")
    COLOR_KKS_BOUND = QColor(46, 204, 113, 120)   # semi-transparent green for KKS-bound nodes
    COLOR_NO_KKS = QColor(231, 76, 60, 140)        # vivid red for equipment without KKS
    COLOR_NO_DIAMETER = QColor(255, 60, 40, 180)    # bright red for edges without diameter
    COLOR_KKS_LABEL_BG = QColor(0, 0, 0, 160)      # label background

    # ── Размеры ──
    EQUIPMENT_MARKER_RADIUS = 6
    CONNECTOR_MARKER_RADIUS = 8
    CLICK_THRESHOLD = 20
    SELECTION_RING_WIDTH = 3
    EDGE_WIDTH = 4

    def __init__(self):
        super().__init__()

        self.scene = QGraphicsScene()
        self.setScene(self.scene)

        # ── Data ──
        self.model = GraphDataModel()
        self.undo_mgr = UndoManager()

        # ── Image ──
        self.original_image: QImage | None = None
        self.img_width: int = 0
        self.img_height: int = 0
        # WYSIWYG: система координат сцены = холст 1920x1080 (граф уже в этих
        # координатах после pretransform). Фон вписывается scale-трансформом.
        self.canvas_w: float = 1920.0
        self.canvas_h: float = 1080.0
        self._canvas_mode: bool = False   # True когда граф пришёл в координатах холста
        self._bg_scale: float = 1.0
        self._bg_offx: float = 0.0
        self._bg_offy: float = 0.0

        # ── Graphics items ──
        self.node_items: dict[str, QGraphicsEllipseItem] = {}
        self.edge_items: dict[tuple[str, str], QGraphicsPathItem] = {}
        self.edge_label_items: dict[tuple[str, str], QGraphicsSimpleTextItem] = {}
        self.bbox_items: dict[str, QGraphicsRectItem] = {}
        self.polygon_items: dict[str, QGraphicsPathItem] = {}

        # ── Mode system ──
        self._mode_handlers: dict[str, ModeHandler] = {}
        self._current_mode: str = ""
        self._current_handler: ModeHandler | None = None

        # ── UI state ──
        self.selected_node: str | None = None
        self.hovered_node: str | None = None
        self.hovered_edge: tuple[str, str] | None = None
        self.selection_ring: QGraphicsEllipseItem | None = None
        self.hover_ring: QGraphicsEllipseItem | None = None
        self.preview_line: QGraphicsLineItem | None = None
        self.edge_highlight: QGraphicsPathItem | None = None
        self.connector_preview: QGraphicsEllipseItem | None = None

        # ── Ctrl key ──
        self.ctrl_pressed: bool = False

        # ── Callbacks ──
        self.status_callback: Optional[Callable] = None
        self.stats_callback: Optional[Callable] = None
        self.mode_callback: Optional[Callable] = None  # вызывается при set_mode(name)

        # ── View setup ──
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))

        # Фоновая подложка и её затемнение (0..1). 0.6 ≈ прежний вид (alpha 153).
        self._bg_item: QGraphicsPixmapItem | None = None
        self._bg_darkness: float = 0.6
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self.show_placeholder()

    # =================================================================
    # Proxy-свойства → GraphDataModel
    # =================================================================

    @property
    def nodes(self):
        return self.model.nodes

    @property
    def edges(self):
        return self.model.edges

    @property
    def edges_data(self):
        return self.model.edges_data

    @property
    def graph_data(self):
        return self.model.graph_data

    @property
    def coco_annotations(self):
        return self.model.coco_annotations

    # =================================================================
    # Placeholder
    # =================================================================

    def show_placeholder(self):
        """Показать текст-заглушку до загрузки данных."""
        self.scene.clear()
        text = self.scene.addText("Загрузите изображение и граф")
        text.setDefaultTextColor(QColor(150, 150, 150))

    # =================================================================
    # Data I/O
    # =================================================================

    def load_data(self, image_path: str, graph_path: str, coco_path: str = "") -> bool:
        """Загрузка image + граф + COCO.

        Advanced переопределяет для _compute_grid_size().
        """
        # 1. Image
        self.original_image = QImage(image_path)
        if self.original_image.isNull():
            self.update_status("Ошибка загрузки изображения")
            return False

        self.img_width = self.original_image.width()
        self.img_height = self.original_image.height()

        # 2. Graph + COCO → model
        if not self.model.load(graph_path, coco_path):
            self.update_status("Ошибка загрузки графа")
            return False

        # 3. Scene
        self.setup_scene()
        self.update_statistics()
        return True

    def save_graph(self, path: str = "") -> bool:
        """Сохранить граф в JSON."""
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Сохранить граф", "graph_edited.json", "JSON (*.json)"
            )
            if not path:
                return False
        result = self.model.save(path)
        if result:
            self.update_status(f"Сохранено: {path}")
        else:
            self.update_status("Ошибка сохранения")
        return result

    # =================================================================
    # Scene rendering
    # =================================================================

    def setup_scene(self):
        """Настройка сцены со всеми слоями.

        WYSIWYG: сцена = холст 1920x1080 (граф уже в этих координатах). Фон —
        полноразмерный оригинал, ВПИСАННЫЙ в холст scale-трансформом (не даунсэмпл:
        зум остаётся резким). Множитель s и офсеты — те же, что в
        modules.graph.core.pretransform (letterbox по размеру картинки).
        """
        self.scene.clear()
        self._reset_scene_state()

        if self._canvas_mode and self.img_width and self.img_height:
            # WYSIWYG: сцена = холст 1920x1080; фон вписан scale-трансформом.
            self._bg_scale = min(self.canvas_w / self.img_width,
                                 self.canvas_h / self.img_height)
            self._bg_offx = (self.canvas_w - self.img_width * self._bg_scale) / 2.0
            self._bg_offy = (self.canvas_h - self.img_height * self._bg_scale) / 2.0
            scene_w, scene_h = self.canvas_w, self.canvas_h
        else:
            # Legacy: сцена = пиксели изображения, фон 1:1 (граф в исходных координатах).
            self._bg_scale = 1.0
            self._bg_offx = self._bg_offy = 0.0
            scene_w, scene_h = self.img_width, self.img_height

        # Z=0: Original image (darkened)
        if self.original_image and not self.original_image.isNull():
            darkened = self.original_image.copy().convertToFormat(QImage.Format.Format_ARGB32)
            painter = QPainter(darkened)
            painter.fillRect(darkened.rect(), QColor(0, 0, 0, int(self._bg_darkness * 255)))
            painter.end()

            self._bg_item = QGraphicsPixmapItem(QPixmap.fromImage(darkened))
            self._bg_item.setScale(self._bg_scale)         # полноразмер → вписан в холст
            self._bg_item.setPos(self._bg_offx, self._bg_offy)
            self._bg_item.setZValue(0)
            self.scene.addItem(self._bg_item)

        # Z=1: Edges
        self._draw_all_edges()

        # Z=2-3: Nodes
        self._draw_all_nodes()

        self.setSceneRect(QRectF(0, 0, scene_w, scene_h))
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def set_left_gutter(self, px: int):
        """Отступ слева у видимой области (px виджета).

        Используется, когда поверх редактора слева выезжает панель
        (например, «Размер объектов»): контент сдвигается вправо и панель
        ничего не перекрывает — левый край листа остаётся доступен.
        """
        self.setViewportMargins(max(0, int(px)), 0, 0, 0)

    def _redraw_all(self):
        """Перерисовать без перезагрузки фона (Z=0). Advanced переопределяет для grid."""
        # Удаляем всё кроме фона
        for item in list(self.scene.items()):
            if item.zValue() > 0:
                self.scene.removeItem(item)

        self._reset_scene_state()
        self._draw_all_edges()
        self._draw_all_nodes()
        self.model.rebuild_edge_data_index()

    def _reset_scene_state(self):
        """Сбросить графические dict'ы и UI-состояние.

        Advanced переопределяет для очистки waypoints, grid, selection highlights.
        """
        self.node_items.clear()
        self.edge_items.clear()
        self.edge_label_items.clear()
        self.bbox_items.clear()
        self.polygon_items.clear()
        self.selection_ring = None
        self.hover_ring = None
        self.preview_line = None
        self.edge_highlight = None
        self.connector_preview = None
        self.selected_node = None
        self.hovered_node = None
        self.hovered_edge = None

    # =================================================================
    # Edge rendering
    # =================================================================

    def _draw_all_edges(self):
        """Базовая отрисовка рёбер с хуками для Advanced."""
        self._before_draw_all_edges()
        for edge in self.edges_data:
            src, tgt = edge.get('source'), edge.get('target')
            if src in self.nodes and tgt in self.nodes:
                key = self.model.edge_key(src, tgt)
                self._before_edge_draw(key, edge)
                self.create_edge_item(key, edge)

    def _before_draw_all_edges(self):
        """Хук перед циклом отрисовки. Base: no-op. Advanced: clear perp_scores."""
        pass

    def _before_edge_draw(self, key: tuple, edge: dict):
        """Хук перед рисованием одного ребра. Base: no-op. Advanced: compute perp."""
        pass

    def _build_edge_path(self, source_point, waypoints, target_point) -> QPainterPath:
        """Построить QPainterPath: source → waypoints → target.

        Points в формате [y, x].
        """
        path = QPainterPath()
        if not source_point or not target_point:
            return path

        sx, sy = source_point[1], source_point[0]
        path.moveTo(sx, sy)

        for wp in (waypoints or []):
            path.lineTo(wp[1], wp[0])

        tx, ty = target_point[1], target_point[0]
        path.lineTo(tx, ty)
        return path

    def set_background_darkness(self, darkness: float):
        """Затемнение фоновой подложки. darkness 0..1 (0 — оригинал, 1 — чёрный)."""
        self._bg_darkness = max(0.0, min(1.0, float(darkness)))
        if self.original_image is None or self.original_image.isNull() or self._bg_item is None:
            return
        darkened = self.original_image.copy().convertToFormat(QImage.Format.Format_ARGB32)
        painter = QPainter(darkened)
        painter.fillRect(darkened.rect(), QColor(0, 0, 0, int(self._bg_darkness * 255)))
        painter.end()
        self._bg_item.setPixmap(QPixmap.fromImage(darkened))

    def set_edge_color(self, color: QColor):
        """Цвет обычных рёбер графа."""
        self.COLOR_EDGE = QColor(color)
        self._redraw_all()

    def _get_edge_color(self, edge_data: dict, key: tuple = None) -> QColor:
        """Виртуальный. Base: стандартный цвет. Advanced: подсветка по диаметру/перпендикулярности."""
        return self.COLOR_EDGE

    def _get_equipment_brush(self, node: dict) -> QBrush:
        """Виртуальный. Кисть заливки для equipment bbox/polygon.

        Base: прозрачная (без подсветки KKS).
        Advanced: зелёная если есть kks_full, красная если нет.
        """
        return QBrush(QColor(0, 0, 0, 0))

    def _get_edge_pen(self, edge_data: dict, key: tuple = None) -> QPen:
        """Виртуальный. Base: стандартный pen. Advanced: утолщение для bad."""
        color = self._get_edge_color(edge_data, key)
        return QPen(color, self.EDGE_WIDTH)

    def create_edge_item(self, edge_key: tuple, edge_data: dict,
                         color: QColor = None) -> QGraphicsPathItem:
        """Создать визуальный элемент ребра + подпись диаметра. Public — для Commands."""
        path = self._build_edge_path(
            edge_data.get('source_point'),
            edge_data.get('waypoints', []),
            edge_data.get('target_point')
        )

        if color is not None:
            pen = QPen(color, self.EDGE_WIDTH)
        else:
            pen = self._get_edge_pen(edge_data, edge_key)

        item = QGraphicsPathItem(path)
        item.setPen(pen)
        item.setZValue(1)
        self.scene.addItem(item)
        self.edge_items[edge_key] = item

        # Diameter label on the edge
        diam_text = edge_data.get('diameter_text')
        if diam_text:
            self._create_edge_label(edge_key, edge_data, diam_text)

        return item

    def _create_edge_label(self, edge_key: tuple, edge_data: dict, text: str):
        """Создать подпись диаметра (только число) на середине ребра."""
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if not sp or not tp:
            return

        # Only the number
        diam_val = edge_data.get('diameter_value')
        display = str(int(diam_val)) if diam_val else text

        # Midpoint
        wps = edge_data.get('waypoints', [])
        if wps:
            mid_y = (sp[0] + wps[0][0]) / 2
            mid_x = (sp[1] + wps[0][1]) / 2
        else:
            mid_y = (sp[0] + tp[0]) / 2
            mid_x = (sp[1] + tp[1]) / 2

        label = QGraphicsSimpleTextItem(display)
        font = QFont("sans-serif", 6)
        font.setBold(True)
        label.setFont(font)
        label.setBrush(QBrush(QColor(255, 255, 255, 200)))
        label.setZValue(5)

        # Center ON the edge (not above)
        br = label.boundingRect()
        label.setPos(mid_x - br.width() / 2, mid_y - br.height() / 2)

        self.scene.addItem(label)
        self.edge_label_items[edge_key] = label

    def remove_edge_item(self, key: tuple):
        """Удалить визуальный элемент ребра + подпись. Public — для Commands."""
        if key in self.edge_items:
            self.scene.removeItem(self.edge_items[key])
            del self.edge_items[key]
        if key in self.edge_label_items:
            self.scene.removeItem(self.edge_label_items[key])
            del self.edge_label_items[key]

    def _update_edge_path(self, edge_key: tuple):
        """Обновить path и pen существующего edge item + подпись."""
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data or edge_key not in self.edge_items:
            return

        path = self._build_edge_path(
            edge_data.get('source_point'),
            edge_data.get('waypoints', []),
            edge_data.get('target_point')
        )
        self.edge_items[edge_key].setPath(path)

        pen = self._get_edge_pen(edge_data, edge_key)
        self.edge_items[edge_key].setPen(pen)

        # Update label position
        if edge_key in self.edge_label_items:
            sp = edge_data.get('source_point')
            tp = edge_data.get('target_point')
            if sp and tp:
                wps = edge_data.get('waypoints', [])
                if wps:
                    mid_y = (sp[0] + wps[0][0]) / 2
                    mid_x = (sp[1] + wps[0][1]) / 2
                else:
                    mid_y = (sp[0] + tp[0]) / 2
                    mid_x = (sp[1] + tp[1]) / 2
                label = self.edge_label_items[edge_key]
                br = label.boundingRect()
                label.setPos(mid_x - br.width() / 2, mid_y - br.height() / 2)

    # =================================================================
    # Node rendering
    # =================================================================

    def _draw_all_nodes(self):
        """Отрисовка всех узлов."""
        connected_nodes = set()
        for a, b in self.edges:
            connected_nodes.add(a)
            connected_nodes.add(b)

        # Собираем bbox с полигонами (для дедупликации)
        drawn_bboxes: set[tuple] = set()
        for node_id, node in self.nodes.items():
            if node.get('type') != 'equipment':
                continue
            seg = node.get('segmentation')
            bbox = node.get('bbox')
            has_polygon = seg and isinstance(seg, list) and len(seg) >= 6
            if has_polygon and bbox:
                drawn_bboxes.add(tuple(bbox))

        for node_id, node in self.nodes.items():
            cx, cy = node['centroid'][1], node['centroid'][0]
            node_type = node.get('type', 'connector')
            is_isolated = node_id not in connected_nodes

            if is_isolated:
                color = self.COLOR_ISOLATED
            elif node_type == 'equipment':
                color = self.COLOR_EQUIPMENT
            else:
                color = self.COLOR_CONNECTOR

            if node_type == 'equipment':
                segmentation = node.get('segmentation')
                bbox = node.get('bbox')
                has_polygon = segmentation and isinstance(segmentation, list) and len(segmentation) >= 6

                if has_polygon:
                    path = QPainterPath()
                    path.moveTo(segmentation[0], segmentation[1])
                    for i in range(2, len(segmentation), 2):
                        path.lineTo(segmentation[i], segmentation[i + 1])
                    path.closeSubpath()

                    poly_item = QGraphicsPathItem(path)
                    poly_item.setPen(QPen(color, 2))
                    poly_item.setBrush(self._get_equipment_brush(node))
                    poly_item.setZValue(2)
                    self.scene.addItem(poly_item)
                    self.polygon_items[node_id] = poly_item
                else:
                    if bbox and len(bbox) == 4:
                        bbox_key = tuple(bbox)
                        if bbox_key not in drawn_bboxes:
                            x1, y1, x2, y2 = bbox
                            rect = QGraphicsRectItem(x1, y1, x2 - x1, y2 - y1)
                            rect.setPen(QPen(color, 2))
                            rect.setBrush(self._get_equipment_brush(node))
                            rect.setZValue(2)
                            self.scene.addItem(rect)
                            self.bbox_items[node_id] = rect

                r = self.EQUIPMENT_MARKER_RADIUS
            else:
                r = self.CONNECTOR_MARKER_RADIUS

            marker = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
            marker.setPen(QPen(color, 2))
            marker.setBrush(QBrush(color.lighter(150)))
            marker.setZValue(3)
            self.scene.addItem(marker)
            self.node_items[node_id] = marker

    def _draw_single_node(self, node_id: str):
        """Отрисовать один узел (маркер + bbox/polygon для equipment)."""
        node = self.nodes.get(node_id)
        if not node:
            return

        cx, cy = node['centroid'][1], node['centroid'][0]
        node_type = node.get('type', 'connector')

        is_isolated = True
        for a, b in self.edges:
            if node_id == a or node_id == b:
                is_isolated = False
                break

        if is_isolated:
            color = self.COLOR_ISOLATED
        elif node_type == 'equipment':
            color = self.COLOR_EQUIPMENT
        else:
            color = self.COLOR_CONNECTOR

        if node_type == 'equipment':
            segmentation = node.get('segmentation')
            bbox = node.get('bbox')
            has_polygon = segmentation and isinstance(segmentation, list) and len(segmentation) >= 6

            if has_polygon:
                path = QPainterPath()
                path.moveTo(segmentation[0], segmentation[1])
                for i in range(2, len(segmentation), 2):
                    path.lineTo(segmentation[i], segmentation[i + 1])
                path.closeSubpath()

                poly_item = QGraphicsPathItem(path)
                poly_item.setPen(QPen(color, 2))
                poly_item.setBrush(self._get_equipment_brush(node))
                poly_item.setZValue(2)
                self.scene.addItem(poly_item)
                self.polygon_items[node_id] = poly_item
            elif bbox and len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                rect = QGraphicsRectItem(x1, y1, x2 - x1, y2 - y1)
                rect.setPen(QPen(color, 2))
                rect.setBrush(self._get_equipment_brush(node))
                rect.setZValue(2)
                self.scene.addItem(rect)
                self.bbox_items[node_id] = rect

        r = self.EQUIPMENT_MARKER_RADIUS if node_type == 'equipment' else self.CONNECTOR_MARKER_RADIUS
        marker = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
        marker.setPen(QPen(color, 2))
        marker.setBrush(QBrush(color.lighter(150)))
        marker.setZValue(3)
        self.scene.addItem(marker)
        self.node_items[node_id] = marker

    def remove_node_items(self, node_id: str):
        """Удалить все визуальные элементы узла. Public — для Commands."""
        if node_id in self.node_items:
            self.scene.removeItem(self.node_items[node_id])
            del self.node_items[node_id]
        if node_id in self.bbox_items:
            self.scene.removeItem(self.bbox_items[node_id])
            del self.bbox_items[node_id]
        if node_id in self.polygon_items:
            self.scene.removeItem(self.polygon_items[node_id])
            del self.polygon_items[node_id]

    def update_node_color(self, node_id: str):
        """Обновить цвет узла по подключенности. Public — для Commands."""
        if node_id not in self.node_items:
            return

        node = self.nodes.get(node_id)
        if not node:
            return

        node_type = node.get('type', 'connector')

        is_isolated = True
        for a, b in self.edges:
            if node_id == a or node_id == b:
                is_isolated = False
                break

        if is_isolated:
            color = self.COLOR_ISOLATED
        elif node_type == 'equipment':
            color = self.COLOR_EQUIPMENT
        else:
            color = self.COLOR_CONNECTOR

        marker = self.node_items[node_id]
        marker.setPen(QPen(color, 2))
        marker.setBrush(QBrush(color.lighter(150)))

        if node_id in self.polygon_items:
            self.polygon_items[node_id].setPen(QPen(color, 2))
        elif node_id in self.bbox_items:
            self.bbox_items[node_id].setPen(QPen(color, 2))

    # =================================================================
    # Hit testing
    # =================================================================

    def _get_node_bbox(self, node_id: str) -> list:
        """Виртуальный bbox: equipment → реальный, connector → CONNECTOR_MARKER_RADIUS."""
        node = self.nodes.get(node_id)
        if not node:
            return [0, 0, 0, 0]
        node_type = node.get('type', 'connector')
        bbox = node.get('bbox')
        if node_type == 'equipment' and bbox and len(bbox) == 4:
            return bbox
        cx, cy = node['centroid'][1], node['centroid'][0]
        r = self.CONNECTOR_MARKER_RADIUS
        return [cx - r, cy - r, cx + r, cy + r]

    def find_node_at(self, x: float, y: float) -> str | None:
        """Найти узел по координатам (threshold = CLICK_THRESHOLD)."""
        best_node = None
        best_dist = self.CLICK_THRESHOLD

        for node_id, node in self.nodes.items():
            cx, cy = node['centroid'][1], node['centroid'][0]
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_node = node_id

        return best_node

    def _get_edge_segments(self, edge_data: dict) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Сегменты ребра в (x, y) координатах."""
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if not sp or not tp:
            return []

        waypoints = edge_data.get('waypoints', [])
        points = [(sp[1], sp[0])]
        for wp in waypoints:
            points.append((wp[1], wp[0]))
        points.append((tp[1], tp[0]))

        return [(points[i], points[i + 1]) for i in range(len(points) - 1)]

    @staticmethod
    def _project_point_on_segment(px: float, py: float,
                                   seg_start: tuple, seg_end: tuple) -> tuple[float, float]:
        """Проекция точки на отрезок."""
        x1, y1 = seg_start
        x2, y2 = seg_end
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-9:
            return (x1, y1)
        t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / length_sq))
        return (x1 + t * dx, y1 + t * dy)

    def find_nearest_edge(self, x: float, y: float, threshold: float = 15.0
                          ) -> tuple[tuple[str, str] | None, tuple[float, float] | None]:
        """Найти ближайшее ребро к точке (x, y)."""
        best_key = None
        best_point = None
        best_dist = threshold

        for edge in self.edges_data:
            key = self.model.edge_key(edge['source'], edge['target'])

            sp = edge.get('source_point')
            tp = edge.get('target_point')
            if not sp or not tp:
                continue

            all_points = [sp, tp] + edge.get('waypoints', [])
            xs = [p[1] for p in all_points]
            ys = [p[0] for p in all_points]
            if (x < min(xs) - threshold or x > max(xs) + threshold or
                y < min(ys) - threshold or y > max(ys) + threshold):
                continue

            segments = self._get_edge_segments(edge)
            for seg_start, seg_end in segments:
                proj = self._project_point_on_segment(x, y, seg_start, seg_end)
                dist = ((x - proj[0])**2 + (y - proj[1])**2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_key = key
                    best_point = proj

        return best_key, best_point

    def find_nearest_edge_segment(self, x: float, y: float, threshold: float = 15.0
                                   ) -> tuple[tuple | None, tuple | None, int, dict | None]:
        """Найти ближайшее ребро + сегмент.

        Returns: (edge_key, projection_point, segment_index, edge_data_ref)
        """
        best_key = None
        best_point = None
        best_dist = threshold
        best_seg_idx = -1
        best_edge_data = None

        for edge in self.edges_data:
            key = self.model.edge_key(edge['source'], edge['target'])

            sp = edge.get('source_point')
            tp = edge.get('target_point')
            if not sp or not tp:
                continue

            all_points = [sp, tp] + edge.get('waypoints', [])
            xs = [p[1] for p in all_points]
            ys = [p[0] for p in all_points]
            if (x < min(xs) - threshold or x > max(xs) + threshold or
                y < min(ys) - threshold or y > max(ys) + threshold):
                continue

            segments = self._get_edge_segments(edge)
            for seg_idx, (seg_start, seg_end) in enumerate(segments):
                proj = self._project_point_on_segment(x, y, seg_start, seg_end)
                dist = ((x - proj[0])**2 + (y - proj[1])**2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_key = key
                    best_point = proj
                    best_seg_idx = seg_idx
                    best_edge_data = edge

        return best_key, best_point, best_seg_idx, best_edge_data

    # =================================================================
    # Connection geometry
    # =================================================================

    def get_connection_point(self, node_id: str, target_x: float, target_y: float) -> tuple[float, float]:
        """Connection point on node boundary toward target.

        For polygon nodes: prefer orthogonal (H/V ray), fallback nearest boundary.
        For bbox-only / connector nodes: midpoint of nearest bbox face.
        """
        node = self.nodes[node_id]
        seg = node.get('segmentation')

        if seg and isinstance(seg, list) and len(seg) >= 6:
            from ui.editors.autofix_chains import _polygon_connection_point
            poly_pts = [(seg[i], seg[i + 1]) for i in range(0, len(seg), 2)]
            bbox = node.get('bbox', [0, 0, 0, 0])
            return _polygon_connection_point(target_x, target_y, poly_pts, bbox)

        bbox = self._get_node_bbox(node_id)
        cx, cy = node['centroid'][1], node['centroid'][0]
        side = bbox_exit_side(bbox, cx, cy, target_x, target_y)
        return bbox_side_midpoint(bbox, side)

    def _closest_point_on_polygon(self, polygon: list, cx: float, cy: float,
                                   px: float, py: float) -> tuple[float, float]:
        """Точка на границе полигона где луч из центра пересекает границу."""
        n = len(polygon) // 2
        if n < 3:
            return (cx, cy)

        dx = px - cx
        dy = py - cy

        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return (polygon[0], polygon[1])

        best_point = None
        best_t = float('inf')

        for i in range(n):
            x1 = polygon[i * 2]
            y1 = polygon[i * 2 + 1]
            x2 = polygon[((i + 1) % n) * 2]
            y2 = polygon[((i + 1) % n) * 2 + 1]

            ex = x2 - x1
            ey = y2 - y1
            denom = dx * ey - dy * ex
            if abs(denom) < 1e-9:
                continue

            t = ((x1 - cx) * ey - (y1 - cy) * ex) / denom
            s = ((x1 - cx) * dy - (y1 - cy) * dx) / denom

            if t > 0 and 0 <= s <= 1:
                if t < best_t:
                    best_t = t
                    best_point = (cx + t * dx, cy + t * dy)

        if best_point:
            return best_point
        return self._closest_point_on_polygon_edge(polygon, px, py)

    def _closest_point_on_rect(self, x1: float, y1: float, x2: float, y2: float,
                                px: float, py: float) -> tuple[float, float]:
        """Ближайшая точка на границе прямоугольника."""
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        dx = px - cx
        dy = py - cy

        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return (x1, cy)

        half_w = (x2 - x1) / 2
        half_h = (y2 - y1) / 2

        t_values = []
        if abs(dx) > 1e-6:
            t_values.append(-half_w / dx)
            t_values.append(half_w / dx)
        if abs(dy) > 1e-6:
            t_values.append(-half_h / dy)
            t_values.append(half_h / dy)

        t_pos = [t for t in t_values if t > 0]
        if not t_pos:
            return (cx, cy)

        t = min(t_pos)
        bx = max(x1, min(x2, cx + dx * t))
        by = max(y1, min(y2, cy + dy * t))
        return (bx, by)

    def _closest_point_on_circle(self, cx: float, cy: float, radius: float,
                                  px: float, py: float) -> tuple[float, float]:
        """Ближайшая точка на окружности."""
        dx = px - cx
        dy = py - cy
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 1e-6:
            return (cx + radius, cy)
        return (cx + dx / dist * radius, cy + dy / dist * radius)

    def _closest_point_on_polygon_edge(self, polygon: list, px: float, py: float) -> tuple[float, float]:
        """Fallback: ближайшая точка на границе полигона."""
        n = len(polygon) // 2
        if n < 2:
            return (polygon[0], polygon[1]) if n >= 1 else (px, py)

        best_point = None
        best_dist_sq = float('inf')

        for i in range(n):
            x1 = polygon[i * 2]
            y1 = polygon[i * 2 + 1]
            x2 = polygon[((i + 1) % n) * 2]
            y2 = polygon[((i + 1) % n) * 2 + 1]

            ex, ey = x2 - x1, y2 - y1
            length_sq = ex * ex + ey * ey

            if length_sq < 1e-9:
                proj_x, proj_y = x1, y1
            else:
                t = max(0, min(1, ((px - x1) * ex + (py - y1) * ey) / length_sq))
                proj_x = x1 + t * ex
                proj_y = y1 + t * ey

            dist_sq = (px - proj_x) ** 2 + (py - proj_y) ** 2
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_point = (proj_x, proj_y)

        return best_point if best_point else (px, py)

    # =================================================================
    # Selection UI
    # =================================================================

    def select_node(self, node_id: str):
        """Выбрать узел (ring + статус)."""
        self.clear_selection()
        self.selected_node = node_id

        node = self.nodes[node_id]
        cx, cy = node['centroid'][1], node['centroid'][0]
        r = self.CLICK_THRESHOLD

        self.selection_ring = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
        self.selection_ring.setPen(QPen(self.COLOR_SELECTION, self.SELECTION_RING_WIDTH))
        self.selection_ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.selection_ring.setZValue(7)
        self.scene.addItem(self.selection_ring)

        self.update_status(f"Выбран: {node_id} ({node.get('class_name', node.get('type'))})")

    def clear_selection(self):
        """Сбросить выбор и preview."""
        self.selected_node = None
        self.hovered_edge = None

        if self.selection_ring:
            self.scene.removeItem(self.selection_ring)
            self.selection_ring = None
        if self.preview_line:
            self.scene.removeItem(self.preview_line)
            self.preview_line = None
        if self.edge_highlight:
            self.scene.removeItem(self.edge_highlight)
            self.edge_highlight = None
        if self.connector_preview:
            self.scene.removeItem(self.connector_preview)
            self.connector_preview = None

    def update_hover(self, node_id: str | None):
        """Обновить подсветку при наведении."""
        if node_id == self.hovered_node:
            return

        if self.hover_ring:
            self.scene.removeItem(self.hover_ring)
            self.hover_ring = None

        self.hovered_node = node_id

        if node_id and node_id != self.selected_node:
            node = self.nodes[node_id]
            cx, cy = node['centroid'][1], node['centroid'][0]
            r = self.CLICK_THRESHOLD - 5

            self.hover_ring = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
            self.hover_ring.setPen(QPen(self.COLOR_HOVER, 2, Qt.PenStyle.DashLine))
            self.hover_ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.hover_ring.setZValue(6)
            self.scene.addItem(self.hover_ring)

    def update_preview_line(self, mouse_x: float, mouse_y: float):
        """Preview линия от выбранного узла к курсору/цели."""
        if not self.selected_node:
            if self.preview_line:
                self.scene.removeItem(self.preview_line)
                self.preview_line = None
            return

        src = self.nodes[self.selected_node]
        x1, y1 = src['centroid'][1], src['centroid'][0]

        if self.hovered_node and self.hovered_node != self.selected_node:
            tgt = self.nodes[self.hovered_node]
            x2, y2 = tgt['centroid'][1], tgt['centroid'][0]
            edge_exists = self.model.edge_exists(self.selected_node, self.hovered_node)

            if self._current_mode == "add_edge":
                color = self.COLOR_PREVIEW_NO if edge_exists else self.COLOR_PREVIEW_OK
            else:  # delete_edge
                color = self.COLOR_PREVIEW_DELETE if edge_exists else self.COLOR_PREVIEW_NO

            pen = QPen(color, 3)
        else:
            x2, y2 = mouse_x, mouse_y
            pen = QPen(self.COLOR_SELECTION, 2, Qt.PenStyle.DashLine)

        if self.preview_line:
            self.scene.removeItem(self.preview_line)

        self.preview_line = QGraphicsLineItem(x1, y1, x2, y2)
        self.preview_line.setPen(pen)
        self.preview_line.setZValue(8)
        self.scene.addItem(self.preview_line)

    def update_connector_preview(self, mouse_x: float, mouse_y: float):
        """Preview для режима ADD_CONNECTOR."""
        if self.edge_highlight:
            self.scene.removeItem(self.edge_highlight)
            self.edge_highlight = None
        if self.connector_preview:
            self.scene.removeItem(self.connector_preview)
            self.connector_preview = None

        edge_key, proj_point = self.find_nearest_edge(mouse_x, mouse_y)
        self.hovered_edge = edge_key

        if edge_key and proj_point:
            edge_data = self.model.find_edge_data(edge_key)
            if edge_data:
                path = self._build_edge_path(
                    edge_data.get('source_point'),
                    edge_data.get('waypoints', []),
                    edge_data.get('target_point')
                )
                self.edge_highlight = QGraphicsPathItem(path)
                self.edge_highlight.setPen(QPen(self.COLOR_EDGE_HIGHLIGHT, 4))
                self.edge_highlight.setZValue(4)
                self.scene.addItem(self.edge_highlight)

            px, py = proj_point
            r = 6
            self.connector_preview = QGraphicsEllipseItem(px - r, py - r, r * 2, r * 2)
            self.connector_preview.setPen(QPen(self.COLOR_CONNECTOR_PREVIEW, 2))
            self.connector_preview.setBrush(QBrush(self.COLOR_CONNECTOR_PREVIEW))
            self.connector_preview.setZValue(5)
            self.scene.addItem(self.connector_preview)
        else:
            r = 6
            self.connector_preview = QGraphicsEllipseItem(mouse_x - r, mouse_y - r, r * 2, r * 2)
            self.connector_preview.setPen(QPen(self.COLOR_ISOLATED, 2, Qt.PenStyle.DashLine))
            self.connector_preview.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.connector_preview.setZValue(5)
            self.scene.addItem(self.connector_preview)

    # =================================================================
    # Status & Statistics
    # =================================================================

    def update_status(self, msg: str):
        """Передать статусное сообщение в callback."""
        if self.status_callback:
            self.status_callback(msg)

    def update_statistics(self):
        """Пересчитать статистику и вызвать callback."""
        if self.stats_callback:
            stats = self.model.compute_statistics()
            self.stats_callback(stats)
        self._after_statistics_update()

    def _after_statistics_update(self):
        """Хук. Base: no-op. Advanced: _update_selection_visuals()."""
        pass

    # =================================================================
    # Mode management
    # =================================================================

    def register_mode(self, name: str, handler: ModeHandler):
        """Зарегистрировать handler для режима."""
        self._mode_handlers[name] = handler

    def set_mode(self, name: str):
        """Переключить режим. Вызывает on_exit / on_enter."""
        old_handler = self._current_handler
        if old_handler:
            old_handler.on_exit(self)

        self._current_mode = name
        self._current_handler = self._mode_handlers.get(name)

        if self._current_handler:
            self._current_handler.on_enter(self)

        self.clear_selection()
        self.update_status(f"Режим: {name}")

        if self.mode_callback:
            self.mode_callback(name)

    # =================================================================
    # Undo / Redo
    # =================================================================

    def undo(self):
        desc = self.undo_mgr.undo()
        if desc:
            self.update_status(f"Undo: {desc}")
            self.update_statistics()
        else:
            self.update_status("Нечего отменять")

    def redo(self):
        desc = self.undo_mgr.redo()
        if desc:
            self.update_status(f"Redo: {desc}")
            self.update_statistics()
        else:
            self.update_status("Нечего повторять")

    # =================================================================
    # Events
    # =================================================================

    def wheelEvent(self, event):
        """Zoom."""
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = True
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif event.key() == Qt.Key.Key_Escape:
            self.clear_selection()
            self.set_mode("idle")
        elif event.key() == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.redo()
            else:
                self.undo()
        elif event.key() == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.save_graph()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = False
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            super().keyReleaseEvent(event)

    def _node_drag_allowed(self) -> bool:
        """Разрешено ли перетаскивание узлов Ctrl+ЛКМ.

        По умолчанию — да. Потомки ограничивают (например, только когда
        не активен ни один инструмент), чтобы случайно не двигать узлы.
        """
        return True

    def mousePressEvent(self, event):
        if self.ctrl_pressed and event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()

            # Проверить: есть ли узел под курсором → потенциальный drag.
            # Перетаскивание разрешено только когда нет активного инструмента
            # (иначе — отдать клик инструменту, чтобы случайно не сдвинуть узел).
            node_id = self.find_node_at(x, y)
            if node_id and self._node_drag_allowed():
                # Отложить решение: клик или drag (определим по движению)
                self._ctrl_lmb_pending = True
                self._ctrl_lmb_start_x = x
                self._ctrl_lmb_start_y = y
                self._ctrl_lmb_node = node_id
                self._ctrl_lmb_dragging = False
                event.accept()
                return
            else:
                # Нет узла или перетаскивание запрещено → сразу клик инструменту
                self._ctrl_lmb_pending = False
                if self._current_handler:
                    self._current_handler.on_press(self, x, y, event)
                return

        # Ctrl+ПКМ — универсальное удаление (узел / ребро)
        if self.ctrl_pressed and event.button() == Qt.MouseButton.RightButton:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            self._ctrl_right_click_delete(x, y)
            event.accept()
            return

        # Shift+ЛКМ (без Ctrl) — toggle выделения / rubber band
        if not self.ctrl_pressed and event.button() == Qt.MouseButton.LeftButton and \
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            pos = self.mapToScene(event.pos())
            self._on_shift_lmb_press(pos.x(), pos.y())
            event.accept()
            return

        super().mousePressEvent(event)

    def _ctrl_right_click_delete(self, x: float, y: float):
        """Ctrl+ПКМ — удалить узел или ребро под курсором. Переопределяется в потомках."""
        pass

    def _on_ctrl_lmb_click(self, x: float, y: float, node_id: str):
        """Ctrl+ЛКМ клик (без drag) на узле. Переопределяется в потомках."""
        # По умолчанию — делегировать handler
        if self._current_handler:
            self._current_handler.on_press(self, x, y, None)

    def _start_ctrl_drag(self, node_id: str):
        """Начать Ctrl+ЛКМ drag узла. Переопределяется в потомках."""
        pass

    def _update_ctrl_drag(self, x: float, y: float):
        """Обновить Ctrl+ЛКМ drag. Переопределяется в потомках."""
        pass

    def _end_ctrl_drag(self):
        """Завершить Ctrl+ЛКМ drag. Переопределяется в потомках."""
        pass

    def _on_shift_lmb_press(self, x: float, y: float):
        """Shift+ЛКМ — toggle / rubber band. Переопределяется в потомках."""
        pass

    def _on_shift_lmb_move(self, x: float, y: float):
        """Shift+ЛКМ — обновить rubber band. Переопределяется в потомках."""
        pass

    def _on_shift_lmb_release(self, x: float, y: float, event):
        """Shift+ЛКМ — завершить rubber band. Переопределяется в потомках."""
        pass

    DRAG_THRESHOLD = 5.0  # пикселей — порог различия клик/drag

    def mouseMoveEvent(self, event):
        pos = self.mapToScene(event.pos())
        x, y = pos.x(), pos.y()

        # Ctrl+ЛКМ: отложенное решение клик/drag
        if getattr(self, '_ctrl_lmb_pending', False):
            if not self._ctrl_lmb_dragging:
                dx = x - self._ctrl_lmb_start_x
                dy = y - self._ctrl_lmb_start_y
                if (dx * dx + dy * dy) ** 0.5 > self.DRAG_THRESHOLD:
                    # Порог превышен → начать drag
                    self._ctrl_lmb_dragging = True
                    self._start_ctrl_drag(self._ctrl_lmb_node)
            if self._ctrl_lmb_dragging:
                self._update_ctrl_drag(x, y)
            event.accept()
            return

        # Rubber band drag (Shift+ЛКМ)
        if getattr(self, '_rb_active', False):
            self._on_shift_lmb_move(x, y)
            event.accept()
            return

        if self._current_handler:
            if self._current_handler.on_move(self, x, y, event):
                super().mouseMoveEvent(event)
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = self.mapToScene(event.pos())
        x, y = pos.x(), pos.y()

        # Ctrl+ЛКМ: завершить drag или клик
        if getattr(self, '_ctrl_lmb_pending', False):
            self._ctrl_lmb_pending = False
            if self._ctrl_lmb_dragging:
                self._end_ctrl_drag()
                self._ctrl_lmb_dragging = False
            else:
                # Не было drag → это клик
                self._on_ctrl_lmb_click(
                    self._ctrl_lmb_start_x, self._ctrl_lmb_start_y,
                    self._ctrl_lmb_node,
                )
            event.accept()
            return

        # Rubber band release
        if getattr(self, '_rb_active', False):
            self._on_shift_lmb_release(x, y, event)
            event.accept()
            return

        if self._current_handler:
            self._current_handler.on_release(self, x, y, event)

        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """Блокировать контекстное меню — ПКМ используется для удаления/выделения."""
        event.accept()
