"""
Base Graph Editor — базовый класс редактора графа P&ID.

Рендеринг, zoom, hit testing, selection UI, event delegation.
Не содержит CRUD, оптимизацию, drag, multi-select — это в потомках.

Принцип: Base НЕ обращается к атрибутам потомков.
Вся расширяемость — через виртуальные методы и хуки.
"""

import logging
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
from ui.editors.graph_geometry import (
    boundary_projection, boundary_mark_points,
)

logger = logging.getLogger(__name__)


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

    # ── Размеры (в единицах СЦЕНЫ; подобраны под сцену=пиксели оригинала) ──
    # В canvas-режиме сцена = холст 1920x1080, и размеры берутся из _VIS_CANVAS
    # (см. _apply_visuals) — не пересчётом отсюда, а явными значениями холста.
    EQUIPMENT_MARKER_RADIUS = 6
    # ВНИМАНИЕ: два радиуса коннектора разведены намеренно.
    # CONNECTOR_MARKER_RADIUS — ГЕОМЕТРИЧЕСКИЙ: виртуальный bbox коннектора
    #   (_get_node_bbox), от него зависят посадка рёбер (source/target_point →
    #   FXML), сторона ребра и геометрия ОКР-привязки. Регуляторами не крутится.
    # CONNECTOR_DRAW_RADIUS — НАРИСОВАННЫЙ: только кружок маркера. Его и меняет
    #   ползунок «размер коннекторов» (шестерёнка), в данные не уходит.
    # Дефолты равны — поведение по умолчанию не меняется.
    CONNECTOR_MARKER_RADIUS = 8
    CONNECTOR_DRAW_RADIUS = 8
    CLICK_THRESHOLD = 20
    SELECTION_RING_WIDTH = 3
    EDGE_WIDTH = 4
    OUTLINE_WIDTH = 2          # контуры bbox/полигонов/маркеров
    HIGHLIGHT_WIDTH = 4        # подсветка ребра
    PREVIEW_WIDTH = 2          # превью коннектора
    # Длина подсвеченного участка границы вокруг точки входа трубы (П8).
    # legacy: символ ~130 px растра; canvas: фикс-боксы порядка 42x38.
    SIDE_MARK_LEN = 24

    # Размеры, зависящие от системы координат сцены
    _VIS_KEYS = (
        "EQUIPMENT_MARKER_RADIUS", "CONNECTOR_MARKER_RADIUS", "CONNECTOR_DRAW_RADIUS",
        "CLICK_THRESHOLD", "SELECTION_RING_WIDTH", "EDGE_WIDTH", "OUTLINE_WIDTH",
        "HIGHLIGHT_WIDTH", "PREVIEW_WIDTH", "SIDE_MARK_LEN",
    )

    # Ключи, которым разрешён субъективный множитель из шестерёнки (П4).
    # Строго нарисованные величины. Сюда НЕ входят:
    #   CONNECTOR_MARKER_RADIUS — геометрия (см. П0),
    #   CLICK_THRESHOLD — hit-test,
    #   EDGE_WIDTH — обязана совпадать с graph_to_fxml.LINE_STROKE_WIDTH.
    SIZE_FACTOR_KEYS = ("CONNECTOR_DRAW_RADIUS", "OUTLINE_WIDTH")

    # WYSIWYG: холст 1920x1080 — не уменьшенный оригинал, а ФИНАЛЬНАЯ система
    # координат: что оператор видит, то и уйдёт в FXML. Поэтому размеры здесь
    # заданы прямо в пикселях холста, а не пересчитаны из legacy-констант
    # (те подобраны под сцену=растр, где символ ~130px, а в холсте он 42x38).
    #
    # EDGE_WIDTH = LINE_STROKE_WIDTH из modules/graph_to_fxml.py: труба в
    # редакторе обязана быть той же толщины, что в SceneBuilder, иначе редактор
    # врёт (и расходится с render_width, который в холст идёт как есть).
    _VIS_CANVAS = {
        "EDGE_WIDTH": 2.0,               # == graph_to_fxml.LINE_STROKE_WIDTH
        "OUTLINE_WIDTH": 1.0,
        "EQUIPMENT_MARKER_RADIUS": 3.0,
        "CONNECTOR_MARKER_RADIUS": 4.0,
        "CONNECTOR_DRAW_RADIUS": 4.0,
        "CLICK_THRESHOLD": 8.0,
        "SELECTION_RING_WIDTH": 1.5,
        "HIGHLIGHT_WIDTH": 2.0,
        "PREVIEW_WIDTH": 1.0,
        "SIDE_MARK_LEN": 8.0,
    }

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
        # Legacy-размеры визуала — с учётом переопределений в потомках.
        # См. _apply_visuals: в холсте вместо них берётся _VIS_CANVAS.
        self._vis_base = {k: getattr(self, k) for k in self._VIS_KEYS}
        # Субъективные множители размеров (ползунки шестерёнки, П4): ключ → фактор.
        # Применяются ВНУТРИ _apply_visuals от базы, поэтому идемпотентны и
        # переживают переключение legacy/canvas (иначе значение, записанное
        # прямо в атрибут, откатывалось бы при каждом setup_scene).
        self._size_factors: dict[str, float] = {}
        # База размерных ключей ВНЕ _VIS_KEYS (у них свой масштаб — например
        # рамка текст-блока живёт с множителем _ocr_vis_scale).
        self._size_base_extra: dict[str, float] = {}

        # ── Graphics items ──
        self.node_items: dict[str, QGraphicsEllipseItem] = {}
        self.edge_items: dict[tuple[str, str], QGraphicsPathItem] = {}
        self.edge_label_items: dict[tuple[str, str], QGraphicsSimpleTextItem] = {}
        self.bbox_items: dict[str, QGraphicsRectItem] = {}
        self.polygon_items: dict[str, QGraphicsPathItem] = {}
        # П8: подсвеченные участки границы узла (по одному на точку входа трубы)
        self.side_items: dict[str, list[QGraphicsPathItem]] = {}
        self.show_side_marks: bool = True
        self.side_mark_color: QColor | None = None   # None → цвет узла
        # Во время массовой отрисовки рёбер участки не пересчитываем: их
        # рисует _draw_all_nodes одним проходом.
        self._side_marks_bulk: bool = False
        # Индекс узел → рёбра на время массовой отрисовки. Без него подсветка
        # всего листа была бы O(узлы × рёбра) — на 1000 узлах это удваивало
        # время сборки сцены (tools/bench/bench_scene.py).
        self._side_edge_index: dict[str, list] | None = None

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
        # Подложка = исходный растр. После авто-раскладки она перестаёт быть
        # системой отсчёта: узлы переставлены (~99 %), а лист заполняется весь,
        # тогда как растр вписан letterbox-ом и уже. Показывать её по умолчанию
        # нельзя — картинка становится нечитаемой. Ставит вкладка по флагу
        # canvas_transform.layout_applied.
        self._bg_visible: bool = True
        # Лист холста: без подложки границу листа видно не было — чёрный фон
        # сливался с пустотой за краем. Светлая тема = как в САПР и в FXML.
        self._sheet_item = None
        self._light_theme: bool = True
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

    def _apply_visuals(self, canvas: bool):
        """Размеры визуала под систему координат сцены.

        legacy → базовые константы (сцена = пиксели растра).
        canvas → _VIS_CANVAS: холст 1920x1080 это финальные пиксели FXML,
                 размеры в нём задаются, а не масштабируются.
        Значения берутся от _vis_base/_VIS_CANVAS, поэтому вызов идемпотентен.
        Поверх базы накладываются множители ползунков (_size_factors) — только
        для SIZE_FACTOR_KEYS, т.е. на нарисованные величины.
        """
        for key, base in self._vis_base.items():
            val = self._VIS_CANVAS[key] if canvas else base
            setattr(self, key, val * self._size_factors.get(key, 1.0))
        for key, base in self._size_base_extra.items():
            setattr(self, key, base * self._size_factors.get(key, 1.0))

    def set_size_factor(self, key: str, factor: float):
        """Субъективный множитель нарисованного размера (ползунок шестерёнки).

        Только визуал: в граф и FXML ничего не уходит (см. П0/П4а — геометрия
        сидит на отдельных константах). Множитель применяется от базы, поэтому
        повторные вызовы не накапливаются.
        """
        if key not in self.SIZE_FACTOR_KEYS:
            logger.warning("set_size_factor: ключ %s не размерный, игнорирую", key)
            return
        self._size_factors[key] = max(0.05, float(factor))
        self._apply_visuals(canvas=self._canvas_mode)
        self._redraw_after_size_change()

    def reset_size_factors(self):
        """Вернуть все множители размеров к 1.0 (общий сброс оформления)."""
        if not self._size_factors:
            return
        self._size_factors.clear()
        self._apply_visuals(canvas=self._canvas_mode)
        self._redraw_after_size_change()

    def _redraw_after_size_change(self):
        """Перерисовка после смены множителя. До загрузки данных — no-op
        (иначе _redraw_all снесёт заглушку пустой сцены)."""
        if not self.nodes:
            return
        self._redraw_all()

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
            logger.info(
                "setup_scene: WYSIWYG-холст %.0fx%.0f, img=%dx%d, s=%.4f, off=(%.1f,%.1f)",
                self.canvas_w, self.canvas_h, self.img_width, self.img_height,
                self._bg_scale, self._bg_offx, self._bg_offy,
            )
        else:
            # Legacy: сцена = пиксели изображения, фон 1:1 (граф в исходных координатах).
            self._bg_scale = 1.0
            self._bg_offx = self._bg_offy = 0.0
            scene_w, scene_h = self.img_width, self.img_height
            logger.info("setup_scene: legacy-режим, сцена=%dx%d (граф в исходных px)",
                        self.img_width, self.img_height)

        self._apply_visuals(canvas=self._canvas_mode)
        self._apply_theme_colors()

        # Z=-1: лист. Только в холсте: там сцена и есть лист 1920x1080, и без
        # него не видно, где он кончается (подложка эту роль больше не играет).
        self._sheet_item = None
        if self._canvas_mode:
            self._sheet_item = QGraphicsRectItem(
                QRectF(0, 0, self.canvas_w, self.canvas_h))
            self._sheet_item.setBrush(QBrush(self._sheet_color()))
            self._sheet_item.setPen(QPen(QColor("#888"), 0))
            self._sheet_item.setZValue(-1)
            self.scene.addItem(self._sheet_item)

        # Z=0: Original image (darkened)
        if (self._bg_visible and self.original_image
                and not self.original_image.isNull()):
            darkened = self.original_image.copy().convertToFormat(QImage.Format.Format_ARGB32)
            painter = QPainter(darkened)
            painter.fillRect(darkened.rect(), QColor(0, 0, 0, int(self._bg_darkness * 255)))
            painter.end()

            self._bg_item = QGraphicsPixmapItem(QPixmap.fromImage(darkened))
            self._bg_item.setScale(self._bg_scale)         # полноразмер → вписан в холст
            self._bg_item.setPos(self._bg_offx, self._bg_offy)
            self._bg_item.setZValue(0)
            self.scene.addItem(self._bg_item)
        else:
            self._bg_item = None

        # Z=1: Edges
        self._draw_all_edges()

        # Z=2-3: Nodes
        self._draw_all_nodes()

        # Запас вокруг листа, иначе сцену не подвинуть мышкой. ScrollHandDrag
        # панорамирует прокруткой, а прокрутка ограничена sceneRect: когда лист
        # целиком влезает в окно, двигать нечего — схватить и сдвинуть можно
        # было только после зума. Лист при этом вписывается как раньше: сам
        # он, а не расширенная сцена.
        pad_x, pad_y = scene_w * 0.5, scene_h * 0.5
        sheet = QRectF(0, 0, scene_w, scene_h)
        self.setSceneRect(QRectF(-pad_x, -pad_y,
                                 scene_w + 2 * pad_x, scene_h + 2 * pad_y))
        self.fitInView(sheet, Qt.AspectRatioMode.KeepAspectRatio)

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
        self.side_items.clear()
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
        self._side_marks_bulk = True
        try:
            for edge in self.edges_data:
                src, tgt = edge.get('source'), edge.get('target')
                if src in self.nodes and tgt in self.nodes:
                    key = self.model.edge_key(src, tgt)
                    self._before_edge_draw(key, edge)
                    self.create_edge_item(key, edge)
        finally:
            self._side_marks_bulk = False

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

    # ---- лист, подложка, тема ----

    def _sheet_color(self) -> QColor:
        """Цвет листа. Тёмная тема — чуть светлее пустоты, чтобы край был виден."""
        return QColor("#ffffff") if self._light_theme else QColor(52, 52, 52)

    def _apply_theme_colors(self):
        """Цвета, зависящие от темы. Узлы не трогаем: синий/зелёный/красный
        читаются и на белом, и на тёмном, а вот рёбра по умолчанию БЕЛЫЕ —
        на белом листе они бы просто исчезли."""
        if self._light_theme:
            self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))   # пустота за листом
            self.COLOR_EDGE = QColor(40, 40, 40, 200)
            self.COLOR_KKS_LABEL_BG = QColor(255, 255, 255, 190)
        else:
            self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
            self.COLOR_EDGE = QColor(255, 255, 255, 150)
            self.COLOR_KKS_LABEL_BG = QColor(0, 0, 0, 160)

    def set_light_theme(self, light: bool):
        """Светлый лист + тёмный граф (как в САПР и в FXML) или прежний тёмный."""
        light = bool(light)
        if light == self._light_theme:
            return
        self._light_theme = light
        self.setup_scene()

    def set_background_visible(self, visible: bool):
        """Показывать ли исходный растр под графом.

        После раскладки он не соответствует графу и по умолчанию скрыт; включают
        его, чтобы свериться с оригиналом и прочитать текст.
        """
        visible = bool(visible)
        if visible == self._bg_visible:
            return
        self._bg_visible = visible
        self.setup_scene()

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

    def _visual_edge_ends(self, edge_key: tuple, edge_data: dict):
        """Виртуальный. Концы ребра ДЛЯ ОТРИСОВКИ (модель не меняется).

        Base: как в данных. Advanced: подтягивает конец к границе контура, когда
        контур нарисован.
        """
        return edge_data.get('source_point'), edge_data.get('target_point')

    def create_edge_item(self, edge_key: tuple, edge_data: dict,
                         color: QColor = None) -> QGraphicsPathItem:
        """Создать визуальный элемент ребра + подпись диаметра. Public — для Commands."""
        _sp, _tp = self._visual_edge_ends(edge_key, edge_data)
        path = self._build_edge_path(_sp, edge_data.get('waypoints', []), _tp)

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

        self._refresh_side_marks_for_edge(edge_key)
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
        self._refresh_side_marks_for_edge(key)

    def _update_edge_path(self, edge_key: tuple):
        """Обновить path и pen существующего edge item + подпись."""
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data or edge_key not in self.edge_items:
            return

        _sp, _tp = self._visual_edge_ends(edge_key, edge_data)
        path = self._build_edge_path(_sp, edge_data.get('waypoints', []), _tp)
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

        self._refresh_side_marks_for_edge(edge_key)

    # =================================================================
    # Подсветка стороны блока, где есть подключение (П8)
    # =================================================================

    def set_side_marks_visible(self, visible: bool):
        """Показывать/скрывать подсветку сторон (переключатель в шестерёнке)."""
        self.show_side_marks = bool(visible)
        self._refresh_all_side_marks()

    def set_side_mark_color(self, color: QColor | None):
        """Цвет подсветки. None — брать цвет узла (поведение по умолчанию)."""
        self.side_mark_color = QColor(color) if color is not None else None
        self._refresh_all_side_marks()

    def _refresh_all_side_marks(self):
        self._build_side_edge_index()
        try:
            for node_id in list(self.nodes):
                self._draw_side_marks(node_id)
        finally:
            self._side_edge_index = None

    def _build_side_edge_index(self):
        """Индекс узел → инцидентные рёбра (на время массового прохода)."""
        index: dict[str, list] = {}
        for edge in self.edges_data:
            for nid in (edge.get('source'), edge.get('target')):
                if nid is not None:
                    index.setdefault(nid, []).append(edge)
        self._side_edge_index = index

    def _refresh_side_marks_for_edge(self, edge_key: tuple):
        """Пересчитать участки у обоих концов ребра.

        Точечных путей пересчёта рёбер много (undo/redo команд, батч-drag, тяга
        конца, autofix, вставка буфера, живой resize), но все они проходят через
        create_edge_item / _update_edge_path / remove_edge_item — хук здесь, а не
        в каждом из них.
        """
        if self._side_marks_bulk or not edge_key:
            return
        for node_id in edge_key:
            if node_id in self.nodes:
                self._draw_side_marks(node_id)

    def _node_outline_points(self, node_id: str):
        """(замкнутая полилиния, обрезать_по_грани) НАРИСОВАННОЙ формы узла.

        Берётся то, что реально на экране: у скинового узла в холсте контур не
        рисуется (_draws_polygon), там форма = bbox. Иначе штрих лёг бы на
        невидимый контур.
        Returns (None, False) — у узла нет нарисованной формы (коннектор).
        """
        node = self.nodes.get(node_id)
        if not node or node.get('type') != 'equipment':
            return None, False
        if self._node_has_polygon(node):
            seg = node.get('segmentation')
            return [(seg[i], seg[i + 1]) for i in range(0, len(seg) - 1, 2)], False
        bb = node.get('bbox')
        if bb and len(bb) == 4:
            x1, y1, x2, y2 = bb
            # Бокс — обрезаем по грани: у мелкого бокса штрих иначе завернёт за угол.
            return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], True
        return None, False

    def _node_entry_points(self, node_id: str) -> list:
        """Точки входа труб в узел (x, y) — по НАРИСОВАННЫМ концам рёбер.

        В холсте конец для отрисовки не равен точке в данных (_visual_edge_ends
        подтягивает его к границе контура) — берём именно его, иначе штрих
        встанет мимо видимой трубы. Фолбэк на get_connection_point — для старых
        графов без source_point/target_point.
        """
        points = []
        if self._side_edge_index is not None:
            incident = self._side_edge_index.get(node_id, ())
        else:
            incident = [e for e in self.edges_data
                        if node_id in (e.get('source'), e.get('target'))]
        for edge in incident:
            src, tgt = edge.get('source'), edge.get('target')
            key = self.model.edge_key(src, tgt)
            sp, tp = self._visual_edge_ends(key, edge)
            pt = sp if node_id == src else tp
            if pt and len(pt) == 2:
                points.append((float(pt[1]), float(pt[0])))   # [y, x] → (x, y)
                continue
            partner = self.nodes.get(tgt if node_id == src else src)
            if partner and partner.get('centroid'):
                points.append(self.get_connection_point(
                    node_id, partner['centroid'][1], partner['centroid'][0]))
        return points

    def _remove_side_marks(self, node_id: str):
        for item in self.side_items.pop(node_id, []):
            if item.scene() is not None:
                self.scene.removeItem(item)

    def _draw_side_marks(self, node_id: str):
        """Перерисовать участки границы узла вокруг точек входа труб."""
        self._remove_side_marks(node_id)
        if not self.show_side_marks:
            return
        outline, clip = self._node_outline_points(node_id)
        if not outline:
            return
        entries = self._node_entry_points(node_id)
        if not entries:
            return

        # Цвет узла: раз точки входа есть, узел заведомо не изолирован, значит
        # COLOR_ISOLATED тут недостижим. Проверять связность перебором рёбер
        # нельзя — это O(узлы × рёбра) на весь лист.
        color = self.side_mark_color or self.COLOR_EQUIPMENT
        pen = QPen(color, self.OUTLINE_WIDTH * 2.5)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        items = []
        seen = set()
        for px, py in entries:
            _i, _t, cx, cy = boundary_projection(outline, px, py)
            key = (round(cx, 3), round(cy, 3))
            if key in seen:
                continue          # два ребра в одну точку — один участок
            seen.add(key)
            pts = boundary_mark_points(outline, px, py, self.SIDE_MARK_LEN, clip)
            if len(pts) < 2:
                continue
            path = QPainterPath()
            path.moveTo(pts[0][0], pts[0][1])
            for x, y in pts[1:]:
                path.lineTo(x, y)
            item = QGraphicsPathItem(path)
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            # Между рамкой узла (2) и маркером-центроидом (3).
            item.setZValue(2.5)
            self.scene.addItem(item)
            items.append(item)
        if items:
            self.side_items[node_id] = items

    # =================================================================
    # Node rendering
    # =================================================================

    def _draws_polygon(self, node: dict) -> bool:
        """Виртуальный. Рисовать ли контур (segmentation) как форму узла.

        Base: да, если контур есть.
        Advanced: при включённых скинах у скиновых узлов форма = фикс-бокс + скин
        (в FXML контур игнорируется — skin_info в приоритете).
        """
        return True

    def _node_has_polygon(self, node: dict) -> bool:
        """Форма узла = контур? (учитывает _draws_polygon, поэтому bbox-ветка
        включается там, где контур не рисуется)."""
        seg = node.get('segmentation')
        if not (seg and isinstance(seg, list) and len(seg) >= 6):
            return False
        return self._draws_polygon(node)

    def _draw_all_nodes(self):
        """Отрисовка всех узлов."""
        self._build_side_edge_index()   # см. _node_entry_points: иначе O(N×E)
        try:
            self._draw_all_nodes_inner()
        finally:
            self._side_edge_index = None

    def _draw_all_nodes_inner(self):
        connected_nodes = set()
        for a, b in self.edges:
            connected_nodes.add(a)
            connected_nodes.add(b)

        # Собираем bbox с полигонами (для дедупликации)
        drawn_bboxes: set[tuple] = set()
        for node_id, node in self.nodes.items():
            if node.get('type') != 'equipment':
                continue
            bbox = node.get('bbox')
            if self._node_has_polygon(node) and bbox:
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
                has_polygon = self._node_has_polygon(node)

                if has_polygon:
                    path = QPainterPath()
                    path.moveTo(segmentation[0], segmentation[1])
                    for i in range(2, len(segmentation), 2):
                        path.lineTo(segmentation[i], segmentation[i + 1])
                    path.closeSubpath()

                    poly_item = QGraphicsPathItem(path)
                    poly_item.setPen(QPen(color, self.OUTLINE_WIDTH))
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
                            rect.setPen(QPen(color, self.OUTLINE_WIDTH))
                            rect.setBrush(self._get_equipment_brush(node))
                            rect.setZValue(2)
                            self.scene.addItem(rect)
                            self.bbox_items[node_id] = rect

                r = self.EQUIPMENT_MARKER_RADIUS
            else:
                r = self.CONNECTOR_DRAW_RADIUS

            marker = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
            marker.setPen(QPen(color, self.OUTLINE_WIDTH))
            marker.setBrush(QBrush(color.lighter(150)))
            marker.setZValue(3)
            self.scene.addItem(marker)
            self.node_items[node_id] = marker

            self._draw_side_marks(node_id)

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
            has_polygon = self._node_has_polygon(node)

            if has_polygon:
                path = QPainterPath()
                path.moveTo(segmentation[0], segmentation[1])
                for i in range(2, len(segmentation), 2):
                    path.lineTo(segmentation[i], segmentation[i + 1])
                path.closeSubpath()

                poly_item = QGraphicsPathItem(path)
                poly_item.setPen(QPen(color, self.OUTLINE_WIDTH))
                poly_item.setBrush(self._get_equipment_brush(node))
                poly_item.setZValue(2)
                self.scene.addItem(poly_item)
                self.polygon_items[node_id] = poly_item
            elif bbox and len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                rect = QGraphicsRectItem(x1, y1, x2 - x1, y2 - y1)
                rect.setPen(QPen(color, self.OUTLINE_WIDTH))
                rect.setBrush(self._get_equipment_brush(node))
                rect.setZValue(2)
                self.scene.addItem(rect)
                self.bbox_items[node_id] = rect

        r = self.EQUIPMENT_MARKER_RADIUS if node_type == 'equipment' else self.CONNECTOR_DRAW_RADIUS
        marker = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
        marker.setPen(QPen(color, self.OUTLINE_WIDTH))
        marker.setBrush(QBrush(color.lighter(150)))
        marker.setZValue(3)
        self.scene.addItem(marker)
        self.node_items[node_id] = marker

        self._draw_side_marks(node_id)

    def remove_node_items(self, node_id: str):
        """Удалить все визуальные элементы узла. Public — для Commands."""
        self._remove_side_marks(node_id)
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
        marker.setPen(QPen(color, self.OUTLINE_WIDTH))
        marker.setBrush(QBrush(color.lighter(150)))

        if node_id in self.polygon_items:
            self.polygon_items[node_id].setPen(QPen(color, self.OUTLINE_WIDTH))
        elif node_id in self.bbox_items:
            self.bbox_items[node_id].setPen(QPen(color, self.OUTLINE_WIDTH))

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
        """Точка подключения на узле, обращённая к (target_x, target_y).

        Э1: посадка канонична — `modules/graph/core/seating` (тот же модуль,
        что сажает выход раскладки). Э2e: это ПРЕДВАРИТЕЛЬНАЯ посадка —
        все боевые пути «Ручной правки» доводят её движком
        (`_engine_finish_ends` / `_seat_end_ported`: порт/слот, угол
        непредставим); бесконтекстный движок здесь собирал бы веер в
        середину (T3-тесты) — канон с проекцией к партнёру честнее."""
        from modules.graph.core import seating

        node = self.nodes[node_id]
        byid = {
            "__self__": node,
            "__toward__": {"id": "__toward__", "type": "connector",
                           "centroid": [float(target_y), float(target_x)]},
        }
        probe = {"source": "__self__", "target": "__toward__",
                 "source_point": None, "target_point": None, "waypoints": []}
        seating.reseat_edge(byid, probe)
        sp = probe["source_point"]                    # [y, x]
        return (sp[1], sp[0])

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
                self.edge_highlight.setPen(QPen(self.COLOR_EDGE_HIGHLIGHT, self.HIGHLIGHT_WIDTH))
                self.edge_highlight.setZValue(4)
                self.scene.addItem(self.edge_highlight)

            px, py = proj_point
            r = 6
            self.connector_preview = QGraphicsEllipseItem(px - r, py - r, r * 2, r * 2)
            self.connector_preview.setPen(QPen(self.COLOR_CONNECTOR_PREVIEW, self.PREVIEW_WIDTH))
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

    def focusOutEvent(self, event):
        """Ушёл фокус — снять защёлку Ctrl и оборвать незавершённый drag.

        `ctrl_pressed` ставится в keyPressEvent и снимается только в
        keyReleaseEvent. При alt-tab отпускание клавиши уходит другому окну,
        защёлка остаётся взведённой, и следующий же клик по холсту начинает
        перетаскивание узла — оператор об этом не просил.
        """
        self._reset_drag_state()
        super().focusOutEvent(event)

    def leaveEvent(self, event):
        self._reset_drag_state()
        super().leaveEvent(event)

    def _reset_drag_state(self):
        if getattr(self, '_ctrl_lmb_dragging', False):
            self._end_ctrl_drag()
        self._ctrl_lmb_dragging = False
        self._ctrl_lmb_pending = False
        self.ctrl_pressed = False
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        # Модификатор события — истина, защёлка `ctrl_pressed` — лишь эхо:
        # она переживает alt-tab, а `event.modifiers()` всегда актуален.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.ctrl_pressed = True
        elif event.button() == Qt.MouseButton.LeftButton:
            self.ctrl_pressed = False

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

        # ЗАЛИПШИЙ DRAG. У вьюпорта включён setMouseTracking, поэтому move
        # приходит и с ОТПУЩЕННОЙ кнопкой. Если release потерялся (клик мимо
        # окна, alt-tab, модальный диалог), узел ехал за курсором дальше и
        # уезжал на сотни px — молча, без единого жеста оператора. Замер на
        # боевом c2f79462: так сдвинуло 5 узлов (до 305 px), автосейв записал
        # это на сервер, и 1 косое ребро на листе стало 9.
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            if getattr(self, '_ctrl_lmb_dragging', False):
                self._end_ctrl_drag()
            self._ctrl_lmb_dragging = False
            self._ctrl_lmb_pending = False

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
