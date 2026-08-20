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
    QToolTip, QGraphicsItemGroup, QGraphicsPolygonItem,
)
from PySide6.QtGui import (
    QColor, QBrush, QPen, QPainterPath, QFont, QPixmap, QTransform, QCursor,
    QPolygonF, QImage,
)
from PySide6.QtCore import Qt, QPointF

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
    bbox_exit_side, closest_bbox_side,
    project_point_to_bbox_border, project_point_to_polygon_border,
    get_node_geometry, compute_edge_perpendicularity,
    node_orientation_by_edges,
)
from ui.editors.edge_routing import route_edge as route_edge_v2, segment_intersects_bbox
from ui.editors.autofix_chains import auto_fix_graph
from ui.editors.ocr_layer_mixin import (
    OcrLayerMixin, AddOcrBlockHandler, OcrBindHandler,
)
from ui.editors.mode_handlers.base_handler import ModeHandler


# Стрелка потока: в FXML это Polygon-треугольник, а не скин (generate_fxml_triangle),
# поэтому PNG для класса нет — рисуем вектором. Обводка — как в FXML.
_NAPRAVLENIE_CLASS = "napravlenie"
_NAPRAVLENIE_STROKE = "#333333"


def _edge_path_pts(e: dict) -> list | None:
    """Полный путь ребра в (x, y) для route_edge_v2 или None без концов."""
    sp, tp = e.get('source_point'), e.get('target_point')
    if not sp or not tp:
        return None
    return ([(sp[1], sp[0])]
            + [(w[1], w[0]) for w in e.get('waypoints', [])]
            + [(tp[1], tp[0])])


def _pts_bbox(pts: list) -> tuple:
    """Габарит полилинии (x1, y1, x2, y2)."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _seg_dist2(px: float, py: float,
               ax: float, ay: float, bx: float, by: float) -> float:
    """Квадрат расстояния точки (px,py) до отрезка (ax,ay)-(bx,by)."""
    vx, vy = bx - ax, by - ay
    d2 = vx * vx + vy * vy
    if d2 <= 1e-9:
        dx, dy = px - ax, py - ay
        return dx * dx + dy * dy
    t = ((px - ax) * vx + (py - ay) * vy) / d2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return dx * dx + dy * dy


def _node_poly_contour(node: dict) -> list | None:
    """Плоский контур [x, y, ...] полигонного узла БЕЗ скина, иначе None.

    Та же ветка, что канон посадки (seating._anchor_rect/node_anchor):
    segmentation >= 3 точек, class_name вне FIXED_SIZES. У такого узла
    габарит невыпуклого контура почти вдвое больше фигуры (докстринг
    modules/graph/core/layout/_shapes.py; репро — деаэратор node_28,
    bbox 616x373 при заполненности ~0.54) — судить его bbox'ом нельзя,
    только реальной формой."""
    from modules.graph.core import seating

    seg = node.get('segmentation')
    if seg and isinstance(seg, list) and len(seg) >= 6 \
            and node.get('class_name') not in seating.FIXED_SIZES:
        return seg
    return None


# Э0 пересборки: полигонные предикаты уехали в канон судьи
# modules/graph/core/edit_checks.py (правило «сторож == судья»).
# Здесь остаются алиасы — их импортируют тесты и внутренние вызовы.
from modules.graph.core.edit_checks import (  # noqa: E402
    pt_in_polygon as _pt_in_polygon,
    seg_pierces_polygon as _seg_pierces_polygon,
)


class EditEdgeDashHandler(ModeHandler):
    """Режим переключения ПУНКТИРА ребра.

    Ctrl+ЛКМ по ребру → переключить пунктирный стиль (edge_data['dashed']).
      • если ребро входит в обводку (shift+протяжка) — переключаются все обведённые;
      • иначе — только это ребро.
    Ctrl+ПКМ по обведённому ребру → убрать его из обводки.
    """

    def on_enter(self, ed):
        # Чистый старт: режим работает только с рёбрами
        ed.clear_multi_select()

    def on_press(self, ed, x, y, event):
        edge_key, _ = ed.find_nearest_edge(x, y, threshold=20.0)
        if edge_key:
            ed.apply_edge_style_at(edge_key, kind="dash")
        else:
            ed.update_status("Ctrl+ЛКМ по ребру — пунктир. Shift+протяжка — обвести рёбра.")
        return True

    def on_move(self, ed, x, y, event):
        ed.update_edge_style_preview(x, y)
        return True


class AdvancedGraphEditor(OcrLayerMixin, SimpleGraphEditor):
    """Полный редактор: routing, оптимизация, drag, multi-select, waypoints, auto-fix.

    Плюс OCR-слой (OcrLayerMixin): текст-блоки и их привязка к узлам/рёбрам,
    активные в состоянии display_regime == 'ocr'.
    """

    # + рамка слоя ОКР (текст-блоки и рамки ОКР-объектов) — своя ручка.
    SIZE_FACTOR_KEYS = SimpleGraphEditor.SIZE_FACTOR_KEYS + ("OCR_BORDER_W",)

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
        self.register_mode("edit_edge_dash", EditEdgeDashHandler())
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

        # ── Копипаст подсистемы (Ctrl+C/Ctrl+V в базовом состоянии) ──
        # Буфер: {"nodes": [...], "edges": [...], "texts": [(block, binding), ...]}
        self._node_clipboard: dict = {}
        # Призрак вставки (Ctrl+V → следует за мышью → Ctrl+ЛКМ фиксирует):
        # {"kind": "nodes"|"ocr", "group": QGraphicsItemGroup, "center": (cx, cy)}
        self._paste_ghost: dict | None = None
        # Цель отложенного Ctrl+ЛКМ-клика для расширения выделения
        # (equipment по bbox / ребро — find_node_at их не видит): ("node", id) | ("edge", key)
        self._ext_click_target: tuple | None = None

        # ── Waypoint / Endpoint ──
        self.waypoint_markers: dict[tuple[str, str], list] = {}
        self.dragging_waypoint: tuple | None = None
        self.dragging_wp_start: list | None = None
        self._wp_ghost_marker: QGraphicsRectItem | None = None
        self._endpoint_markers: dict[tuple, list] = {}
        self._dragging_endpoint: tuple | None = None
        self._dragging_ep_start_side: str | None = None
        # Этап A (портовая модель): маркеры портов на время drag конца ребра
        # + «конец сейчас прилип к порту» (на отпускании в свободном месте
        # границы рождается постоянный ручной порт узла).
        self._port_markers: list = []
        self._ep_on_port: bool = False

        # ── Drag ──
        self.dragging_node: str | None = None
        self.drag_start_centroid: list | None = None
        self.drag_start_bbox: list = []
        self.drag_start_segmentation: list = []
        self.drag_start_edge_points: dict = {}
        # Э3: bbox привязанных текст-блоков на старте drag — блоки едут за
        # узлом, undo обязан вернуть и их.
        self.drag_start_block_bboxes: dict = {}
        self._batch_drag: bool = False
        self._batch_internal_edges: list = []
        self._batch_boundary_edges: list = []
        self._batch_bound_blocks: list = []
        # Э5: рёбра, которые ведёт текущий жест, — ВСЕ инцидентные ему.
        # Геометрия производная: waypoints — кэш последнего расчёта, drag
        # вправе перестраивать любой маршрут; закреплён только вход с пином
        # (edge['pin_source'|'pin_target'], seat_end сажает в него).
        self._drag_routable_edges: set = set()
        # Э7-перф: кэш жеста для входов route_edge_v2 (bbox'ы узлов + пути
        # рёбер собираются один раз в start_drag_node; двигающиеся узлы и
        # инцидентные им рёбра читаются свежими на каждом кадре).
        self._drag_route_ctx: dict | None = None
        # Э3-амнистия: база «что труба уже пересекала» закрепляется на
        # ВХОДЕ жеста (кэш edge_key -> set узлов); плавающая база по
        # текущему кадру легализовала лишние боксы (репро 222222)
        self._amnesty_cache: dict = {}
        # Этап B: оконная libavoid-сессия жеста (edit_avoid.AvoidDragSession)
        # или None — тогда кадры роутит самописная лестница (запасной путь).
        self._avoid_session = None
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
        self._resize_pin_base: list = []           # [(edge, role, dx, dy)] до превью
        self._resize_pin_preview: list = []        # [(dx, dy) | None] — что ВПИСАЛО превью
        self._resize_model_rev = None              # undo_mgr.revision на момент базлайна
        self._resize_preview_geom: dict = {}       # node_id → что ВПИСАЛО превью
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
        self._skin_items: dict[str, list] = {}        # node_id → [pixmap_item]

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

    def _apply_visuals(self, canvas: bool):
        """+ стартовый размер кисти = базовая толщина трубы в этой системе координат.

        В __init__ edge_brush_size взят из legacy-константы (система координат
        тогда ещё неизвестна) — здесь, в setup_scene, она уже определена.
        """
        super()._apply_visuals(canvas)
        self.edge_brush_size = self.EDGE_WIDTH

    def _get_edge_color(self, edge_data: dict, key: tuple = None) -> QColor:
        """Цвет ребра — зависит от активного режима.

        style → индивидуальный цвет (render_color) или белый по умолчанию;
        perp  → оранжевый для неперпендикулярных, иначе белый;
        ocr   → красный для рёбер без диаметра, иначе белый.

        Индивидуальный цвет показывается в режиме style (там его правят) и при
        включённых скинах: скин — предпросмотр FXML, а цвет линии это тот же скин,
        но для ребра. В FXML экспортируется всегда.
        """
        rc = edge_data.get('render_color')
        if rc and (self.display_regime == "style" or self.show_skins):
            return QColor(rc)

        if self.display_regime == "style":
            return self.COLOR_EDGE

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

    @staticmethod
    def _node_has_skin(node: dict) -> bool:
        """Есть ли у узла скин — тем же правилом, что в FXML (смотрит и class_id)."""
        from modules.graph_to_fxml import get_skin_info
        return get_skin_info(node) is not None

    def _node_geometry(self, node: dict) -> dict:
        """Геометрия узла для РАСЧЁТА рёбер — форма, которая уйдёт в FXML.

        get_node_geometry отдаёт приоритет полигону, но у скинового узла в FXML
        полигон игнорируется: узел эмитится контролом в своём bbox. Считать по
        контуру нельзя — точки подключения уедут с символа (autofix/перетаскивание
        сажали их на границу контура 62x68, тогда как скин живёт в боксе 42x38).

        От show_skins НЕ зависит: режим отображения не должен менять данные.
        """
        if self._canvas_mode and node and self._node_has_skin(node):
            bb = node.get('bbox')
            if bb and len(bb) == 4:
                return {'type': 'bbox', 'data': bb}
        return get_node_geometry(node)

    def _draws_polygon(self, node: dict) -> bool:
        """Скины включены → у скинового узла контур не рисуем.

        В FXML skin_info имеет приоритет над segmentation: узел со скином
        эмитится контролом в своём bbox, а контур игнорируется. Показывать его
        при включённых скинах значит рисовать форму, которой в SceneBuilder не
        будет (и рёбра, честно посаженные на границу bbox, выглядят «внутри»).
        Скины выключены → контур виден как есть: это рабочий слой, не предпросмотр.
        """
        if not self.show_skins:
            return True
        return not self._node_has_skin(node)

    # Э1 пересборки «экран == данные»: отрисовочная доводка лучом
    # (_contour_endpoint) удалена. Полилиния рисуется строго
    # sp + waypoints + tp (база _visual_edge_ends). Неканоничные концы
    # старых файлов материализуются В ДАННЫЕ один раз при открытии холста
    # (_materialize_ray_ends в base_graph_tab) — доводка была ещё и
    # display-зависимой (_node_has_polygon смотрел на show_skins): картинка
    # менялась от настройки отображения при неизменном файле.

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
        state_modes = ("edit_edge_color", "edit_edge_size", "edit_edge_dash",
                       "optimize_edge", "edit_waypoint", "add_ocr_block", "ocr_bind")
        if self._current_mode in state_modes:
            self.set_mode("idle")
        # KKS-подписи показываются только в режиме 'ocr'
        if regime != "ocr":
            self._hide_all_kks_labels()
            if hasattr(self, "_selected_ocr"):
                self._selected_ocr.clear()
            # Снять ручки изменения размера текст-блока (актуальны только в 'ocr').
            if hasattr(self, "_hide_ocr_block_resize"):
                self._hide_ocr_block_resize()
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
        Флаг edge_data['dashed'] делает ребро пунктирным во всех режимах.

        Как и цвет, индивидуальная толщина видна в режиме style и при включённых
        скинах (предпросмотр FXML, куда она уходит всегда).
        """
        color = self._get_edge_color(edge_data, key)

        render_width = edge_data.get('render_width')
        if render_width and (self.display_regime == "style" or self.show_skins):
            pen_width = float(render_width)
        elif (self.display_regime == "perp" and key
                and key in self.edge_perp_scores
                and not self.edge_perp_scores[key].get('is_good', True)):
            pen_width = self.EDGE_WIDTH + 1
        else:
            pen_width = self.EDGE_WIDTH

        pen = QPen(color, pen_width)
        if edge_data.get('dashed'):
            pen.setStyle(Qt.PenStyle.DashLine)
        return pen

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
                source_geom = self._node_geometry(self.nodes[edge['source']])
                target_geom = self._node_geometry(self.nodes[edge['target']])
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
        self._port_markers.clear()
        self._dragging_endpoint = None
        self.dragging_waypoint = None
        self._auto_fix_result = None
        # OCR-слой: сцена уже очищается в Base; сбросить ссылки на item'ы.
        if hasattr(self, "_ocr_block_items"):
            self._ocr_block_items.clear()
            self._ocr_hl_restore = []
        # Призрак вставки не переживает перерисовку сцены (item'ы удалены).
        if getattr(self, "_paste_ghost", None) is not None:
            self._paste_ghost = None
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
        """Добавить ребро; посадка концов — канон `modules/graph/core/seating`.

        Э1-хвост: прежний дубль-контракт (connect_* из graph_geometry) заменён
        каноном reseat_edge — коннектор = жёстко центроид, FIXED_SIZES-скин =
        граница _skin_content_rect (и приоритетнее полигона), полигон = луч в
        контур, bbox = грань.

        Э6/Э7-a: если посаженное каноном ребро НЕ прямое (осевое расхождение
        концов >= snap_threshold — гистерезис, см. _route_orthogonal), вместо
        косой диагонали строится ортогональный L/Z-маршрут (требование
        заказчика 2026-07-31); после маршрута концы пересаживаются повторным
        reseat_edge — для ребра С waypoints канон сажает конец по оси
        подводящего сегмента (_seg_lock), посадка остаётся каноничной
        (матрица T-B tests/test_seat_contract.py). «Минимум пересечений труб»
        здесь не реализуется — см. докстринг _route_orthogonal (Э7-b).
        """
        if node_a == node_b:
            self.update_status("Нельзя соединить узел с самим собой")
            return False

        key = self.model.edge_key(node_a, node_b)
        if key in self.edges:
            self.update_status(f"Ребро уже существует: {node_a} — {node_b}")
            return False

        from modules.graph.core import seating

        probe = {'source': node_a, 'target': node_b,
                 'source_point': None, 'target_point': None, 'waypoints': []}
        seating.reseat_edge(self.nodes, probe)
        self._engine_finish_ends(probe)          # Э2c: порт/слот, не угол
        if self._route_orthogonal(probe):
            # концы на осях подводящих сегментов, в портах движка
            self._engine_finish_ends(probe)
        sp, tp = probe['source_point'], probe['target_point']
        src_x, src_y = sp[1], sp[0]
        tgt_x, tgt_y = tp[1], tp[0]
        connection_type = "seating"

        edge_data = self.model.create_edge_data(
            node_a, node_b,
            source_point=[src_y, src_x],
            target_point=[tgt_y, tgt_x],
        )
        if probe['waypoints']:
            edge_data['waypoints'] = [list(wp) for wp in probe['waypoints']]
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
        # Э2c: канонная посадка доводится движком (порт/слот, не угол);
        # waypoints оператора неприкосновенны — полигонная ветка сдвигает
        # только смежное колено при перпендикулярном стабе
        probe = {'source': node_a, 'target': node_b,
                 'source_point': [src_y, src_x], 'target_point': [tgt_y, tgt_x],
                 'waypoints': [wp.copy() for wp in waypoints]}
        self._engine_finish_ends(probe)
        src_y, src_x = probe['source_point']
        tgt_y, tgt_x = probe['target_point']
        waypoints = probe['waypoints']

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

        # Э1: ось выравнивания и концы — от канона посадки: `reseat_edge` сам
        # выводит замок H/V из реальной геометрии пары (НЕ из старого
        # направления ребра) и сажает концы канонично (коннектор = центроид,
        # FIXED_SIZES-скин = граница content-rect, полигон = контур).
        # Probe без waypoints — оптимизация, как и раньше, стирает маршрут
        # (OptimizeEdgeCommand.execute ставит waypoints=[]).
        from modules.graph.core import seating

        probe = {'source': original_source_id, 'target': original_target_id,
                 'source_point': None, 'target_point': None, 'waypoints': []}
        seating.reseat_edge(self.nodes, probe)
        self._engine_finish_ends(probe)          # Э2c: порт/слот, не угол
        new_sp = probe['source_point']
        new_tp = probe['target_point']
        src_x, src_y = new_sp[1], new_sp[0]
        tgt_x, tgt_y = new_tp[1], new_tp[0]

        cmd = OptimizeEdgeCommand(
            self.model, self,
            original_source_id, original_target_id,
            old_sp, old_tp, old_wp,
            new_sp, new_tp,
        )
        self.undo_mgr.execute(cmd)

        # Пересчитываем перпендикулярность
        source_geom = self._node_geometry(self.nodes[original_source_id])
        target_geom = self._node_geometry(self.nodes[original_target_id])
        perp_info = compute_edge_perpendicularity(
            (src_x, src_y), (tgt_x, tgt_y), source_geom, target_geom)
        self.edge_perp_scores[key] = perp_info

        # ВАЖНО: перерисовать ребро ПОСЛЕ обновления perp_scores
        # (cmd.execute уже вызвал _update_edge_path, но с СТАРЫМИ scores)
        # Как в оригинале: удалить + создать заново с правильным цветом
        self.remove_edge_item(key)
        edge_data = self.model.find_edge_data(key)
        if edge_data:
            self.create_edge_item(key, edge_data)

        self.update_status(f"Оптимизировано: {original_source_id} — {original_target_id} (score: {perp_info['score']:.2f})")
        return True

    def optimize_all_edges(self) -> int:
        """Оптимизировать все неперпендикулярные рёбра."""
        optimized = 0
        edges_to_optimize = [
            key for key, info in self.edge_perp_scores.items()
            if not info.get('is_good', True)
        ]
        if edges_to_optimize:
            # Точка возврата строится ниже — снять превью ДО неё (см.
            # drop_uncommitted_preview). Пустой список команд не даёт:
            # вваривать нечего, значит и картинку у оператора не отбираем.
            self.drop_uncommitted_preview()
        for node_a, node_b in edges_to_optimize:
            if self.optimize_edge(node_a, node_b):
                optimized += 1
        self.update_status(f"Оптимизировано {optimized} рёбер из {len(edges_to_optimize)}")
        self.update_statistics()
        return optimized

    def smooth_canvas(self, budget=None, allow_node_shift=True) -> dict:
        """Э4: адресное сглаживание после ручных правок.

        Решение заказчика 2026-08-02: сгладить мелкие зигзаги и диагонали;
        двигать оборудование «можно, но ограниченно». Алгоритм и пороги —
        `modules/graph/core/edit_smooth` (там же замеры, которыми они
        выбраны). Здесь только связка: движку отдаются РЕАЛЬНАЯ лестница
        маршрутов редактора и мини-жест пересадки, чтобы своей геометрии
        он не изобретал.

        Один шаг undo на всю операцию (SnapshotCommand): оператор жмёт
        кнопку — и одним Ctrl+Z возвращает лист как был.
        """
        from modules.graph.core import edit_smooth
        from ui.editors.undo_manager import SnapshotCommand

        # Незафиксированное превью «Размеров» — ДО снятия точки возврата,
        # иначе оно вваривается в _before и перестаёт откатываться
        # (см. drop_uncommitted_preview).
        self.drop_uncommitted_preview()
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd.description = "Сглаживание"
        cmd.execute()

        def route_fn(edge_data):
            key = self.model.edge_key(edge_data.get('source'),
                                      edge_data.get('target'))
            live = self.model.find_edge_data(key)
            if live is None:
                return False
            ok = self._route_orthogonal(live)
            self._update_edge_path(key)
            return ok

        prev_ctx, prev_routable = self._drag_route_ctx, self._drag_routable_edges
        self._drag_route_ctx = None          # вне жеста: полный набор препятствий
        self._drag_routable_edges = set()
        self._amnesty_cache = {}
        try:
            stats = edit_smooth.smooth(
                self.model.graph_data,
                route_fn=route_fn,
                reseat_fn=self._reseat_after_resize,
                budget=edit_smooth.BUDGET if budget is None else float(budget),
                allow_node_shift=allow_node_shift,
            )
        finally:
            self._drag_route_ctx = prev_ctx
            self._drag_routable_edges = prev_routable
            self._amnesty_cache = {}
            # Закрытие шага undo — тоже в finally: движок мутирует граф НА
            # МЕСТЕ, и его падение посреди лестницы оставляло оператора с
            # изменённым холстом и без Ctrl+Z (шаг открыт execute() выше, но
            # не закрыт). Порядок тот же, что на зелёном пути.
            self.model.rebuild_edge_data_index()
            self._redraw_all()
            cmd.finalize()
            self.undo_mgr.push_executed(cmd)

        self.update_status(
            f"Сглаживание: колен {stats['колено']}, изломов "
            f"{stats['излом']}, скольжений {stats['скольжение']}, "
            f"коннекторов {stats['коннектор']}, узлов {stats['узел']}; "
            f"осталось {stats['осталось']}")
        self.update_statistics()
        return stats

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

        Посадка концов — канон `modules/graph/core/seating` (Э1); маршрут
        (решение о waypoints + route_edge_v2) — прежняя логика из
        graph_editor.py:2497-2595.
        """
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

        # Э1: посадка концов — канон `seating.reseat_edge` (коннектор = жёстко
        # центроид без виртуального bbox r=CONNECTOR_MARKER_RADIUS, FIXED_SIZES-
        # скин = граница content-rect, полигон = контур, прямые связи — замок
        # общей оси). Виртуальный bbox коннектора остаётся ниже только в
        # routing (обход препятствий) и в стороне выхода — это маршрут и
        # визуал, не посадка. Маршрут инструмент, как и раньше, строит заново.
        from modules.graph.core import seating

        edge_data['waypoints'] = []
        seating.reseat_edge(self.nodes, edge_data)
        self._engine_finish_ends(edge_data)      # Э2c: порт/слот, не угол
        sp, tp = edge_data['source_point'], edge_data['target_point']
        sx, sy = sp[1], sp[0]
        tx, ty = tp[1], tp[0]

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
            source_geom = self._node_geometry(self.nodes[src_id])
            target_geom = self._node_geometry(self.nodes[tgt_id])
            perp_info = compute_edge_perpendicularity(
                (sx, sy), (tx, ty), source_geom, target_geom)
            self.edge_perp_scores[edge_key] = perp_info
        else:
            self.edge_perp_scores[edge_key] = {'is_good': True, 'score': 1.0, 'source_angle': 0}

    def _seated_face(self, node_id: str, px: float, py: float,
                     other_x: float, other_y: float) -> str:
        """Сторона узла для route_edge_v2: грань, на которой сидит конец.

        Коннектор — точка (конец == центроид == центр виртуального bbox),
        грань вырождена: сторона берётся по направлению на другой конец
        (bbox_exit_side). Прочие — ближайшая к посаженной точке грань bbox
        (closest_bbox_side): stub роутера выходит перпендикулярно ИМЕННО
        этой грани, подводящий сегмент ⟂ ей же — конец после пересадки по
        оси сегмента остаётся на своей грани, а не уезжает лучом в угол.
        """
        node = self.nodes.get(node_id) or {}
        bbox = self._get_node_bbox(node_id)
        if node.get('type') == 'connector' or not node.get('bbox'):
            cx, cy = node['centroid'][1], node['centroid'][0]
            return bbox_exit_side(bbox, cx, cy, other_x, other_y)
        return closest_bbox_side(bbox, px, py)

    def _route_orthogonal(self, edge_data: dict, alive: bool = False) -> bool:
        """Э3: лестница маршрутов для НЕпрямого ребра.

        1. Основной роутер (_route_orthogonal_main: route_edge_v2 с
           клиренсом, браковкой прошиваний и полигонных контуров);
        2. отказ → голый L/Z-фолбэк (_route_fallback_lz): два колена-
           кандидата, пересечение ТРУБ легально (мост), боксов/контуров —
           нет; без клиренса — колено впритык лучше диагонали;
        3. совсем некуда → честная диагональ, но ПОМЕЧЕННАЯ
           (_route_defect=True — не в sha, FXML игнорирует): судья и
           судья видит её как несведённый маршрут, а не норму.
        """
        if self._route_orthogonal_main(edge_data, alive):
            # финальный валидатор ПОВЕРХ главного роутера (репро 222222,
            # решение «страшно, когда труба легла на бокс»): BOUNDED-кап
            # препятствий (14 ближайших) позволял главному легально
            # прошивать далёкие боксы — принятый маршрут обязан пройти
            # тот же суд, что и фолбэки (прошивание+hug, амнистия базы)
            amn = self._amnesty_ids(edge_data)
            pts = [(p[1], p[0]) for p in
                   [edge_data['source_point']]
                   + list(edge_data.get('waypoints') or [])
                   + [edge_data['target_point']]]
            if not any(self._fb_seg_bad(a, b, edge_data, amn)
                       for a, b in zip(pts, pts[1:])):
                edge_data.pop('_route_defect', None)
                return True
            edge_data['waypoints'] = []
        if self._route_fallback_lz(edge_data) \
                or self._route_fallback_z(edge_data):
            edge_data.pop('_route_defect', None)
            return True
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if sp and tp and not (edge_data.get('waypoints') or []):
            sx2, sy2 = sp[1], sp[0]
            tx2, ty2 = tp[1], tp[0]
            diag = min(abs(tx2 - sx2), abs(ty2 - sy2)) > 0.5
            # строго-прямая, прошившая бокс (скольжение конца), при
            # отказе лестницы — тоже несведённый маршрут (репро edge_192)
            conflict = not diag and self._straight_conflicts_obstacle(
                edge_data, sx2, sy2, tx2, ty2)
            if diag or conflict:
                edge_data['_route_defect'] = True
                return False
        edge_data.pop('_route_defect', None)
        return False

    def _route_fallback_lz(self, edge_data: dict) -> bool:
        """Ступень 2 лестницы Э3: голый L-обход, когда основной роутер
        отказал (репро заказчика edge_145/87537 и graph_edited_line:
        отказ оставлял косую через боксы). Два кандидата-колена
        (H-V и V-H); сегменты не смеют прошивать НУТРО чужих боксов и
        реальных контуров (пересечение труб — мост, П5). Клиренса нет
        сознательно: фолбэк честнее диагонали, красоту наведёт доводка."""
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if not sp or not tp:
            return False
        sx, sy = sp[1], sp[0]
        tx, ty = tp[1], tp[0]
        if min(abs(tx - sx), abs(ty - sy)) <= 0.5:
            return False                     # почти прямая — не наш случай
        amn = self._amnesty_ids(edge_data)
        for wx, wy in ((tx, sy), (sx, ty)):
            pts = ((sx, sy), (wx, wy), (tx, ty))
            if any(self._fb_seg_bad(a, b, edge_data, amn)
                   for a, b in zip(pts, pts[1:])):
                continue
            edge_data['waypoints'] = [[wy, wx]]
            return True
        return False

    def _route_fallback_z(self, edge_data: dict) -> bool:
        """Ступень 2b лестницы Э3: голый Z-обход (два колена), когда и
        главный роутер, и L-колено отказали (замер на graph_edited_line:
        в плотных местах оба Г-кандидата прошивают соседей — 8 из 9
        диагоналей оставались). Промежуточная ось перебирается по
        серединам и КРОМКАМ мешающих боксов коридора (+/-3px); первый
        кандидат, чьи три сегмента не прошивают чужое нутро, побеждает.
        Пересечение труб легально (мост), клиренса нет — фолбэк."""
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if not sp or not tp:
            return False
        sx, sy = sp[1], sp[0]
        tx, ty = tp[1], tp[0]
        if min(abs(tx - sx), abs(ty - sy)) <= 0.5:
            return False
        amn = self._amnesty_ids(edge_data)
        lox, hix = min(sx, tx) - 40.0, max(sx, tx) + 40.0
        loy, hiy = min(sy, ty) - 40.0, max(sy, ty) + 40.0
        xs, ys = {(sx + tx) / 2.0}, {(sy + ty) / 2.0}
        # оси-кандидаты отступают от кромок на клиренс + полтолщину чернил
        # (раньше +/-3px — hug-браковка отбивала бы всех кандидатов)
        off = self.ROUTE_CLEARANCE \
            + float(edge_data.get('render_width') or 2.0) / 2.0 + 0.5
        own_ends = {edge_data.get('source'), edge_data.get('target')}
        for nid, node in self.nodes.items():
            if nid in amn[0] or nid in own_ends:
                continue
            bb = self._get_node_bbox(nid)
            if not bb or bb[2] < lox or bb[0] > hix \
                    or bb[3] < loy or bb[1] > hiy:
                continue
            xs.update((bb[0] - off, bb[2] + off))
            ys.update((bb[1] - off, bb[3] + off))
        cx_mid, cy_mid = (sx + tx) / 2.0, (sy + ty) / 2.0
        xs = sorted((x for x in xs if lox <= x <= hix),
                    key=lambda v: abs(v - cx_mid))[:24]
        ys = sorted((y for y in ys if loy <= y <= hiy),
                    key=lambda v: abs(v - cy_mid))[:24]
        for xm in xs:                                # H-V-H
            pts = ((sx, sy), (xm, sy), (xm, ty), (tx, ty))
            if not any(self._fb_seg_bad(a, b, edge_data, amn)
                       for a, b in zip(pts, pts[1:])):
                edge_data['waypoints'] = [[sy, xm], [ty, xm]]
                return True
        for ym in ys:                                # V-H-V
            pts = ((sx, sy), (sx, ym), (tx, ym), (tx, ty))
            if not any(self._fb_seg_bad(a, b, edge_data, amn)
                       for a, b in zip(pts, pts[1:])):
                edge_data['waypoints'] = [[ym, sx], [ym, tx]]
                return True
        return False

    def _amnesty_ids(self, edge_data: dict) -> set:
        """Узлы, чьё нутро фолбэкам МОЖНО пересекать: те, что БАЗОВАЯ
        полилиния ребра уже прошивала (правило «не хуже входа», репро
        graph_edited971: грань node_95 накрыта чужим гигантом node_94 —
        труба и так живёт внутри него). БЕЗ концевых узлов — их судит
        отдельная логика (hug своей грани запрещён).

        База = полилиния на ВХОДЕ ЖЕСТА (бэкап drag_start_edge_points /
        снимок _reseat_after_resize), кэш на жест: плавающая база по
        текущему кадру легализовала боксы, которых исходная труба не
        касалась (репро 222222 — «труба легла на неподвижный бокс»)."""
        key = self.model.edge_key(edge_data.get('source'),
                                  edge_data.get('target'))
        if key in self._amnesty_cache:
            return self._amnesty_cache[key]
        base = self.drag_start_edge_points.get(key) \
            if not self._batch_drag else None
        src_geom = base if base else edge_data
        sp = src_geom.get('source_point')
        tp = src_geom.get('target_point')
        amn = set()
        if sp and tp:
            # СТОРОЖ == СУДЬЯ (главное метаправило; амнистия со своим
            # предикатом (усадка 0.75 без полов) легализовала касания,
            # которых судья на входе не видел — маршруты «не хуже входа»
            # оказывались ХУЖЕ по судье): базу меряют те же
            # edit_checks.through_box/along_border на входной полилинии
            from modules.graph.core import edit_checks as _ec
            probe = {"id": "__amn__",
                     "source": edge_data.get('source'),
                     "target": edge_data.get('target'),
                     "source_point": sp, "target_point": tp,
                     "waypoints": list(src_geom.get('waypoints') or [])}
            mini = {"nodes": list(self.nodes.values()), "links": [probe]}
            thru = {i["node"] for i in _ec.through_box(mini)}
            along = {i["node"] for i in _ec.along_border(mini)}
            # амнистия РАЗДЕЛЬНАЯ: «вдоль» на входе НЕ разрешает «сквозь»
            # на выходе (репро manual_4/node_70: зазор 2.4px на входе
            # превращался в прошивание — это ухудшение класса)
            amn = (thru, along | thru)
        else:
            amn = (set(), set())
        self._amnesty_cache[key] = amn
        return amn

    def _fb_seg_bad(self, a, b, edge_data, amn) -> bool:
        """Сегмент фолбэка недопустим (решение заказчика 2026-08-01
        «не страшно, когда бокс наехал на трубу; страшно, когда ТРУБА
        легла на бокс»):
          * прошивание нутра неамнистированного узла (амнистия — только
            то, что БАЗОВАЯ полилиния уже прошивала);
          * прижатие-hug вдоль грани прямоугольного узла ближе клиренса,
            ВКЛЮЧАЯ свой бокс (репро edge_145: Г-колено ложилось на свою
            грань с зазором 0). Перпендикулярный стаб от порта hug не
            триггерит (перекрытие вдоль грани нулевое).
        Амнистированные пропускаются целиком (труба живёт внутри них);
        контуры — прошивание по реальной форме, hug по контуру не судим
        (ложняки над карманами)."""
        ax, ay = a
        bx, by = b
        amn_thru, amn_hug = amn
        own = {edge_data.get('source'), edge_data.get('target')}
        for nid, node in self.nodes.items():
            if nid in amn_thru:
                continue                     # труба живёт внутри — не судимо
            seg = _node_poly_contour(node)
            if seg is not None:
                if nid not in own \
                        and _seg_pierces_polygon(ax, ay, bx, by, seg):
                    return True
                # прижатие к КОНТУРУ — и к СВОЕМУ тоже (класс «вдоль своей
                # границы», edge_75/107/109 у node_28); перпендикулярный
                # стаб от точки посадки даёт короткий пробег ниже порога
                # судьи и не триггерит. Дёшево: bbox-претест, потом замер
                # по форме тем же сэмплером, что у судьи.
                if nid in amn_hug:
                    continue
                bb = node.get('bbox')
                if bb and len(bb) == 4:
                    c = self.ROUTE_CLEARANCE
                    if not (max(ax, bx) < bb[0] - c or min(ax, bx) > bb[2] + c
                            or max(ay, by) < bb[1] - c
                            or min(ay, by) > bb[3] + c):
                        from modules.graph.core import edit_checks as _ec
                        border = _ec._node_border(node)
                        # порог судьи: пробег >= OVERLAP_MIN; короткий
                        # перпендикулярный ПОДХОД к контуру (последние
                        # ~клиренс px перед посадкой) — легален, иначе
                        # валидатор браковал каждый заход в свой узел
                        if border and any(
                                r[1] >= _ec.OVERLAP_MIN for r in
                                _ec._hug_runs(ax, ay, bx, by, border, c)):
                            return True
                continue
            bb = self._get_node_bbox(nid)
            if not bb:
                continue
            if nid in own:
                if self._seg_hugs_bbox(ax, ay, bx, by, bb):
                    return True
                continue
            if segment_intersects_bbox(
                    ax, ay, bx, by,
                    (bb[0] + 0.75, bb[1] + 0.75, bb[2] - 0.75, bb[3] - 0.75)) \
                    and bb[0] + 0.75 < bb[2] - 0.75 \
                    and bb[1] + 0.75 < bb[3] - 0.75:
                return True                  # прошивание — амнистии нет
            if nid not in amn_hug \
                    and self._seg_hugs_bbox(ax, ay, bx, by, bb):
                return True
        return False

    def _route_orthogonal_main(self, edge_data: dict, alive: bool = False) -> bool:
        """Э6/Э7-a-лайт: ортогональный маршрут для НЕпрямого ребра.

        Требование заказчика (2026-07-31): при переносе узла и создании
        ребра «путь ортогональный, с минимальным пересечением труб, БЕЗ
        пересечения узлов, минимальной длины» — вместо косой диагонали.
        Маршрут строит СУЩЕСТВУЮЩИЙ route_edge_v2
        (ui/editors/edge_routing.py): R1 ортогональность, R4 обход bbox
        чужих узлов, R6 минимум поворотов, длина в скоринге.
        ОГРАНИЧЕНИЕ: «минимум пересечений труб» в этом заходе НЕ
        реализуется (это Э7-b, libavoid) — пересечения лишь штрафуются
        скорингом R8 по текущим путям остальных рёбер.

        Гистерезис (Э6/H7, два порога): расхождение осей посаженных концов
        div = min(|dx|, |dy|):
          * рождение — div >= snap_threshold (alive=False);
          * гашение — div < snap_threshold / 2 (alive=True);
          * между порогами существующее состояние сохраняется — маршрут у
            границы не мигает «родился/умер» на соседних кадрах.
        Порог — тот же snap_threshold = grid_size // 2 (деф. 12; после
        _compute_grid_size — четверть медианной ширины бокса), которым
        _recalculate_edge гасит waypoints у почти-соосных концов. Слабина
        прямизны (straight_slack_lock, 805ccd4) при этом уже отработала у
        вызывающего и для почти-соосной пары даёт min(...) == 0 —
        приоритет прямой трубы над коленом обеспечен по построению.

        Э5: waypoints — кэш последнего расчёта, флагов принадлежности нет;
        следующий жест вправе перестраивать и гасить любой маршрут.

        Концы НЕ трогает (пишет только waypoints + кэши сторон) —
        пересадка концов по осям подводящих сегментов остаётся
        вызывающему. Возвращает True, если маршрут построен.
        """
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if not sp or not tp:
            return False
        sx, sy = sp[1], sp[0]
        tx, ty = tp[1], tp[0]
        # Э2b («А->В»): конец жёстко в порту, слабина больше не скользит
        # его по грани — «почти-соосной» пары без колена не существует.
        # Прямая без маршрута — только СТРОГО осевая (0.5px); иначе роутер
        # обязан построить колено. Прежняя мёртвая зона гистерезиса
        # (snap/2..snap) оставляла диагонали 1-12px — файлы заказчика
        # «33» (dx=1.53) и bag (dy=1.7/2.6).
        if min(abs(tx - sx), abs(ty - sy)) <= 0.5:
            # Требование заказчика (скрины 2026-07-31): труба СКВОЗЬ чужое
            # оборудование недопустима и у соосной пары — конфликт прямой
            # (прошивание нутра ИЛИ прижатие ближе ROUTE_CLEARANCE к чужой
            # грани, жалоба edge_75/node_81) рождает обход так же, как увод
            # с оси. Пока конфликта нет, почти-прямые не роутятся
            # (гистерезис H7); родившийся из-за прижатия маршрут живёт,
            # пока конфликт не исчез (гашение — зазор >= клиренса на всём
            # пробеге: этот же предикат возвращает False).
            if not self._straight_conflicts_obstacle(edge_data, sx, sy, tx, ty):
                return False

        src_id, tgt_id = edge_data['source'], edge_data['target']
        src_bbox = self._route_end_bbox(src_id, sx, sy)
        tgt_bbox = self._route_end_bbox(tgt_id, tx, ty)
        # Осевая прямая (конфликтный роутинг соосной пары): грань выхода
        # однозначна по направлению трубы. closest_bbox_side у КОРНЕР-
        # посадки и у конца ВНУТРИ bbox полигонного узла давал изнаночную
        # грань — роутер не строил кандидатов (репро edge_75/node_28,
        # жалоба заказчика). Неосевые пары — прежний _seated_face.
        if abs(tx - sx) <= 0.5:                    # V-труба
            src_side, tgt_side = ('bottom', 'top') if ty > sy \
                else ('top', 'bottom')
        elif abs(ty - sy) <= 0.5:                  # H-труба
            src_side, tgt_side = ('right', 'left') if tx > sx \
                else ('left', 'right')
        else:
            src_side = self._seated_face(src_id, sx, sy, tx, ty)
            tgt_side = self._seated_face(tgt_id, tx, ty, sx, sy)

        obstacle_bboxes, existing_paths, reserve, poly_obs = \
            self._route_gesture_inputs(edge_data, sx, sy, tx, ty)
        # СЫРЫЕ прямоугольники — для браковки/донесения ниже: судится
        # прошивание реального нутра, а не раздутой рамки (двойной запас
        # клиренс+margin зажимал плотные места — маршрут отвергался, хотя
        # лишь шёл в коридоре у грани).
        all_raw = obstacle_bboxes + reserve
        raw_reserve = reserve
        # КЛИРЕНС (скрин заказчика «вдоль границы», 2026-07-31): ПРЯМОУГОЛЬНЫЕ
        # препятствия раздуваются на ROUTE_CLEARANCE — маршрут держит зазор от
        # чужих граней, а не липнет к ним вплотную (аналог standoff AutoCAD
        # P&ID / shapeBufferDistance libavoid; клиренс+нуджинг — Э7-b).
        # Полигонные узлы без скина в obstacle_bboxes НЕ попадают (их габарит
        # закрывает пол-листа) — их реальные контуры (poly_obs) судят
        # победивший маршрут ниже.
        c = self.ROUTE_CLEARANCE
        obstacle_bboxes = [(b[0] - c, b[1] - c, b[2] + c, b[3] + c)
                           for b in obstacle_bboxes]
        reserve = [(b[0] - c, b[1] - c, b[2] + c, b[3] + c) for b in reserve]

        # BOUNDED-режим (Э7-перф, лимит U-кандидатов): роутим по ближним
        # препятствиям; если победивший маршрут прошивает НУТРО препятствия
        # из резерва (сырой bbox, ужатие 0.75) — оно доносится в набор
        # (раздутым) и маршрут перестраивается (ленивое доуточнение,
        # детерминировано, <= ROUTE_AUGMENT_ITERS повторов). В наиве/EXACT
        # reserve пуст — ровно один вызов.
        for _ in range(1 + self.ROUTE_AUGMENT_ITERS):
            waypoints = route_edge_v2(
                src_conn=(sx, sy), tgt_conn=(tx, ty),
                src_side=src_side, tgt_side=tgt_side,
                src_bbox=src_bbox, tgt_bbox=tgt_bbox,
                obstacle_bboxes=obstacle_bboxes,
                existing_edge_paths=existing_paths,
            )
            if not reserve or not waypoints:
                break
            pts = ([(sx, sy)] + [(w[1], w[0]) for w in waypoints]
                   + [(tx, ty)])
            violated, still, still_raw = [], [], []
            for infl, raw in zip(reserve, raw_reserve):
                if self._path_pierces_bbox_inner(pts, raw):
                    violated.append(infl)
                else:
                    still.append(infl)
                    still_raw.append(raw)
            if not violated:
                break
            obstacle_bboxes = obstacle_bboxes + violated
            reserve, raw_reserve = still, still_raw
        if not waypoints:
            return False
        pts = ([(sx, sy)] + [(w[1], w[0]) for w in waypoints] + [(tx, ty)])
        ctx = self._drag_route_ctx
        if ctx is not None and ctx['bounded']:
            # BOUNDED: fallback-маршрут route_edge_v2 (нефильтрованный L при
            # полном провале кандидатов — зажатая позиция, стаб липнет к
            # соседу) прошивал бы узлы. Честнее оставить прямое ребро без
            # маршрута; в наиве/EXACT поведение прежнее (бит-паритет).
            # Судятся СЫРЫЕ нутра (ужатие 0.75, без клиренса и margin):
            # браковка ловит настоящее прошивание, а не проход в коридоре.
            for bbox in all_raw:
                if self._path_pierces_bbox_inner(pts, bbox):
                    return False
        # Полигонные препятствия — по РЕАЛЬНОМУ контуру (во всех режимах):
        # прошивание фигуры бракует маршрут, проход над пустым углом её
        # bbox легален (см. _node_poly_contour/_seg_pierces_polygon).
        for pseg in poly_obs:
            for a, b in zip(pts, pts[1:]):
                if _seg_pierces_polygon(a[0], a[1], b[0], b[1], pseg):
                    return False
        edge_data['waypoints'] = waypoints
        edge_data['_src_side'] = src_side
        edge_data['_tgt_side'] = tgt_side
        return True

    # Э7-перф (H7): режимы подбора входов route_edge_v2 на кадре drag.
    # EXACT: графы, где полные списки дешёвые, — вход бит-в-бит как наив.
    # BOUNDED: большие графы — префильтр прямоугольником маршрута + запас,
    # дальний хвост препятствий сворачивается в квадрантные блоки
    # (консервативно, «лимит U-кандидатов»), пути — ближайшие к прямой.
    ROUTE_EXACT_MAX_OBS = 40    # препятствий (узлов минус концы) для EXACT
    ROUTE_EXACT_MAX_PATHS = 60  # путей рёбер для EXACT
    ROUTE_RECT_PAD = 64         # запас прямоугольника маршрута, px
    ROUTE_CLEARANCE = 6.0       # px: зазор маршрута от чужих граней (= floor
                                # порога видимости трубы; standoff AutoCAD)
    # Порог перекрытия вдоль грани для конфликта-«прижатия» (жалоба
    # заказчика: edge_75 прямая x=926.0 в 2.48px от грани node_81 при
    # пробеге 18px — обход не рождался). Сегмент ближе ROUTE_CLEARANCE к
    # чужому bbox конфликтует, только если ПЕРЕКРЫВАЕТСЯ с ним вдоль
    # грани > 8px: уголковые касания и короткие пересечения коридоров
    # обход НЕ рождают — иначе плотные гребёнки взрывались бы коленями.
    ROUTE_HUG_OVERLAP_MIN = 8.0
    ROUTE_OBS_CAP = 14          # BOUNDED: стартовых препятствий у прямой
    ROUTE_PATH_CAP = 12         # BOUNDED: путей в скоринге R8/R5
    ROUTE_AUGMENT_ITERS = 3     # BOUNDED: доуточнений по нарушениям R4

    def _route_end_bbox(self, node_id: str, px: float, py: float):
        """bbox КОНЦА для route_edge_v2. Полигонному узлу без скина bbox
        шире фигуры (крупный контур != bbox): фильтр R4 роутера считает
        src/tgt bbox сплошным и убивал бы ЛЮБОЙ обход в его нутре — а
        труба легально живёт внутри рамки своей станции (репро
        edge_75 -> node_28). Такому концу отдаётся вырожденная рамка
        вокруг точки посадки; остальным — обычный bbox."""
        node = self.nodes.get(node_id) or {}
        if _node_poly_contour(node) is not None:
            return [px - 1.0, py - 1.0, px + 1.0, py + 1.0]
        return self._get_node_bbox(node_id)

    @staticmethod
    def _path_pierces_bbox_inner(pts: list, bbox) -> bool:
        """Полилиния [(x, y), ...] заходит в НУТРО сырого bbox (ужатие
        0.75px — касание кромки и проход по коридору у грани легальны)."""
        x1, y1, x2, y2 = bbox
        ix1, iy1, ix2, iy2 = x1 + 0.75, y1 + 0.75, x2 - 0.75, y2 - 0.75
        if ix1 >= ix2 or iy1 >= iy2:
            return False
        inner = (ix1, iy1, ix2, iy2)
        for a, b in zip(pts, pts[1:]):
            # margin=0: дефолтный margin=2 раздувал бы нутро обратно
            if segment_intersects_bbox(a[0], a[1], b[0], b[1], inner,
                                       margin=0):
                return True
        return False

    @classmethod
    def _seg_conflicts_bbox(cls, ax: float, ay: float,
                            bx: float, by: float, bbox) -> bool:
        """Конфликт сегмента с ЧУЖИМ bbox: прошивание ИЛИ прижатие.

        Прошивание — сегмент заходит в НУТРО bbox, ужатого на 0.75px
        (касание кромки прошиванием не считается).
        Прижатие (жалоба заказчика, edge_75/node_81: труба в 2.48px от
        грани) — осевой сегмент идёт ближе ROUTE_CLEARANCE к bbox И
        перекрывается с ним вдоль грани > ROUTE_HUG_OVERLAP_MIN: труба
        «лежит на грани» чужого бокса. Порог перекрытия отсекает
        уголковые касания и короткие пересечения коридоров (см. коммент
        у константы). Диагональный сегмент прижатием не судится —
        переходное состояние, его ловит прошивание."""
        x1, y1, x2, y2 = bbox
        ix1, iy1, ix2, iy2 = x1 + 0.75, y1 + 0.75, x2 - 0.75, y2 - 0.75
        if ix1 < ix2 and iy1 < iy2 and segment_intersects_bbox(
                ax, ay, bx, by, (ix1, iy1, ix2, iy2)):
            return True
        return cls._seg_hugs_bbox(ax, ay, bx, by, bbox)

    @classmethod
    def _seg_hugs_bbox(cls, ax: float, ay: float,
                       bx: float, by: float, bbox) -> bool:
        """Только прижатие-hug (без прошивания): осевой сегмент идёт ближе
        ROUTE_CLEARANCE к грани с перекрытием вдоль неё > порога."""
        x1, y1, x2, y2 = bbox
        c = cls.ROUTE_CLEARANCE
        m = cls.ROUTE_HUG_OVERLAP_MIN
        if abs(ax - bx) <= 0.5:                  # V-сегмент у вертикальной грани
            x = (ax + bx) / 2.0
            gap = max(x1 - x, x - x2, 0.0)
            overlap = min(max(ay, by), y2) - max(min(ay, by), y1)
            return gap < c and overlap > m
        if abs(ay - by) <= 0.5:                  # H-сегмент у горизонтальной грани
            y = (ay + by) / 2.0
            gap = max(y1 - y, y - y2, 0.0)
            overlap = min(max(ax, bx), x2) - max(min(ax, bx), x1)
            return gap < c and overlap > m
        return False

    @classmethod
    def _seg_conflicts_shape(cls, ax: float, ay: float,
                             bx: float, by: float, shape) -> bool:
        """Конфликт сегмента с ФОРМОЙ чужого узла: ('rect', bbox) |
        ('poly', контур).

        Прямоугольник — прежний _seg_conflicts_bbox (прошивание нутра ИЛИ
        прижатие-hug). Полигон без скина — только прошивание РЕАЛЬНОГО
        контура (_seg_pierces_polygon): hug-прижатие к полигону НЕ судится —
        прижатие к габариту было бы ложным (труба легально живёт над пустым
        углом bbox), а мелкая пластика зазоров у самого контура — территория
        раскладки (Э7-b/libavoid), не drag-роутера."""
        kind, geom = shape
        if kind == 'poly':
            return _seg_pierces_polygon(ax, ay, bx, by, geom)
        return cls._seg_conflicts_bbox(ax, ay, bx, by, geom)

    def _straight_conflicts_obstacle(self, edge_data: dict,
                                     sx: float, sy: float,
                                     tx: float, ty: float) -> bool:
        """Прямой отрезок концов конфликтует с чужой формой?

        Прямоугольные узлы — _seg_conflicts_bbox (прошивание нутра ИЛИ
        прижатие ближе ROUTE_CLEARANCE с перекрытием вдоль грани).
        Полигонные без скина — прошивание РЕАЛЬНОГО контура, без hug
        (см. _seg_conflicts_shape).

        На кадре drag берётся кэш жеста (O(N) дешёвый bbox-отсев), вне
        жеста (add_edge) — все узлы."""
        src_id, tgt_id = edge_data['source'], edge_data['target']
        ctx = self._drag_route_ctx
        pad = self.ROUTE_CLEARANCE
        lo_x, hi_x = min(sx, tx) - pad, max(sx, tx) + pad
        lo_y, hi_y = min(sy, ty) - pad, max(sy, ty) + pad
        if ctx is not None:
            nodes_iter, polys_iter = ctx['nodes'], ctx['polys']
        else:
            nodes_iter, polys_iter = [], []
            for nid, node in self.nodes.items():
                seg = _node_poly_contour(node)
                if seg is not None:
                    polys_iter.append((nid, seg))
                else:
                    nodes_iter.append((nid, None))
        for nid, bbox in nodes_iter:
            if nid == src_id or nid == tgt_id:
                continue
            bb = bbox if bbox is not None else self._get_node_bbox(nid)
            if not bb:
                continue
            if bb[2] < lo_x or bb[0] > hi_x or bb[3] < lo_y or bb[1] > hi_y:
                continue
            if self._seg_conflicts_bbox(sx, sy, tx, ty, bb):
                return True
        for nid, seg in polys_iter:
            if nid == src_id or nid == tgt_id:
                continue
            if seg is None:                        # узел едет — контур свежий
                seg = (self.nodes.get(nid) or {}).get('segmentation')
            if seg and _seg_pierces_polygon(sx, sy, tx, ty, seg):
                return True
        return False

    def _route_gesture_inputs(self, edge_data: dict,
                              sx: float, sy: float,
                              tx: float, ty: float
                              ) -> tuple[list, list, list, list]:
        """Входы route_edge_v2 + контуры: (obstacle_bboxes, existing_paths,
        reserve, poly_contours).

        Полигонные узлы без скина (_node_poly_contour) в obstacle_bboxes НЕ
        попадают ни в одном режиме — их габарит почти вдвое больше фигуры и
        закрывал бы пол-листа (роутер отвергал бы легальные маршруты над
        пустым углом bbox; репро node_28/звезда). Вместо bbox их плоские
        контуры возвращаются четвёртым списком — победивший маршрут судится
        по реальной форме в _route_orthogonal.

        Вне жеста drag (_drag_route_ctx is None, путь add_edge) — полный
        наив: bbox всех узлов кроме концов + пути всех рёбер, порядок
        словаря/списка; reserve пуст.

        На кадре drag — кэш жеста (дефект перф H7, кадр был O(N^2+N*E)):
          * маленькие графы (<= ROUTE_EXACT_MAX_*) — те же полные списки,
            в том же порядке и с теми же значениями, что наив (статика из
            кэша, двигающееся — свежим чтением): маршрут бит-в-бит равен
            неоптимизированному (это проверяет временная сверка в репро);
            reserve пуст;
          * большие графы — BOUNDED: препятствия за прямоугольником
            маршрута + ROUTE_RECT_PAD отбрасываются bbox-отсевом (простое
            пересечение прямоугольников, без shapely), из оставшихся
            ROUTE_OBS_CAP ближайших к прямой конн-конн идут в набор сразу
            (лимит U-кандидатов: 4 кандидата на препятствие), остальные —
            в reserve: _route_orthogonal доносит их в набор, только если
            победивший маршрут их прошивает (ленивое доуточнение R4);
            пути — до ROUTE_PATH_CAP ближайших к прямой из пересекающих
            регион. Здесь маршрут может отличаться от наивного (меньше
            дальних U-обходов) — осознанная цена перф на CPU-only листах
            (боевой лист ~937 узлов).
        """
        src_id, tgt_id = edge_data['source'], edge_data['target']
        ctx = self._drag_route_ctx
        if ctx is None:
            obstacle_bboxes, poly_contours = [], []
            for nid, node in self.nodes.items():
                if nid == src_id or nid == tgt_id:
                    continue
                seg = _node_poly_contour(node)
                if seg is not None:
                    poly_contours.append(seg)
                else:
                    obstacle_bboxes.append(self._get_node_bbox(nid))
            existing_paths = []
            for e in self.edges_data:
                if e is edge_data:
                    continue
                pts = _edge_path_pts(e)
                if pts:
                    existing_paths.append(pts)
            return obstacle_bboxes, existing_paths, [], poly_contours

        poly_contours = []
        for nid, seg in ctx['polys']:
            if nid == src_id or nid == tgt_id:
                continue
            if seg is None:                        # узел едет — контур свежий
                seg = (self.nodes.get(nid) or {}).get('segmentation')
            if seg:
                poly_contours.append(seg)

        if not ctx['bounded']:
            # -- EXACT: полные списки, порядок/значения — как наив --
            obstacles = []
            for nid, bbox in ctx['nodes']:
                if nid == src_id or nid == tgt_id:
                    continue
                obstacles.append(bbox if bbox is not None
                                 else self._get_node_bbox(nid))
            existing_paths = []
            for e, pts, _bb in ctx['paths']:
                if e is edge_data:
                    continue
                if pts is None:
                    pts = _edge_path_pts(e)
                if pts:
                    existing_paths.append(pts)
            return obstacles, existing_paths, [], poly_contours

        # -- BOUNDED: bbox-отсев прямоугольником маршрута + запас --
        # Прямоугольник накрывает и bbox КОНЦЕВЫХ узлов: route_edge_v2
        # генерирует U-кандидатов и вокруг src/tgt bbox (обход себя,
        # generate_candidates: all_obs + [src_bbox, tgt_bbox]) — их трубы
        # ходят на bbox+15 за пределами конн-прямоугольника.
        pad = float(self.ROUTE_RECT_PAD)
        sb = self._get_node_bbox(src_id)
        tb = self._get_node_bbox(tgt_id)
        rx1 = min(sx, tx, sb[0], tb[0]) - pad
        rx2 = max(sx, tx, sb[2], tb[2]) + pad
        ry1 = min(sy, ty, sb[1], tb[1]) - pad
        ry2 = max(sy, ty, sb[3], tb[3]) + pad

        near, rest = [], []
        for nid, bbox in ctx['nodes']:
            if nid == src_id or nid == tgt_id:
                continue
            if bbox is None:
                bbox = self._get_node_bbox(nid)
            if (bbox[2] >= rx1 and bbox[0] <= rx2
                    and bbox[3] >= ry1 and bbox[1] <= ry2):
                near.append(bbox)
            else:
                rest.append(bbox)
        # кольцо: кандидаты (U-трубы) ходят на ~15px за габарит ближних
        # препятствий — препятствия сразу за ним обязаны попасть в резерв
        if near and rest:
            nb_x1 = min(b[0] for b in near) - 32
            nb_y1 = min(b[1] for b in near) - 32
            nb_x2 = max(b[2] for b in near) + 32
            nb_y2 = max(b[3] for b in near) + 32
            if nb_x1 < rx1 or nb_y1 < ry1 or nb_x2 > rx2 or nb_y2 > ry2:
                still = []
                for bbox in rest:
                    if (bbox[2] >= nb_x1 and bbox[0] <= nb_x2
                            and bbox[3] >= nb_y1 and bbox[1] <= nb_y2):
                        near.append(bbox)
                    else:
                        still.append(bbox)
                rest = still

        kept, reserve = near, []
        if len(kept) > self.ROUTE_OBS_CAP:
            order = sorted(
                range(len(kept)),
                key=lambda i: (_seg_dist2(
                    (kept[i][0] + kept[i][2]) / 2.0,
                    (kept[i][1] + kept[i][3]) / 2.0,
                    sx, sy, tx, ty), i))
            ind_idx = sorted(order[:self.ROUTE_OBS_CAP])
            res_idx = sorted(order[self.ROUTE_OBS_CAP:])
            reserve = [kept[i] for i in res_idx]
            kept = [kept[i] for i in ind_idx]

        # регион кандидатов: прямоугольник маршрута + габарит препятствий
        kx1 = min([rx1] + [b[0] for b in kept]) - 20
        ky1 = min([ry1] + [b[1] for b in kept]) - 20
        kx2 = max([rx2] + [b[2] for b in kept]) + 20
        ky2 = max([ry2] + [b[3] for b in kept]) + 20

        # -- пути: пересекающие регион, до ROUTE_PATH_CAP ближайших --
        scored_paths = []
        for idx, (e, pts, bb) in enumerate(ctx['paths']):
            if e is edge_data:
                continue
            if pts is None:
                pts = _edge_path_pts(e)
                if not pts:
                    continue
                bb = _pts_bbox(pts)
            if bb[2] < kx1 or bb[0] > kx2 or bb[3] < ky1 or bb[1] > ky2:
                continue
            d = _seg_dist2((bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0,
                           sx, sy, tx, ty)
            scored_paths.append((d, idx, pts))
        if len(scored_paths) > self.ROUTE_PATH_CAP:
            scored_paths.sort(key=lambda t: (t[0], t[1]))
            scored_paths = scored_paths[:self.ROUTE_PATH_CAP]
            scored_paths.sort(key=lambda t: t[1])   # порядок списка рёбер
        return kept, [t[2] for t in scored_paths], reserve, poly_contours

    def _build_drag_route_ctx(self, moving_ids: set) -> dict:
        """Э7-перф (а): кэш жеста — собирается ОДИН раз в start_drag_node.

        nodes: [(nid, bbox|None)] в порядке self.nodes — только
        ПРЯМОУГОЛЬНЫЕ формы; None = узел едет, его bbox читается свежим на
        каждом кадре. polys: [(nid, контур|None)] — полигонные узлы без
        скина (_node_poly_contour): в препятствия route_edge_v2 не идут,
        конфликты судятся их РЕАЛЬНЫМ контуром; None = узел едет.
        paths: [(edge, pts, bbox)]
        в порядке edges_data; pts=None = ребро живое (инцидентно едущим
        узлам или без концов) — путь строится на кадре. bounded: граф
        больше EXACT-порогов (см. _route_gesture_inputs); route_anchor:
        {edge_key: (cx, cy)} — позиция узла на момент последнего роутинга
        ребра (переиспользование маршрута, _can_reuse_route).

        Уступания чужих рёбер НЕТ (решение заказчика 2026-08-01, третья
        итерация жалобы «ребро убегает от бокса»): drag трогает ТОЛЬКО
        рёбра таскаемых узлов. Бокс, положенный на чужую трубу, честно
        оставляет наложение — судья видит его классом «сквозь узел»,
        лечат доводка (Э4) или оператор. История: уступание вводилось
        по скрину 2026-07-31 (8088c3e), отменено целиком.
        """
        node_entries, poly_entries = [], []
        for nid, node in self.nodes.items():
            seg = _node_poly_contour(node)
            if seg is not None:
                poly_entries.append((nid, None if nid in moving_ids else seg))
            else:
                node_entries.append(
                    (nid, None if nid in moving_ids
                     else self._get_node_bbox(nid)))
        path_entries = []
        for e in self.edges_data:
            if e['source'] in moving_ids or e['target'] in moving_ids:
                path_entries.append((e, None, None))
                continue
            pts = _edge_path_pts(e)
            path_entries.append((e, pts, _pts_bbox(pts) if pts else None))
        bounded = (len(self.nodes) - 2 > self.ROUTE_EXACT_MAX_OBS
                   or len(path_entries) > self.ROUTE_EXACT_MAX_PATHS + 1)
        return {'nodes': node_entries, 'polys': poly_entries,
                'paths': path_entries,
                'bounded': bounded, 'route_anchor': {}, 'fail_anchor': {}}

    def _routing_failed_nearby(self, edge_key: tuple,
                               moved_node_id: str) -> bool:
        """Э7-перф (BOUNDED): негативный кэш — попытка роутинга провалилась
        (зажатая позиция, fallback отвергнут), и узел с тех пор не уехал на
        >= snap_threshold: не жечь полный роутинг на каждом кадре, ребро
        остаётся прямым до заметного сдвига. EXACT — без кэша."""
        ctx = self._drag_route_ctx
        if ctx is None or not ctx['bounded']:
            return False
        anchor = ctx['fail_anchor'].get(edge_key)
        node = self.nodes.get(moved_node_id)
        if anchor is None or node is None:
            return False
        cy, cx = node['centroid']
        return abs(cx - anchor[0]) + abs(cy - anchor[1]) < self.snap_threshold

    def _can_reuse_route(self, edge_data: dict, edge_key: tuple,
                         moved_node_id: str) -> bool:
        """Э7-перф (BOUNDED): живой авто-маршрут переиспользуется, пока узел
        не уехал от позиции последнего роутинга на >= snap_threshold
        (Manhattan). Ближний конец при этом пересаживается по подводящему
        сегменту как у обычного ребра с waypoints — форма маршрута отстаёт
        от узла не больше чем на порог. Гашение не запаздывает: при грубой
        оценке div < snap_threshold/2 (порог гашения гистерезиса)
        переиспользование запрещено — кадр честно перероутит и погасит.
        В EXACT-режиме (маленькие графы) не применяется: там роутинг дешёв
        и маршрут пересобирается каждый кадр.
        """
        ctx = self._drag_route_ctx
        if ctx is None or not ctx['bounded']:
            return False
        anchor = ctx['route_anchor'].get(edge_key)
        node = self.nodes.get(moved_node_id)
        if anchor is None or node is None:
            return False
        if node.get('segmentation') or node.get('skin_info'):
            # полигонные станции/скины сажают конец мимо оси bbox-замка —
            # предсказать ортогональность подводящего стаба нельзя
            return False
        cy, cx = node['centroid']
        if abs(cx - anchor[0]) + abs(cy - anchor[1]) >= self.snap_threshold:
            return False
        wps = edge_data.get('waypoints') or []
        if edge_data['source'] == moved_node_id:
            w, cur = wps[0], edge_data.get('source_point')
            far = edge_data.get('target_point')
        else:
            w, cur = wps[-1], edge_data.get('target_point')
            far = edge_data.get('source_point')
        if not cur or not far:
            return False
        bbox = self._get_node_bbox(moved_node_id)
        # подводящий сегмент осевой, и его ось всё ещё в створе грани
        # сдвинутого узла — посадка замком оси оставит стаб ортогональным
        if abs(cur[1] - w[1]) <= 0.5:                 # V-стаб: общий x
            if not (bbox[0] + 2 <= w[1] <= bbox[2] - 2):
                return False
        elif abs(cur[0] - w[0]) <= 0.5:               # H-стаб: общий y
            if not (bbox[1] + 2 <= w[0] <= bbox[3] - 2):
                return False
        else:
            return False                              # уже диагональ
        fx, fy = far[1], far[0]
        dx = fx - min(max(fx, bbox[0]), bbox[2])
        dy = fy - min(max(fy, bbox[1]), bbox[3])
        return min(abs(dx), abs(dy)) >= self.snap_threshold / 2.0

    # ── Этап B: оконная libavoid-сессия drag ─────────────────────────
    # Кадр жеста = посадка концов движком (как раньше) -> moveShape +
    # одна processTransaction по окну -> приёмка маршрутов live-рёбер.
    # Чужие рёбра стоят в сессии ФИКСАМИ (байт-в-байт, контракт
    # 2026-08-01). Любой сбой сессии = жест доезжает на самописной
    # лестнице (запасной путь по заданию).

    def _build_avoid_session(self, moving_ids: set):
        """Сессия жеста или None (нет биндинга / нет routable-рёбер /
        сбой сборки — лестница работает как прежде).

        virtual_boxes: судья _fb_seg_bad судит коннекторы их виртуальным
        боксом (_get_node_bbox) — роутер обязан видеть те же фигуры,
        иначе его маршрут через стык труб систематически бракуется и
        жест молча вырождается в лестницу (сторож == судья)."""
        if not self._drag_routable_edges:
            return None
        try:
            from modules.graph.core import edit_avoid
            vboxes = {}
            for nid, n in self.nodes.items():
                bb = n.get('bbox')
                if not bb or len(bb) != 4:
                    vb = self._get_node_bbox(nid)
                    if vb and len(vb) == 4:
                        vboxes[nid] = tuple(float(v) for v in vb)
            return edit_avoid.AvoidDragSession.build(
                self.nodes, self.edges_data, moving_ids,
                set(self._drag_routable_edges), self.model.edge_key,
                virtual_boxes=vboxes)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "edit_avoid: сессия жеста не собралась — drag на лестнице",
                exc_info=True)
            return None

    def _close_avoid_session(self):
        ses = self._avoid_session
        self._avoid_session = None
        if ses is not None:
            ses.close()

    def _avoid_route_frame(self, moving_ids: set, pending: list):
        """Кадр сессии: синхронизация геометрии + транзакция + приёмка.

        pending — [(edge_key, edge_data, alive, near_id)], live-рёбра
        кадра после посадки (defer_route). Приёмка per-ребро: концы в
        пинах и ортогональность — сторожа сессии (молчаливый fallback
        libavoid при недостижимом пине), прошивание/hug — ТОТ ЖЕ судья
        _fb_seg_bad с амнистией входа, что у лестницы (сторож == судья).
        Забракованное ребро на этом кадре роутит лестница — с негативным
        кэшем fail_anchor (BOUNDED), чтобы стабильная браковка не жгла
        полную лестницу поверх транзакции на каждом кадре."""
        ses = self._avoid_session
        routes = None
        if ses is not None:
            from modules.graph.core.edit_avoid import StaleModelError
            try:
                if ses.needs_rebuild(self.nodes):
                    # капнутое окно уехало / бюджет пинов — пересборка
                    # (редко: полный прогрев, зато роутер снова видит
                    # все фигуры рядом и свежие пины)
                    self._close_avoid_session()
                    ses = self._avoid_session = \
                        self._build_avoid_session(moving_ids)
                if ses is not None:
                    routes = ses.route_frame(self.nodes, self.edges_data)
            except StaleModelError:
                # undo/redo/delete снапшотом под жестом: штатная
                # деградация, не сбой — жест доезжает на лестнице
                import logging
                logging.getLogger(__name__).info(
                    "edit_avoid: модель пересобрана под жестом — сессия "
                    "закрыта, кадры ведёт лестница")
                self._close_avoid_session()
                routes = None
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "edit_avoid: кадр сессии упал — жест доезжает на "
                    "лестнице", exc_info=True)
                self._close_avoid_session()
                routes = None
        ctx = self._drag_route_ctx
        for edge_key, edge_data, alive, near_id in pending:
            ok = False
            if routes is not None:
                r = routes.get(edge_key)
                if r is not None and r['ends_ok'] and r['ortho']:
                    ok = self._avoid_apply_route(edge_data, r['pts'])
            if ok:
                if ctx is not None:
                    ctx['fail_anchor'].pop(edge_key, None)
            else:
                # запасной путь: лестница для ЭТОГО ребра на ЭТОМ кадре
                # (пометку _route_defect при полном отказе ставит она);
                # бухгалтерия негативного кэша — как у прямого пути
                skip = self._routing_failed_nearby(edge_key, near_id)
                routed = (not skip) and self._route_orthogonal(
                    edge_data, alive=alive)
                if routed:
                    if ctx is not None:
                        ctx['fail_anchor'].pop(edge_key, None)
                elif not skip and ctx is not None and ctx['bounded']:
                    node_now = self.nodes.get(near_id)
                    if node_now is not None:
                        ctx['fail_anchor'][edge_key] = (
                            node_now['centroid'][1], node_now['centroid'][0])
            self._refresh_edge_decor(edge_key, edge_data)

    def _avoid_apply_route(self, edge_data: dict, pts: list) -> bool:
        """Приёмка маршрута сессии тем же судом, что у лестницы
        (финальный валидатор _route_orthogonal): прошивание/hug с
        амнистией входа жеста. Успех пишет waypoints и кэши сторон
        (паритет с _route_orthogonal_main)."""
        amn = self._amnesty_ids(edge_data)
        if any(self._fb_seg_bad(a, b, edge_data, amn)
               for a, b in zip(pts, pts[1:])):
            return False
        mid = pts[1:-1]
        if not mid:
            edge_data['waypoints'] = []
            edge_data.pop('_route_defect', None)
            return True
        edge_data['waypoints'] = [[y, x] for x, y in mid]
        # Кэши сторон — от РЕАЛЬНЫХ стабов маршрута (первый/последний
        # сегмент), не от соосности концов: маршрут «из боковой грани и
        # обратно» у соосной пары писал бы ложные bottom/top (репро
        # скептика ревью). Семантика прежняя: грань, из которой выходит
        # стаб; у цели — грань, в которую он входит.
        sdx, sdy = mid[0][0] - pts[0][0], mid[0][1] - pts[0][1]
        tdx, tdy = pts[-1][0] - mid[-1][0], pts[-1][1] - mid[-1][1]
        edge_data['_src_side'] = (
            ('right' if sdx > 0 else 'left') if abs(sdx) > abs(sdy)
            else ('bottom' if sdy > 0 else 'top'))
        edge_data['_tgt_side'] = (
            ('left' if tdx > 0 else 'right') if abs(tdx) > abs(tdy)
            else ('top' if tdy > 0 else 'bottom'))
        edge_data.pop('_route_defect', None)
        return True

    def _reseat_moved_end(self, edge_data: dict, moved_node_id: str,
                          defer_route: bool = False):
        """Э3 (семантика GoJS adjusting=End): пересадить ТОЛЬКО конец ребра
        у сдвинутого узла; дальний конец и промежуточные waypoints
        неприкосновенны (§2 п.4 плана, метрика far_end_moved §3.2).

        Этап B (defer_route=True): маршрут кадра строит оконная
        libavoid-сессия ПОСЛЕ посадки всех ближних концов
        (_avoid_route_frame) — здесь только посадка и side-flip; кэши
        reuse/fail не участвуют (сессия перекладывает live-рёбра каждый
        кадр целиком), обновление пути/перпендикулярности делает приёмка
        маршрута.

        Ближний конец сажается единой посадкой `_seat_end_ported`
        (этап A — портовая модель):
          * toward — первый/последний waypoint ребра, если есть, иначе
            СУЩЕСТВУЮЩИЙ конец соседа (source/target_point из данных,
            НЕ его центроид);
          * замок оси почти-осевого концевого сегмента (`seating._seg_lock`)
            и слабина прямизны берутся, только если форма реально накрывает
            ось; иначе конец сидит В ПОРТУ (`port_model.choose_port`), а не
            ползёт ray-посадкой по периметру.
        Один и тот же расчёт работает и на каждом кадре протяжки, и на
        отпускании — предпросмотр честный ПО ПОСТРОЕНИЮ (решение заказчика:
        «что видишь при перетаскивании, то и получишь»); расчёт идемпотентен.
        reseat_edge целиком здесь звать нельзя: он выводит ось из НОВОЙ
        геометрии и пересаживает оба конца (дальний уезжал в 25/36 прогонов
        interactive_bench — замер Э0).

        Э5: жест ведёт все инцидентные рёбра (_drag_routable_edges);
        при уводе с оси больше слабины и порога строится ортогональный
        L/Z-маршрут (_route_orthogonal) от НЕПОДВИЖНОГО дальнего конца;
        маршрут прошлого кадра протяжки стирается и строится заново —
        кадр == отпускание (отпускание НИЧЕГО не пересчитывает). Ближний
        конец пересаживается каноном по оси подводящего сегмента
        (node_anchor + _seg_lock — как reseat_edge для рёбер с waypoints),
        дальний конец байт-в-байт (route_edge_v2 концов не двигает).

        Э7-перф, только BOUNDED (большие графы): живой маршрут
        переиспользуется, пока узел не уехал на >= snap_threshold от
        позиции последнего роутинга (_can_reuse_route), а провальная
        попытка не повторяется до такого же сдвига (_routing_failed_nearby)
        — кадр не жжёт полный роутинг впустую.
        """
        node = self.nodes.get(moved_node_id)
        if node is None:
            return
        src_id, tgt_id = edge_data['source'], edge_data['target']
        edge_key = self.model.edge_key(src_id, tgt_id)
        routable = edge_key in self._drag_routable_edges
        # Э6/H7-гистерезис: жив ли авто-маршрут на входе кадра — от этого
        # зависит порог _route_orthogonal (рождение/гашение).
        route_alive = routable and bool(edge_data.get('waypoints'))
        # Э7-перф (BOUNDED): живой маршрут переиспользуется, пока узел не
        # уехал от позиции последнего роутинга — ребро на этом кадре
        # ведётся как обычное с waypoints (конец по подводящему сегменту).
        reuse = (not defer_route) and route_alive and self._can_reuse_route(
            edge_data, edge_key, moved_node_id)
        # негативный кэш (BOUNDED): рядом с этой позицией роутинг уже
        # проваливался — не повторять попытку на каждом кадре
        skip_route = ((not defer_route) and routable and not route_alive
                      and self._routing_failed_nearby(edge_key, moved_node_id))
        if route_alive and not reuse:
            # маршрут прошлого кадра протяжки: перестраивается с нуля от
            # текущей геометрии (Э5: waypoints — кэш, drag ведёт все
            # инцидентные рёбра)
            edge_data['waypoints'] = []
        wps = edge_data.get('waypoints') or []
        if src_id == moved_node_id:
            point_key, far_key = 'source_point', 'target_point'
            ref = wps[0] if wps else edge_data.get(far_key)
        else:
            point_key, far_key = 'target_point', 'source_point'
            ref = wps[-1] if wps else edge_data.get(far_key)
        if not ref:
            return
        ref_x, ref_y = ref[1], ref[0]                  # [y, x] -> (x, y)
        cur = edge_data.get(point_key)
        # Этап A (портовая модель): станция Э10 → эффективный замок прямизны
        # (ось сегмента / слабина по дальнему якорю — только для рёбер без
        # waypoints, как раньше) → порт с гистерезисом. Конец больше не
        # ползёт по периметру ray-посадкой (жалоба §2 п.2-3 плана).
        far_node = self.nodes.get(tgt_id if src_id == moved_node_id
                                  else src_id)
        ax, ay = self._seat_end_ported(
            node, far_node, edge_data,
            's' if point_key == 'source_point' else 't',
            cur, ref_x, ref_y, try_slack=not wps)
        edge_data[point_key] = [ay, ax]

        # Side-flip дальнего конца (скрины заказчика 2026-07-31): adjusting=End
        # держит дальний конец байт-в-байт, но когда сдвинутый узел ПЕРЕСЁК
        # соседа, прежняя грань стала изнаночной — труба прошивала бы свой же
        # дальний узел. Смена стороны здесь — «нужда» (side_kept судит смену
        # «БЕЗ нужды»): дальний конец пересаживается каноном на обращённую
        # грань. Блок стоит ДО роутинга: маршрут ниже строится уже от
        # правильной грани В ЭТОМ ЖЕ кадре (иначе на отпускании оставалась
        # диагональ сквозь чужие блоки — второй скрин).
        from ui.editors import port_model as _pm
        fp = edge_data.get(far_key)
        # Пин оператора > автолечение изнанки: закреплённый дальний конец
        # side-flip не пересаживает (seat_end всё равно вернул бы пин) —
        # наложение остаётся судье.
        far_pin = _pm.edge_pin(
            edge_data, 'source' if far_key == 'source_point' else 'target')
        if far_node is not None and fp is not None and far_pin is None:
            wps_now = edge_data.get('waypoints') or []
            far_adj = ((wps_now[-1] if far_key == 'target_point' else wps_now[0])
                       if wps_now else edge_data[point_key])
            if self._end_pierces_own_node(far_node, fp, far_adj):
                fa_x, fa_y = far_adj[1], far_adj[0]
                # Этап A: изнанка — обязательная смена порта; choose_port
                # внутри исключает изнаночные порты и сажает на обращённый.
                fx, fy = self._seat_end_ported(
                    far_node, node, edge_data,
                    's' if far_key == 'source_point' else 't',
                    fp, fa_x, fa_y, try_slack=True)
                edge_data[far_key] = [fy, fx]
                if routable:
                    # маршрут строился от старой грани — сброс; роутинг
                    # ниже перестроит его от новой конфигурации на этом кадре
                    edge_data['waypoints'] = []
                    reuse = False
                    skip_route = False
                    route_alive = False
                    if self._drag_route_ctx is not None:
                        self._drag_route_ctx['fail_anchor'].pop(edge_key, None)
                        self._drag_route_ctx['route_anchor'].pop(edge_key, None)
                if not (edge_data.get('waypoints') or []):
                    # досадить ближний конец по новой грани дальнего
                    nref = edge_data[far_key]
                    ax, ay = self._seat_end_ported(
                        node, far_node, edge_data,
                        's' if point_key == 'source_point' else 't',
                        edge_data[point_key], nref[1], nref[0],
                        try_slack=True)
                    edge_data[point_key] = [ay, ax]

        if defer_route:
            # Этап B: посадка сделана, маршрут этого кадра построит
            # оконная libavoid-сессия (_avoid_route_frame) одной
            # транзакцией по всем live-рёбрам жеста.
            return

        if routable and not reuse and not skip_route \
                and self._route_orthogonal(edge_data, alive=route_alive):
            # Э6/Э7-a: увод больше слабины и порога — ортогональный маршрут;
            # ближний конец пересаживается по оси подводящего сегмента
            # (последний сегмент ⟂ грани — конец на грани, не в углу).
            if self._drag_route_ctx is not None:
                node_now = self.nodes[moved_node_id]
                self._drag_route_ctx['route_anchor'][edge_key] = (
                    node_now['centroid'][1], node_now['centroid'][0])
                self._drag_route_ctx['fail_anchor'].pop(edge_key, None)
            new_wps = edge_data['waypoints']
            ref2 = new_wps[0] if point_key == 'source_point' else new_wps[-1]
            # подводящий стаб ортогонален и начат в посаженном конце —
            # seg_lock внутри держит конец на порту (идемпотентно)
            ax, ay = self._seat_end_ported(
                node, None, edge_data,
                's' if point_key == 'source_point' else 't',
                edge_data[point_key], ref2[1], ref2[0], try_slack=False)
            edge_data[point_key] = [ay, ax]
        elif routable and not reuse:
            # Маршрут погашен (строгая соосность) либо лестница отказала —
            # ребро прямое, пометку ставит обёртка лестницы.
            # ВОССТАНОВЛЕНИЕ старого маршрута ОТВЕРГНУТО ДВАЖДЫ (2026-08-01):
            # старые колени + едущий порт = косой стаб 15-30px и «зигзаг
            # прибит гвоздями» (репро заказчика).
            if self._drag_route_ctx is not None:
                self._drag_route_ctx['route_anchor'].pop(edge_key, None)
                if not skip_route and self._drag_route_ctx['bounded']:
                    # позиция провала — негативный кэш до сдвига на порог
                    node_now = self.nodes[moved_node_id]
                    self._drag_route_ctx['fail_anchor'][edge_key] = (
                        node_now['centroid'][1], node_now['centroid'][0])

        self._refresh_edge_decor(edge_key, edge_data)

    def _refresh_edge_decor(self, edge_key: tuple, edge_data: dict):
        """Хвост кадра ребра: перерисовка пути + перпендикулярность.
        Зовут _reseat_moved_end (лестница) и приёмка маршрута сессии
        (_avoid_route_frame) — Этап B."""
        src_id, tgt_id = edge_data['source'], edge_data['target']
        self._update_edge_path(edge_key)
        if not edge_data.get('waypoints'):
            sp, tp = edge_data['source_point'], edge_data['target_point']
            self.edge_perp_scores[edge_key] = compute_edge_perpendicularity(
                (sp[1], sp[0]), (tp[1], tp[0]),
                self._node_geometry(self.nodes[src_id]),
                self._node_geometry(self.nodes[tgt_id]))
        else:
            self.edge_perp_scores[edge_key] = {'is_good': True, 'score': 1.0,
                                               'source_angle': 0}

    def _seat_end_ported(self, node, other_node, edge_data, role,
                         cur, ref_x, ref_y, try_slack):
        """Делегат движка (Э2a): вся посадка — `edit_engine.seat_end`.

        Э2b: движку передаются рёбра узла — рамочные концы садятся в
        слоты вокруг середины грани (несколько труб в грань не сливаются
        в точку); см. докстринг движка."""
        from modules.graph.core import edit_engine

        nid = node.get('id')
        # соседи по грани/участку; своё ребро исключается ПО ПАРЕ узлов
        # (инструменты работают с probe-копией — по идентичности объекта
        # реальное ребро посчиталось бы соседом самому себе)
        node_edges = [e for e in self.edges_data
                      if nid in (e.get('source'), e.get('target'))
                      and not (e.get('source') == edge_data.get('source')
                               and e.get('target') == edge_data.get('target'))]
        return edit_engine.seat_end(node, other_node, edge_data, role,
                                    cur, ref_x, ref_y, try_slack,
                                    float(self.snap_threshold),
                                    node_edges=node_edges)

    def _reseat_after_resize(self, node_id: str):
        """Э2d (переопределение легаси Simple): после resize/расталкивания
        пересаживается ТОЛЬКО конец у изменённого узла — движком
        (порт/слот/контур); дальние концы соседей неприкосновенны (C6).
        Прежний путь переписывал ОБА конца по центроидам: терял порты,
        рушил дальние концы и косил стабы маршрутов. Пин конца движок
        уважает сам (seat_end: пин первее всего).

        Мини-жест (репро заказчика 2026-08-01 «после resize линии
        наслаиваются»): авто-маршруты рёбер узла ПЕРЕСТРАИВАЮТСЯ от новых
        посадок — вне drag роутинг заперт за _drag_routable_edges, и без
        мини-жеста концы разъезжались по слотам, а колени оставались
        стопкой (трубы лежали друг на друге всей длиной)."""
        # Э5: мини-жест ведёт ВСЕ рёбра узла (waypoints — кэш расчёта).
        auto_keys = set()
        for edge in self.edges_data:
            if node_id not in (edge.get('source'), edge.get('target')):
                continue
            auto_keys.add(self.model.edge_key(edge['source'],
                                              edge['target']))
        prev_routable = self._drag_routable_edges
        prev_ctx = self._drag_route_ctx
        prev_ses = self._avoid_session
        self._drag_routable_edges = auto_keys
        self._drag_route_ctx = self._build_drag_route_ctx({node_id})
        self._amnesty_cache = {}
        # Этап B: мини-жест ведёт ТА ЖЕ оконная libavoid-сессия, что и drag.
        # Иначе смена размеров/толщины оставалась бы на самописной лестнице и
        # рождала класс дефектов («труба легла на бокс»), которого протяжка
        # уже не делает: развод слотов косит стаб, лестница отказывает, гейт
        # ниже откатывает ребро в прежний слот — трубы остаются в нахлёсте.
        self._avoid_session = self._build_avoid_session({node_id})
        work, pending = [], []
        try:
            for edge in self.edges_data:
                if node_id not in (edge.get('source'), edge.get('target')):
                    continue
                key = self.model.edge_key(edge['source'], edge['target'])
                # снимок ДО посадки: гейт судит его же (см. ниже)
                work.append((edge, key, self._edge_is_ortho(edge),
                             (list(edge.get('source_point') or []),
                              list(edge.get('target_point') or []),
                              [list(w) for w in edge.get('waypoints') or []],
                              bool(edge.get('_route_defect')))))
                if self._avoid_session is not None and key in auto_keys:
                    alive = bool(edge.get('waypoints'))
                    self._reseat_moved_end(edge, node_id, defer_route=True)
                    pending.append((key, edge, alive, node_id))
                else:
                    self._reseat_moved_end(edge, node_id)
            if pending:
                # маршруты всех рёбер узла — ОДНОЙ транзакцией, с клиренсом и
                # нуджингом (как кадр drag); отказ приёмки пер-ребро уводит
                # это ребро на лестницу внутри _avoid_route_frame
                self._avoid_route_frame({node_id}, pending)
            # ГЕЙТ «не хуже входа» (репро заказчика 2026-08-01: утолщение
            # разводило слоты, а при отказе роутера труба становилась косой):
            # ортогональное ребро НЕ ИМЕЕТ ПРАВА стать диагональю от развода.
            # Ни сессия, ни лестница не нашли колено — полный откат ребра в
            # прежний слот (нахлёст чернил честнее косой). Гейт стоит ПОСЛЕ
            # транзакции: до неё маршрута ещё нет и судить нечего.
            for edge, key, was_ortho, bak in work:
                if was_ortho and not self._edge_is_ortho(edge):
                    sp0, tp0, wp0, defect0 = bak
                    edge['source_point'] = sp0
                    edge['target_point'] = tp0
                    edge['waypoints'] = wp0
                    if defect0:
                        edge['_route_defect'] = True
                    else:
                        edge.pop('_route_defect', None)
                    self._update_edge_path(key)
        finally:
            self._drag_routable_edges = prev_routable
            self._drag_route_ctx = prev_ctx
            self._amnesty_cache = {}
            self._close_avoid_session()
            self._avoid_session = prev_ses

    @staticmethod
    def _edge_is_ortho(edge_data: dict, tol: float = 1.0) -> bool:
        """Все сегменты полилинии осевые (H/V) в пределах tol."""
        sp = edge_data.get('source_point')
        tp = edge_data.get('target_point')
        if not sp or not tp:
            return True
        pts = [sp] + list(edge_data.get('waypoints') or []) + [tp]
        return all(min(abs(b[1] - a[1]), abs(b[0] - a[0])) <= tol
                   for a, b in zip(pts, pts[1:]))

    def _engine_finish_ends(self, edge_data: dict):
        """Э2c: довести ОБА конца ребра до контракта движка после канонной
        оси — порт/слот у рамочных, отступ/развод у полигонов, центроид у
        коннектора; угол непредставим. Для инструментов (optimize,
        add_edge, recalculate), пересчитывающих ребро целиком: понятия
        «дальний конец» здесь нет — это не жест drag."""
        for role, point_key, other_key, node_key in (
                ('s', 'source_point', 'target_point', 'source'),
                ('t', 'target_point', 'source_point', 'target')):
            node = self.nodes.get(edge_data.get(node_key))
            p = edge_data.get(point_key)
            if node is None or p is None:
                continue
            wps = edge_data.get('waypoints') or []
            ref = (wps[0] if role == 's' else wps[-1]) if wps \
                else edge_data.get(other_key)
            if not ref:
                continue
            x, y = self._seat_end_ported(node, None, edge_data, role, p,
                                         ref[1], ref[0], try_slack=False)
            edge_data[point_key] = [y, x]

    @staticmethod
    def _end_pierces_own_node(node, end_yx, adj_yx):
        """Конец сидит на ИЗНАНОЧНОЙ грани: первый сегмент от конца уходит
        внутрь прямоугольника посадки собственного узла. Полигонные узлы без
        скина пропускаются (их bbox шире фигуры — проба внутри bbox легальна
        над вырезом контура); коннекторы — тоже (rect нет)."""
        from modules.graph.core import seating

        seg = node.get('segmentation')
        if seg and isinstance(seg, list) and len(seg) >= 6 \
                and node.get('class_name') not in seating.FIXED_SIZES:
            return False
        # рамка редактора (bbox, «символ тянется на рамку» 2026-08-01):
        # прошивание полей letterbox — тоже изнанка, символ там нарисован
        from modules.graph.core.edit_checks import seat_rect
        rect = seat_rect(node)
        if rect is None:
            return False
        x1, y1, x2, y2 = rect
        ex, ey = end_yx[1], end_yx[0]
        dx, dy = adj_yx[1] - ex, adj_yx[0] - ey
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return False
        px, py = ex + dx / dist * 2.0, ey + dy / dist * 2.0   # 2px вдоль сегмента
        return x1 + 0.25 < px < x2 - 0.25 and y1 + 0.25 < py < y2 - 0.25

    def _shift_bound_blocks(self, node_id: str, dx: float, dy: float):
        """Э3: текст-блоки, ПРИВЯЗАННЫЕ к узлу, едут за ним на ту же дельту
        (решение заказчика §8.3: привязанная подпись — якорь узла,
        непривязанная стоит). Для side-привязок совпадает с производной
        позицией (_bound_block_bbox от новой геометрии цели); для привязок
        без side — даёт само следование."""
        if not dx and not dy:
            return
        seen_blk = set()   # дубль-привязка в файле не должна двигать блок дважды
        for b in self.model.bindings:
            if b.get("node_id") != node_id:
                continue
            blk_id = b.get("block_id")
            if blk_id in seen_blk:
                continue
            seen_blk.add(blk_id)
            blk = self.model.find_text_block(blk_id)
            bb = blk.get("bbox") if blk else None
            if bb and len(bb) == 4:
                blk["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]

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
        rect_bbox = [rx, ry, rx + rw, ry + rh]

        # В режиме «Размер и цвет» обводка выделяет только рёбра, не узлы.
        if not edge_only:
            for node_id in self.nodes:
                # Частичное пересечение: bbox узла перекрывается с рамкой.
                if self._bboxes_overlap(self._get_node_bbox(node_id), rect_bbox):
                    self.selected_nodes.add(node_id)

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
                # Частичное пересечение: любой сегмент задевает рамку
                # (как в ветке edge_only).
                for (ax, ay), (bx, by) in self._get_edge_segments(edge):
                    if ((rx <= ax <= rx + rw and ry <= ay <= ry + rh) or
                            (rx <= bx <= rx + rw and ry <= by <= ry + rh) or
                            segment_intersects_bbox(ax, ay, bx, by, rect_bbox)):
                        self.selected_edges.add(key)
                        break

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
        """Применить текущий цвет ('color'), размер ('size') или пунктир ('dash')
        к ребру/обводке.

        Для 'dash' флаг edge_data['dashed'] переключается (toggle). Если ребро
        входит в обводку — переключаются все обведённые рёбра одинаково: целевое
        значение берётся как отрицание флага ребра под курсором, чтобы вся пачка
        меняла состояние согласованно.
        """
        keys = self._edges_to_style(edge_key)
        if not keys:
            return

        desc = {"color": "Цвет рёбер", "size": "Размер рёбер",
                "dash": "Пунктир рёбер"}.get(kind, "Стиль рёбер")
        cmd = SetEdgeStyleCommand(self.model, self._redraw_all, description=desc)
        cmd.execute()  # snapshot before

        # Для пунктира целевое значение — отрицание текущего флага ребра под
        # курсором (чтобы обведённая пачка переключалась согласованно).
        dash_target = None
        if kind == "dash":
            anchor = self.model.find_edge_data(edge_key)
            dash_target = not bool(anchor.get('dashed')) if anchor else True

        changed = 0
        for k in keys:
            edge_data = self.model.find_edge_data(k)
            if not edge_data:
                continue
            if kind == "color":
                edge_data['render_color'] = self.edge_brush_color.name()
            elif kind == "dash":
                edge_data['dashed'] = bool(dash_target)
            else:
                edge_data['render_width'] = int(self.edge_brush_size)
            changed += 1

        if kind == "size" and changed:
            # Решение заказчика (карточка 2026-07-31 + репро 2026-08-01
            # «после ресайза ребра наслаиваются»): смена толщины =
            # пересчёт — слоты держат зазор ПО ЧЕРНИЛАМ (_ink_pitch),
            # авто-маршруты затронутых узлов перестраиваются мини-жестом.
            nids = set()
            for k in keys:
                ed_ = self.model.find_edge_data(k)
                if ed_:
                    nids.update((ed_.get('source'), ed_.get('target')))
            for nid in nids:
                if nid:
                    self._reseat_after_resize(nid)

        cmd.finalize()  # snapshot after
        self.undo_mgr.push_executed(cmd)

        self._redraw_all()
        # _redraw_all не восстанавливает подсветку обводки — вернуть её
        self._update_selection_visuals()

        if kind == "color":
            self.update_status(
                f"Цвет {self.edge_brush_color.name()} применён к {changed} рёбрам"
            )
        elif kind == "dash":
            state = "включён" if dash_target else "выключен"
            self.update_status(f"Пунктир {state} для {changed} рёбер")
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
        elif self._current_mode == "edit_edge_dash":
            base_w = edge_data.get('render_width') or (self.EDGE_WIDTH + 2)
            pen = QPen(QColor(255, 255, 255), float(base_w))
            pen.setStyle(Qt.PenStyle.DashLine)
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
        """Ctrl+ПКМ — удалить под курсором (без выделения).

        При активном выделении Ctrl+ПКМ ничего не удаляет: клик по объекту
        ИЗ выделения исключает его из выделения, по любому другому — ничего.
        Удаление выделенной пачки — только клавишей Delete.
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
                before = self._undo_point()
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
        # или удалить блок. При активном выделении — только исключение из
        # выделения (удаление выделенных — клавишей Delete).
        if self.display_regime == "ocr" and hasattr(self, "_ocr_block_at"):
            bid = self._ocr_block_at(x, y)
            if getattr(self, "_selected_ocr", None):
                if bid is not None and bid in self._selected_ocr:
                    self._selected_ocr.discard(bid)
                    self.refresh_ocr_layer()
                    self.update_status(
                        f"Блок исключён из выделения (осталось {len(self._selected_ocr)})"
                    )
                # По любому другому объекту при активном выделении — ничего.
                return
            if bid is not None:
                # Снять ручки размера (блок может быть удалён этой операцией).
                if getattr(self, "_ocr_resize_overlay", None) is not None:
                    self._hide_ocr_block_resize()
                if self.model.find_binding(bid) is not None:
                    self.unbind_ocr_block(bid)
                    self.update_status("Блок отвязан")
                else:
                    self.delete_ocr_block(bid)
                    self.update_status("Блок удалён")
                return

        if self._current_mode in ("edit_edge_color", "edit_edge_size", "edit_edge_dash"):
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

        # Активное выделение base-слоя: Ctrl+ПКМ только исключает объект из
        # выделения (никаких удалений); удаление выделенного — клавишей Delete.
        if self.selected_nodes or self.selected_edges:
            node_id = self._node_for_copy_at(x, y)
            if node_id and node_id in self.selected_nodes:
                self.selected_nodes.discard(node_id)
                self._update_selection_visuals()
                self.update_status(
                    f"Узел исключён из выделения (осталось {len(self.selected_nodes)} узлов, "
                    f"{len(self.selected_edges)} рёбер)"
                )
                return
            edge_key, _ = self.find_nearest_edge(x, y)
            if edge_key and edge_key in self.selected_edges:
                self.selected_edges.discard(edge_key)
                self._update_selection_visuals()
                self.update_status(
                    f"Ребро исключено из выделения (осталось {len(self.selected_nodes)} узлов, "
                    f"{len(self.selected_edges)} рёбер)"
                )
                return
            # По невыделенному объекту / пустому месту при активном выделении — ничего.
            return

        node_id = self.find_node_at(x, y)
        if node_id:
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

        # Точка возврата — ниже; превью «Размеров» снимается до неё
        # (см. drop_uncommitted_preview).
        self.drop_uncommitted_preview()
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

        # СМЕЩЕНИЕ ЗАХВАТА: узел едет на дельту курсора, а не прыгает в него.
        # Без этого первый же кадр drag телепортировал центроид ровно под
        # курсор — на боевом c2f79462 так уехал node_103 на 7.68 px при пороге
        # клика 8, то есть «щёлкнул рядом с узлом» превращалось в сдвиг.
        gx = getattr(self, '_ctrl_lmb_start_x', None)
        gy = getattr(self, '_ctrl_lmb_start_y', None)
        self._drag_grab_dx = (node_data['centroid'][1] - gx) if gx is not None else 0.0
        self._drag_grab_dy = (node_data['centroid'][0] - gy) if gy is not None else 0.0

        self._batch_drag = (node_id in self.selected_nodes and len(self.selected_nodes) > 1)
        self._drag_routable_edges = set()
        self._amnesty_cache = {}

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
                    # Э5: boundary-рёбра ведёт жест — все (waypoints — кэш).
                    self._drag_routable_edges.add(
                        self.model.edge_key(sid, tid))

            # Э3: блоки, привязанные к узлам выделения, едут вместе с группой
            # (undo покрыт snapshot'ом BatchDragCommand — он включает text_blocks)
            self._batch_bound_blocks = []
            seen_blk = set()
            for b in self.model.bindings:
                bid = b.get("block_id")
                if b.get("node_id") in sel and bid and bid not in seen_blk:
                    blk = self.model.find_text_block(bid)
                    if blk and blk.get("bbox"):
                        seen_blk.add(bid)
                        self._batch_bound_blocks.append(blk)
        else:
            self.drag_start_edge_points = {}
            for key in self.model.get_connected_edges(node_id):
                edge_data = self.model.find_edge_data(key)
                if edge_data:
                    self.drag_start_edge_points[key] = {
                        'source_point': (edge_data.get('source_point') or []).copy(),
                        'target_point': (edge_data.get('target_point') or []).copy(),
                        # Бэкап waypoints: пересадка конца waypoints не трогает
                        # (adjusting=End), но ручные рёбра перепроецируются
                        # мимо undo — DragNodeCommand возвращает всё скопом.
                        'waypoints': [wp.copy() for wp in edge_data.get('waypoints', [])],
                        # кэши сторон мутируют на кадрах — undo обязан
                        # вернуть и их (иначе side_kept/декор судят по лжи)
                        '_src_side': edge_data.get('_src_side'),
                        '_tgt_side': edge_data.get('_tgt_side'),
                    }
                    # Э5: жест ведёт все инцидентные рёбра (waypoints — кэш;
                    # закреплён только вход с пином, его держит seat_end).
                    self._drag_routable_edges.add(key)
            # Э3: бэкап bbox привязанных текст-блоков — они едут за узлом,
            # undo обязан вернуть и их (тест T-C: undo возвращает оба).
            self.drag_start_block_bboxes = {}
            for b in self.model.bindings:
                if b.get("node_id") != node_id:
                    continue
                blk = self.model.find_text_block(b.get("block_id"))
                if blk and blk.get("bbox"):
                    self.drag_start_block_bboxes[b["block_id"]] = list(blk["bbox"])

        # Э7-перф (а): кэш препятствий и путей — один раз на весь жест.
        # Строится всегда: даже без routable-инцидентных рёбер кадру нужны
        # «уступающие» кандидаты (Дефект 2) — бокс без труб, надвинутый на
        # чужую авто-трубу, обязан её раздвигать.
        moving_ids = set(self.selected_nodes) if self._batch_drag \
            else {node_id}
        self._drag_route_ctx = self._build_drag_route_ctx(moving_ids)
        # Этап B: оконная libavoid-сессия жеста — live-рёбра перекладывает
        # роутер с клиренсом и нуджингом на каждом кадре; None (нет
        # биндинга/сбой сборки) = кадры роутит лестница, как раньше.
        # Закрыть возможную прошлую (защита «залипшего» drag без release).
        self._close_avoid_session()
        self._avoid_session = self._build_avoid_session(moving_ids)

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
            self._move_single_node(self.dragging_node,
                                   x + getattr(self, '_drag_grab_dx', 0.0),
                                   y + getattr(self, '_drag_grab_dy', 0.0))
        self._update_selection_visuals()

    def _batch_move_fast(self, dx: float, dy: float):
        """Перемещение группы узлов на кадре протяжки.

        Стратегия для рёбер (Э3, adjusting=End):
        - Internal (оба конца в выделении): оба конца «ближние» — жёсткий
          сдвиг source_point/target_point/waypoints на dx,dy (каноничная
          посадка сохраняется трансляцией);
        - Boundary (один конец в выделении): пересаживается ТОЛЬКО конец у
          узла из выделения — ТОЙ ЖЕ функцией, что и на отпускании
          (_reseat_moved_end): предпросмотр честный по построению.
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
                r = self.EQUIPMENT_MARKER_RADIUS if node_type == 'equipment' else self.CONNECTOR_DRAW_RADIUS
                self.node_items[nid].setRect(nx-r, ny-r, r*2, r*2)

            # Скин следует за боксом при групповом drag (bbox уже обновлён выше)
            if self.show_skins and nid in self._skin_items:
                self._update_node_skin(nid)

        # Э3: привязанные к выделению текст-блоки едут на ту же дельту
        # (precomputed в start_drag_node; по блоку ровно одна привязка)
        for blk in self._batch_bound_blocks:
            bb = blk.get("bbox")
            if bb and len(bb) == 4:
                blk["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]

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

        pending = []
        for e in self._batch_boundary_edges:
            # Э3/H7: единственный расчёт boundary-ребра — здесь, на кадре;
            # отпускание НИЧЕГО не пересчитывает (итог жеста = последний
            # кадр), иначе Гаусс-Зейдель по свежим путям соседей двигал
            # маршрут и конец после отпускания (32/46 расхождений в репро).
            near = e['source'] if e['source'] in sel else e['target']
            key = self.model.edge_key(e['source'], e['target'])
            if self._avoid_session is not None \
                    and key in self._drag_routable_edges:
                # Этап B: маршрут кадра строит сессия одной транзакцией
                alive = bool(e.get('waypoints'))
                self._reseat_moved_end(e, near, defer_route=True)
                pending.append((key, e, alive, near))
            else:
                self._reseat_moved_end(e, near)
        if self._avoid_session is not None:
            # internal-рёбра и ручные фиксы уже сдвинуты выше — сессия
            # дотащит их фиксированные маршруты до роутера на этом кадре
            self._avoid_route_frame(set(sel), pending)

        # Дефект 2: НЕинцидентные авто-трубы уступают надвинутой группе
        # (паритет с одиночным drag; undo — snapshot BatchDragCommand)

        # Pass 3: перерисовать привязанные текст-блоки (данные уже сдвинуты
        # выше; no-op вне состояния 'ocr')
        if hasattr(self, "_refresh_ocr_layer_for_node"):
            for nid in sel:
                self._refresh_ocr_layer_for_node(nid)

    def end_drag_node(self):
        """Завершить перетаскивание."""
        if not self.dragging_node:
            return
        node_id = self.dragging_node

        try:
            if self._batch_drag:
                # H7-паритет (Гаусс-Зейдель): boundary-рёбра НЕ пересчитываются
                # на отпускании. Каждый кадр протяжки уже посадил концы и маршруты
                # той же _reseat_moved_end; повторный прогон здесь скорил бы
                # маршруты против СВЕЖИХ путей соседних рёбер (кадры скорили
                # против прошлого кадра) — маршрут и даже посаженный конец
                # прыгали на отпускании (32/46 расхождений в репро).
                # Итог жеста = ровно состояние последнего кадра протяжки.
                self._batch_snap_cmd.finalize()
                self.undo_mgr.push_executed(self._batch_snap_cmd)
                self._batch_snap_cmd = None
            else:
                cmd = DragNodeCommand(
                    self.model, self, node_id,
                    self.drag_start_centroid, self.drag_start_bbox,
                    self.drag_start_segmentation, self.drag_start_edge_points,
                    self.drag_start_block_bboxes,
                )
                cmd.capture_new_state()
                self.undo_mgr.push_executed(cmd)
        finally:
            # состояние жеста чистится даже при сбое undo-снапшота —
            # иначе Router-сессия и dragging_node переживали бы жест
            self.dragging_node = None
            self._batch_drag = False
            self._batch_internal_edges = []
            self._batch_boundary_edges = []
            self._batch_bound_blocks = []
            self._drag_routable_edges = set()
            self._amnesty_cache = {}
            self._drag_route_ctx = None
            self._close_avoid_session()   # Этап B: Router-сессия живёт жест
            self.drag_start_centroid = None
            self.drag_start_bbox = []
            self.drag_start_segmentation = []
            self.drag_start_edge_points = {}
            self.drag_start_block_bboxes = {}

    def _move_single_node(self, node_id: str, x: float, y: float):
        """Переместить один узел; у инцидентных рёбер пересадить ТОЛЬКО
        ближний конец (Э3, adjusting=End — _reseat_moved_end). Вызывается на
        каждом кадре протяжки; end_drag_node геометрию больше не меняет —
        предпросмотр честный по построению."""
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
            r = self.EQUIPMENT_MARKER_RADIUS if node_type == 'equipment' else self.CONNECTOR_DRAW_RADIUS
            self.node_items[node_id].setRect(x - r, y - r, r * 2, r * 2)

        # Э3: привязанные текст-блоки едут за узлом на ту же дельту
        # (непривязанные стоят; undo — через DragNodeCommand)
        self._shift_bound_blocks(node_id, dx, dy)

        # Рёбра: обновить кэши сторон + пересадить только ближний конец
        affected = [e for e in self.edges_data if e['source'] == node_id or e['target'] == node_id]

        for e in affected:
            sid, tid = e['source'], e['target']
            s, t = self.nodes[sid], self.nodes[tid]
            s_cx, s_cy = s['centroid'][1], s['centroid'][0]
            t_cx, t_cy = t['centroid'][1], t['centroid'][0]
            s_bbox = self._get_node_bbox(sid)
            t_bbox = self._get_node_bbox(tid)
            e['_src_side'] = bbox_exit_side(s_bbox, s_cx, s_cy, t_cx, t_cy)
            e['_tgt_side'] = bbox_exit_side(t_bbox, t_cx, t_cy, s_cx, s_cy)

        pending = []
        for e in affected:
            # Э3 (adjusting=End): дальний конец неприкосновенен; Э5:
            # waypoints — кэш, жест ведёт все инцидентные рёбра, вход с
            # пином держит seat_end. reseat_edge целиком не зовётся.
            key = self.model.edge_key(e['source'], e['target'])
            if self._avoid_session is not None \
                    and key in self._drag_routable_edges:
                # Этап B: маршрут кадра строит сессия одной
                # транзакцией ниже; alive — для гистерезиса лестницы
                # при пер-рёберном отказе приёмки
                alive = bool(e.get('waypoints'))
                self._reseat_moved_end(e, node_id, defer_route=True)
                pending.append((key, e, alive, node_id))
            else:
                self._reseat_moved_end(e, node_id)
        if self._avoid_session is not None:
            # и с пустым pending: чужие фиксы обязаны доехать до роутера
            # (нуджинг соседей по ним)
            self._avoid_route_frame({node_id}, pending)

        # Дефект 2: НЕинцидентные авто-трубы уступают надвинутому боксу
        # (и гаснут обратно в прямую при уводе) — на каждом кадре.

        # Скин следует за боксом при drag (bbox в модели уже обновлён выше)
        if self.show_skins and node_id in self._skin_items:
            self._update_node_skin(node_id)

        # Привязанные текст-блоки следуют за узлом/его рёбрами
        # (гранулярно; внутри — no-op вне состояния 'ocr')
        if hasattr(self, "_refresh_ocr_layer_for_node"):
            self._refresh_ocr_layer_for_node(node_id)

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
            # Э5: правка waypoint — одноразовая косметика; следующий жест,
            # задевший ребро, пересчитает маршрут (waypoints — кэш).
            new_wp = edge_data['waypoints'][wp_idx].copy()
            cmd = MoveWaypointCommand(self.model, self, edge_key, wp_idx,
                                      self.dragging_wp_start, new_wp)
            self.undo_mgr.push_executed(cmd)
        self.dragging_waypoint = None
        self.dragging_wp_start = None
        self.update_status("Waypoint перемещён")

    def _add_waypoint_on_segment(self, edge_key: tuple, segment_index: int, x: float, y: float):
        edge_data = self.model.find_edge_data(edge_key)
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

    # ── Port markers (этап A) ──

    def _show_port_markers(self, node_id: str):
        """Маленькие маркеры портов узла на время drag конца ребра:
        голубые — кандидаты (производные), оранжевые — пины рёбер (Э5)."""
        self._hide_port_markers()
        from ui.editors import port_model
        node = self.nodes.get(node_id)
        if not node:
            return
        r = 3.0
        cx, cy = port_model._node_cxy(node)
        nid = node.get('id')
        pins = []
        for e in self.edges_data:
            if nid not in (e.get('source'), e.get('target')):
                continue
            pin = port_model.pinned_on_node(node, e)
            if pin is not None:
                pins.append((cx + float(pin['dx']), cy + float(pin['dy']),
                             0.0, 0.0, True))
        for px, py, _nx, _ny, manual in pins + port_model.all_ports(node):
            color = QColor(255, 152, 0) if manual else QColor(0, 188, 212)
            m = QGraphicsEllipseItem(px - r, py - r, r * 2, r * 2)
            m.setPen(QPen(color, 1.5))
            m.setBrush(QBrush(QColor(color.red(), color.green(),
                                     color.blue(), 110)))
            m.setZValue(9)
            self.scene.addItem(m)
            self._port_markers.append(m)

    def _hide_port_markers(self):
        for m in self._port_markers:
            self.scene.removeItem(m)
        self._port_markers.clear()

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

    EP_DRAG_THRESHOLD = 5.0   # px сцены: ниже — это клик, а не перетаскивание

    def _start_endpoint_drag(self, ep_hit):
        edge_key, endpoint = ep_hit
        edge_data = self.model.find_edge_data(edge_key)
        if edge_data:
            side_key = '_src_side' if endpoint == 'source' else '_tgt_side'
            self._dragging_endpoint = ep_hit
            self._dragging_ep_start_side = edge_data.get(side_key, 'right')
            # порог клик/drag: без него дрожь в 1 px объявляла маршрут
            # ручным (`_manual_route`) и стирала waypoints
            pt = edge_data.get('source_point' if endpoint == 'source'
                               else 'target_point') or [0.0, 0.0]
            self._ep_drag_origin = (float(pt[1]), float(pt[0]))
            self._ep_drag_armed = False
            # Решение заказчика 2026-08-02: «есть обработка при перетаскивании
            # — нужно то же самое, только зафиксировать один из портов».
            # Поэтому жест протяжки конца открывает ТОТ ЖЕ кадровый конвейер,
            # что и перенос узла: кэш препятствий, набор ведомых рёбер и
            # оконная libavoid-сессия. Своей самописной ветки роутинга здесь
            # больше нет — она и рождала рассинхрон «маршрут от старых концов».
            node_id = (edge_data['source'] if endpoint == 'source'
                       else edge_data['target'])
            self._ep_node_id = node_id
            self._drag_routable_edges = {edge_key}
            self._drag_route_ctx = self._build_drag_route_ctx({node_id})
            self._amnesty_cache = {}
            self._close_avoid_session()
            self._avoid_session = self._build_avoid_session({node_id})
            # Этап A: показать порты узла (кандидаты + ручные) — конец
            # будет липнуть к ним при протяжке.
            self._ep_on_port = False
            self._show_port_markers(edge_data['source'] if endpoint == 'source'
                                    else edge_data['target'])
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
        geom = self._node_geometry(node) if node else None
        if geom and geom['type'] == 'polygon':
            return project_point_to_polygon_border(geom['data'], x, y)
        bbox = self._get_node_bbox(node_id)
        return project_point_to_bbox_border(bbox, x, y)

    def _drag_endpoint_to(self, x: float, y: float):
        """Ручное перемещение точки прикрепления.

        Точка свободно скользит по периметру узла (bbox/полигон). Линия НЕ
        пересчитывается обычным кадровым конвейером (сессия/лестница).
        Точка оператора закрепляется ПИНОМ конца ребра (edge['pin_source'|
        'pin_target']) — seat_end сажает в пин первее всего; снять пин —
        ПКМ по маркеру конца («отвязать вход»).
        """
        if not self._dragging_endpoint:
            return
        ox, oy = getattr(self, '_ep_drag_origin', (x, y))
        if not getattr(self, '_ep_drag_armed', False):
            if ((x - ox) ** 2 + (y - oy) ** 2) ** 0.5 <= self.EP_DRAG_THRESHOLD:
                return                       # ещё клик, маршрут не трогаем
            self._ep_drag_armed = True
        edge_key, endpoint = self._dragging_endpoint
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return

        node_id = edge_data['source'] if endpoint == 'source' else edge_data['target']
        px, py = self._project_to_node_border(node_id, x, y)
        # Этап A: конец липнет к портам (кандидаты + существующие ручные);
        # мимо портов — свободное скольжение по границе, отпускание там
        # создаст постоянный ручной порт (_end_endpoint_drag).
        from ui.editors import port_model
        snap_p = port_model.nearest_port(self.nodes.get(node_id) or {},
                                         x, y, float(self.snap_threshold))
        if snap_p is not None:
            px, py = snap_p[0], snap_p[1]
        self._ep_on_port = snap_p is not None

        point_key = 'source_point' if endpoint == 'source' else 'target_point'
        side_key = '_src_side' if endpoint == 'source' else '_tgt_side'

        edge_data[point_key] = [py, px]  # формат [y, x]
        edge_data[side_key] = closest_bbox_side(self._get_node_bbox(node_id), px, py)
        # Решение заказчика 2026-08-02: «нужен КЛАССИЧЕСКИЙ ОБХОД, но с
        # ФИКСИРОВАННЫМ ВХОДОМ; даже когда двигаю вход — пересчитывать».
        # Точка оператора немедленно становится ПИНОМ конца ребра (Э5:
        # переживает кадр, перенос узла и resize; на коннекторе пин не
        # рождается — set_edge_pin вернёт None, конец держит центроид),
        # а маршрут пересчитывается ПРЯМО НА КАДРЕ протяжки — оператор
        # видит настоящий обход, а не прямую-обманку.
        from ui.editors import port_model as _pm
        node = self.nodes.get(node_id)
        if node is not None:
            _pm.set_edge_pin(node, edge_data, endpoint, px, py)
        edge_data.pop('_manual_route', None)   # легаси-флаг: файл мимо миграции
        key = self.model.edge_key(edge_data['source'], edge_data['target'])
        if self._drag_route_ctx is None:
            # кадровый конвейер обычно поднимает _start_endpoint_drag; если
            # конец потянули другим путём — поднимаем лениво, иначе роутинг
            # промолчал бы и труба осталась голой прямой
            self._ep_node_id = node_id
            self._drag_routable_edges = {key}
            self._drag_route_ctx = self._build_drag_route_ctx({node_id})
            self._amnesty_cache = {}
            self._close_avoid_session()
            self._avoid_session = self._build_avoid_session({node_id})
        # ОБЫЧНЫЙ кадр, как при переносе узла: посадка концов + маршрут одной
        # транзакцией + приёмка. Ближний конец при этом никуда не «садится» —
        # его держит якорь (seat_end отдаёт закреплённый порт первым), то есть
        # это ровно «то же самое, только один порт зафиксирован».
        self._reseat_far_end_after_endpoint_drag()
        pending = []
        if self._avoid_session is not None:
            alive = bool(edge_data.get('waypoints'))
            self._reseat_moved_end(edge_data, node_id, defer_route=True)
            pending.append((key, edge_data, alive, node_id))
            self._avoid_route_frame({node_id}, pending)
        else:
            self._reseat_moved_end(edge_data, node_id)
        self._update_edge_path(key)
        self._refresh_waypoint_markers_for_edge(key)
        self._refresh_endpoint_markers()

    def _reseat_far_end_after_endpoint_drag(self):
        """Развернуть ДАЛЬНИЙ конец навстречу точке, которую поставил оператор.

        Репро заказчика (graph_edited_fix.json, 2026-08-02): «вручную сменил
        порт — получил диагональ через чужой блок». Механика: протяжка двигает
        ТОЛЬКО ближний конец, а дальний остаётся на грани, которая новому
        положению уже не смотрит. У edge_22 конец оператора сел на левую грань
        node_19, а дальний остался на ВЕРХНЕЙ грани node_20 — прямая между
        ними вошла в node_20 через правую грань и прошла ~24px внутри блока.
        Судья этого не видел: through_box исключает свои же концевые узлы.

        Разворот делает движок (порт/слот/контур), ориентир — точка оператора.
        Его конец при этом неприкосновенен: пришпилил оператор — значит его
        выбор и есть база, под которую подстраивается сосед. Это НЕ нарушение
        C6 («дальний конец не трогать»): C6 про перенос УЗЛА, где оператор
        ребра не касался, а здесь он пересоединил трубу руками и ждёт, что она
        соединится осмысленно.
        """
        if not self._dragging_endpoint or not getattr(self, '_ep_drag_armed', False):
            return
        edge_key, endpoint = self._dragging_endpoint
        edge_data = self.model.find_edge_data(edge_key)
        if not edge_data:
            return
        near_key = 'source_point' if endpoint == 'source' else 'target_point'
        far_key = 'target_point' if endpoint == 'source' else 'source_point'
        near_pt = edge_data.get(near_key)
        far_pt = edge_data.get(far_key)
        if not near_pt or not far_pt:
            return
        far_id = (edge_data['target'] if endpoint == 'source'
                  else edge_data['source'])
        near_id = (edge_data['source'] if endpoint == 'source'
                   else edge_data['target'])
        far_node = self.nodes.get(far_id)
        near_node = self.nodes.get(near_id)
        if far_node is None:
            return
        x, y = self._seat_end_ported(
            far_node, near_node, edge_data,
            's' if far_key == 'source_point' else 't',
            far_pt, near_pt[1], near_pt[0], try_slack=True)
        edge_data[far_key] = [y, x]
        self._update_edge_path(edge_key)

    def _end_endpoint_drag(self):
        # Э5: пин конца уже записан на кадрах протяжки (set_edge_pin в
        # _drag_endpoint_to); здесь — страховка для жеста, пришедшего в
        # обход кадров. Undo побайтово: пин в данных ребра, снапшот
        # _ep_snap_cmd покрывает модель целиком.
        if self._dragging_endpoint and getattr(self, '_ep_drag_armed', False) \
                and not self._ep_on_port:
            edge_key, endpoint = self._dragging_endpoint
            edge_data = self.model.find_edge_data(edge_key)
            if edge_data:
                from ui.editors import port_model
                node_id = (edge_data['source'] if endpoint == 'source'
                           else edge_data['target'])
                node = self.nodes.get(node_id)
                pt = edge_data.get('source_point' if endpoint == 'source'
                                   else 'target_point')
                if node is not None and pt:
                    port_model.set_edge_pin(node, edge_data, endpoint,
                                            pt[1], pt[0])
        # Итог жеста = состояние последнего кадра (тот же принцип, что у
        # переноса узла: предпросмотр честен по построению). Здесь только
        # закрытие сессии и очистка кадрового состояния — ДО finalize
        # снапшота, иначе redo не вернёт маршрут.
        self._hide_port_markers()
        self._close_avoid_session()
        self._drag_routable_edges = set()
        self._drag_route_ctx = None
        self._amnesty_cache = {}
        self._ep_node_id = None
        if hasattr(self, '_ep_snap_cmd') and self._ep_snap_cmd:
            self._ep_snap_cmd.finalize()
            self.undo_mgr.push_executed(self._ep_snap_cmd)
            self._ep_snap_cmd = None
        self._dragging_endpoint = None
        self._dragging_ep_start_side = None
        self.update_status("Точка прикрепления перемещена")

    def contextMenuEvent(self, event):
        """ПКМ по маркеру конца — «отвязать вход» (Э5): пин снимается,
        конец возвращается в общий конкурс портов и пересаживается
        мини-жестом. Остальные ПКМ глушатся, как в Base (Ctrl+ПКМ —
        удаление)."""
        pos = self.mapToScene(event.pos())
        hit = self._find_endpoint_at(pos.x(), pos.y())
        if hit is not None:
            edge_key, endpoint = hit
            if self._unpin_endpoint(edge_key, endpoint):
                event.accept()
                return
        super().contextMenuEvent(event)

    def _unpin_endpoint(self, edge_key: tuple, endpoint: str) -> bool:
        """Э5: снять пин конца («отвязать вход»). Один шаг undo."""
        from ui.editors import port_model as _pm

        edge_data = self.model.find_edge_data(edge_key)
        if edge_data is None or _pm.edge_pin(edge_data, endpoint) is None:
            return False
        node_id = (edge_data['source'] if endpoint == 'source'
                   else edge_data['target'])
        # `contextMenuEvent` не проверяет ни режим, ни состояние: ПКМ по
        # приколотому концу достижим при живом превью «Размеров» (§83.28).
        # Точка возврата — ниже (см. drop_uncommitted_preview); граница И5
        # соблюдена ранним выходом «пина нет» выше.
        self.drop_uncommitted_preview()
        snap_cmd = AutoFixCommand(self.model, self._redraw_all)
        snap_cmd.description = "Отвязать вход"
        snap_cmd.execute()
        _pm.clear_edge_pin(edge_data, endpoint)
        # Мини-жест: конец и маршруты рёбер узла пересаживаются той же
        # механикой, что resize/толщина (сессия + гейт «не хуже входа»).
        self._reseat_after_resize(node_id)
        snap_cmd.finalize()
        self.undo_mgr.push_executed(snap_cmd)
        self._refresh_endpoint_markers()
        self._refresh_waypoint_markers_for_edge(edge_key)
        self.update_status("Вход отвязан — конец снова в общем конкурсе портов")
        return True

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
        if self._canvas_mode:
            cw, ch = int(self.canvas_w), int(self.canvas_h)   # сетка по холсту
        else:
            cw, ch = int(self.img_width), int(self.img_height)
        for gx in range(0, cw + 1, gs):
            line = self.scene.addLine(gx, 0, gx, ch, pen)
            line.setZValue(0.5)
            self._grid_items.append(line)
        for gy in range(0, ch + 1, gs):
            line = self.scene.addLine(0, gy, cw, gy, pen)
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

        # Незафиксированное превью «Размеров» — ДО снятия точки возврата
        # (см. drop_uncommitted_preview).
        self.drop_uncommitted_preview()
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd.execute()
        cmd.description = "Auto-Fix (chains)"

        try:
            stats = auto_fix_graph(
                self.nodes,
                self.edges_data,
                equip_max_shift=self.EQUIP_MAX_SHIFT,
                conn_max_shift=self.CONN_MAX_SHIFT,
            )
        finally:
            # Закрытие шага undo — в finally: движок мутирует узлы и рёбра НА
            # МЕСТЕ (autofix_chains.auto_fix_graph, Step 4), и его падение на
            # пересадке рёбер оставляло оператора с изменённым холстом, без
            # Ctrl+Z и без роста revision — вкладка считала, что несохранённого
            # нет. Перерисовка здесь же: иначе сцена показывает старые
            # координаты поверх уже сдвинутой модели. Порядок тот же, что на
            # зелёном пути.
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
        # Выход из режима (Esc / кнопка) — тот же брошенный превью, что и смена
        # набора: «Применить» не нажимали, значит возвращаем как было.
        self._revert_resize_preview()
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
        # Набор изменился → незафиксированное превью откатить (базлайн уходит
        # вместе с ним и пересоберётся при следующем превью).
        self._revert_resize_preview()
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
        self._resize_pin_base = self._capture_resize_pins()
        self._resize_pin_preview = []
        self._resize_model_rev = self.undo_mgr.revision
        self._resize_preview_geom = {}

    @staticmethod
    def _preview_geom_key(node: dict) -> dict:
        """Отпечаток геометрии узла — им сверяется, что превью ещё в модели.

        ПО ПОЛЯМ, а не одним кортежем: чужой откат возвращает узлу не всё
        сразу. `DragNodeCommand.undo` кладёт назад centroid/bbox/segmentation
        и не трогает `area` — превью в непокрытом поле пережило бы откат
        навсегда (замер §97.3: `area` 1428 → 8100 доезжала до сервера).
        """
        return {'centroid': tuple(node['centroid']),
                'bbox': tuple(node.get('bbox') or ()),
                'segmentation': tuple(node.get('segmentation') or ()),
                'area': node.get('area')}

    def _preview_live_fields(self) -> dict:
        """Где ещё лежит вписанное превью: `{узел → множество полей}`.

        ⛔ Сторож, отвечающий одним «да/нет» на МНОЖЕСТВО объектов, обязан
        отвечать ПОЭЛЕМЕНТНО (`PROTOCOL §3`, третий возврат пункта). Агрегат
        выглядел верным ровно пока набор ОДНОРОДЕН: чужой Ctrl+Z по переносу
        возвращает ОДНОМУ узлу его геометрию и честно стирает превью только
        на нём — а вердикт «базлайн мёртв» выключал лечение для всех 17,
        и превью остальных вваривалось в схему (замер §97.1: `node_11` уезжал
        на сервер как `[-10, 188, 80, 278]` при стеке 0).

        Отсюда же вторая ось: у ТОГО САМОГО узла превью остаётся в полях,
        которых чужой откат не касался, — их снимать можно и нужно, это не
        воскрешение превью, а уборка за собой (граница §48 держится тем, что
        поля, которые откат вернул, в ответе не значатся).
        """
        if self._resize_model_base is None:
            return {}
        if self._resize_model_rev == self.undo_mgr.revision:
            # Стек не двигался — тронуть превью было некому.
            return {nid: set(self._preview_geom_key(self.nodes[nid]))
                    for nid in self._resize_base if nid in self.nodes}
        live = {}
        for nid, key in self._resize_preview_geom.items():
            node = self.nodes.get(nid)
            if node is None:
                continue
            cur = self._preview_geom_key(node)
            fields = {name for name, val in key.items() if cur[name] == val}
            if fields:
                live[nid] = fields
        return live

    def _preview_still_in_model(self) -> bool:
        """Превью, вписанное последним `_apply_sizes_from_base`, ещё в модели
        ЦЕЛИКОМ — у каждого узла набора, в каждом поле И В КАЖДОМ ПИНЕ.

        Прямой ответ вместо косвенного: `model.restore` пересобирает словари
        и кладёт в них состояние ДО превью — отпечатки расходятся, и базлайн
        переиспользовать нельзя. Правка НА МЕСТЕ чужого узла отпечатков набора
        не трогает — базлайн цел.

        ⛔ Пины — ВТОРАЯ ПОЛОВИНА превью, и до пятого возврата пункта их здесь
        не было: сторож судил по четырём узловым полям, а откат, вернувший
        ОДНИ ПИНЫ, оставался невидим. Базлайн объявлялся живым, пересъёма не
        было, и `_apply_sizes_from_base` безусловно писал ПРОТУХШУЮ пиновую
        базу — то есть отменял чужой Ctrl+Z (замер `MEASUREMENTS §110.18`:
        пин 33.0 при стеке 0, след второго тика 148.5 вместо 45.0; отмена до
        дна стека давала 33.0, и 10.0 было недостижимо). Отпечаток у пинов
        уже был (`_resize_pin_preview`) — здесь его просто СПРАШИВАЮТ, ровно
        как узловой, поэтому вопрос «какие команды пишут пины» снят и на этом
        пути тоже.

        Агрегат остаётся ровно там, где вопрос ДЕЙСТВИТЕЛЬНО про весь набор:
        «можно ли взять базлайн как есть». Что именно откатывать, решается
        поэлементно — `_preview_live_fields()` для узлов и
        `_rollback_owned_resize_pins()` для пинов.
        """
        if not self._resize_preview_geom:
            return False
        live = self._preview_live_fields()
        if not all(len(live.get(nid, ())) == len(key)
                   for nid, key in self._resize_preview_geom.items()):
            return False
        return self._snapshot_resize_pins() == self._resize_pin_preview

    def _resize_baseline_alive(self) -> bool:
        """Базлайн снят и всё ещё описывает текущее состояние схемы.

        Между снятием и применением базлайна может пройти чужая команда —
        прежде всего Ctrl+Z / Ctrl+Y посреди живого превью. Тогда базлайн
        описывает состояние, которое оператор уже отменил: откат по нему
        отменил бы сам откат, а превью село бы вокруг отменённого центроида
        (замер §48). Первый сторож — `undo_mgr.revision`: он считает ВСЕ
        мутации через стек, включая команды, которые правят узлы на месте
        и модель не пересобирают (`DragNodeCommand`), — сравнение объектов
        `nodes`/`edges_data` по ссылке такие правки пропускает (замер §48).

        ⛔ Но `revision` — сторож КОСВЕННЫЙ, и своей косвенностью он сам стал
        дефектом (ревизия связки, §83б): он растёт на ЛЮБОЙ мутации стека,
        а «модель пересобрали» верно только для undo/redo снимочных команд.
        `OptimizeEdgeCommand.undo` и `DragNodeCommand.undo` правят словари НА
        МЕСТЕ — один Ctrl+Z по такой команде объявлял базлайн мёртвым, хотя
        превью физически оставалось в модели, и все ШЕСТЬ потребителей
        `drop_uncommitted_preview()` вырождались в no-op ОДНОВРЕМЕННО (четыре
        кнопки, воронка OCR, путь записи). Поэтому у сторожа есть второе,
        ПРЯМОЕ мнение — `_preview_still_in_model()`: превью снимается, пока
        оно наше, независимо от того, сколько чужих шагов легло в стек.

        ⚠ Ответ здесь — про ВЕСЬ набор, и это законно: спрашивают, годится ли
        базлайн к переиспользованию целиком. Ответ «нет» больше не означает
        «превью бросить» — что снимать, решает `_preview_live_fields()`
        поэлементно, а зовущие сначала снимают своё и только потом пересобирают
        базлайн (третий возврат пункта, `PROTOCOL §3`).
        """
        if self._resize_model_base is None:
            return False
        if self._resize_model_rev == self.undo_mgr.revision:
            return True
        return self._preview_still_in_model()

    def _capture_resize_pins(self):
        """Пины инцидентных рёбер набора — тоже часть базлайна превью.

        `rescale_edge_pins` домножает `dx/dy` НА МЕСТЕ, поэтому без возврата к
        исходным значениям каждый тик бегунка множит уже домноженное: замер §48
        на рамке 20×36 при одном и том же 90×90 дал `dx` 10.0 → 45.0 → 202.5
        за два тика, а после «Применить» — 911.25 при полуширине узла 45.
        """
        from ui.editors import port_model

        pins = []
        for e in self.edges_data:
            for role in ('source', 'target'):
                if e.get(role) not in self._resize_sel:
                    continue
                pin = port_model.edge_pin(e, role)
                if pin is not None:
                    pins.append((e, role, float(pin['dx']), float(pin['dy'])))
        return pins

    def _restore_resize_pins(self):
        """Вернуть пины набора к базлайну — БЕЗУСЛОВНО.

        Зовущий один: `_apply_sizes_from_base`, и там безусловность и есть
        механизм идемпотентности. Предпосылка — «базлайн жив ⇒ ПИНОВАЯ база
        актуальна»: оба входа (`preview_resize`, `apply_resize`) сначала
        спрашивают `_resize_baseline_alive()`, а тот с пятого возврата пункта
        сверяет и пиновый отпечаток тоже. ⛔ До этого предпосылка держалась
        не сторожем, а ПЕРЕЧНЕМ команд («команды, пишущей только пины, не
        существует»), и на команде-наследнике ломалась: безусловная запись
        протухшей базы отменяла чужой Ctrl+Z (`MEASUREMENTS §110.18`).
        ⛔ Для ОТКАТА превью этот метод не годится — см.
        `_rollback_owned_resize_pins`.
        """
        from ui.editors import port_model

        for e, role, dx, dy in self._resize_pin_base:
            pin = port_model.edge_pin(e, role)
            if pin is not None:
                pin['dx'], pin['dy'] = dx, dy

    def _snapshot_resize_pins(self) -> list:
        """Отпечаток пинов: что оставило в них превью. Порядок — `_resize_pin_base`."""
        from ui.editors import port_model

        out = []
        for e, role, _dx, _dy in self._resize_pin_base:
            pin = port_model.edge_pin(e, role)
            out.append((float(pin['dx']), float(pin['dy'])) if pin is not None else None)
        return out

    def _rollback_owned_resize_pins(self):
        """Снять след превью с пинов ТАМ, ГДЕ ОН ЕЩЁ НАШ.

        Та же поэлементность, что у `_preview_live_fields` для узлов, и по той
        же причине: пин, который вернул ЧУЖОЙ откат, базлайну не принадлежит,
        и возврат по базлайну отменил бы этот откат (граница §48).

        ⛔ Прежняя граница («ни одна команда-на-месте пинов не трогает»,
        `MEASUREMENTS §97.22`) ОПРОВЕРГНУТА исполнением — четвёртый возврат
        пункта 1-6. Полный перебор `ui/editors/commands/*` (22 класса, греп
        `^class .*Command`; таблица «команда × что возвращает undo» —
        `MEASUREMENTS §106.3`) даёт РОВНО ОДНУ команду-на-месте, чей `undo`
        пишет пины: **`ResizeNodeCommand`** (`simple_commands.py:363` —
        `_apply_pins(self._old_pins)`), штатный угловой ресайз бокса;
        в «Ручной правке» он достижим двойным кликом по equipment-боксу
        (`mouseDoubleClickEvent` → `_enter_resize_mode`). Замер §106.4:
        пин (10,0) → угловой ресайз → «Размеры» → превью → Ctrl+Z (пин честно
        вернулся к (10,0)) → «Сохранить» клало на сервер `dx` 20.0 при
        ОТКАТНОЙ рамке `[25, 215, 45, 251]` — рассинхрон рамка/пин,
        персистентный в JSON.

        ⚠ Граница держится теперь не перечнем команд, а ПРЯМЫМ ответом:
        отпечаток `_resize_pin_preview` говорит, что в пине оставило превью.
        Совпало — след наш, снимаем; разошлось — пин тронул кто-то ещё,
        не трогаем. Поэтому список выше — доказательство, а не условие.

        ⛔ ЗАМЕРЕНО, ГДЕ ИМЕННО ЭТО ВЕРНО (пятый возврат пункта). Прямой ответ
        здесь снимал вопрос только на пути ЗАПИСИ (`drop_uncommitted_preview`
        → `_rollback_owned_preview`; зонд ревизора §110.16/.17). Тот же
        инвариант потребляют ещё два пути — пересъём базлайна в
        `preview_resize` и в `apply_resize`, — и там он держался на перечне,
        пока `_preview_still_in_model` не научился спрашивать пиновый
        отпечаток (§110.18 → §115). Потребители перечислены грепом
        `_resize_baseline_alive` по этому файлу, зонд прогнан на каждом:
        `tests/ui/test_preview_after_foreign_action.py`, половина Д.
        """
        from ui.editors import port_model

        for (e, role, dx, dy), mark in zip(self._resize_pin_base,
                                           self._resize_pin_preview):
            if mark is None:
                continue
            pin = port_model.edge_pin(e, role)
            if pin is None:
                continue                      # чужой откат снял пин — не воскрешаем
            if (float(pin['dx']), float(pin['dy'])) != mark:
                continue                      # в пине уже не наш след
            pin['dx'], pin['dy'] = dx, dy

    def _drop_resize_baseline(self):
        """Забыть базлайн: превью зафиксировано или откачено, возвращать нечего."""
        self._resize_model_base = None
        self._resize_base = {}
        self._resize_pin_base = []
        self._resize_pin_preview = []
        self._resize_model_rev = None
        self._resize_preview_geom = {}

    def _rollback_owned_preview(self) -> dict:
        """Снять превью ТАМ, где оно ещё наше, и забыть базлайн.

        Решение — поэлементное (`_preview_live_fields`), а не «всё или ничего»:
        узлу, чью геометрию уже вернул чужой откат, базлайн не возвращается
        (это отменило бы сам откат — граница §48), а его соседям по набору
        возвращается, потому что их превью никто не трогал.
        """
        live = self._preview_live_fields()
        if not live:
            # Превью не осталось ни у кого: модель пересобрали (undo/redo
            # снимочной команды) либо его и не вписывали. Возвращать нечего —
            # базлайн больше не про эту модель (замер §83б: раньше в эту ветку
            # уходила ЛЮБАЯ правка на месте, и превью оставалось молча).
            self._drop_resize_baseline()
            return {}
        # Пины — тоже ПОЭЛЕМЕНТНО, по своему отпечатку: чужой откат геометрии
        # их не касается (`DragNodeCommand`), но откат ШТАТНОГО углового
        # ресайза касается (`ResizeNodeCommand._apply_pins`) — граница §97.22
        # опровергнута исполнением, замер §106.4. Снять свой след надо
        # обязательно: иначе конец трубы держится по рамке 90×90 при рамке
        # 20×36 у узла, которому откат вернул размер (замер §48: dx 10 → 45).
        self._rollback_owned_resize_pins()
        for nid, fields in live.items():
            n = self.nodes.get(nid)
            base = self._resize_base.get(nid)
            if not n or not base:
                continue
            if 'centroid' in fields:
                n['centroid'] = list(base['centroid'])
            if 'bbox' in fields and base['bbox']:
                n['bbox'] = list(base['bbox'])
            if 'segmentation' in fields and base['segmentation']:
                n['segmentation'] = list(base['segmentation'])
            if 'area' in fields and base['area'] is not None:
                n['area'] = base['area']
        poly_edit_in_base = self._poly_edit_node in self._resize_base
        self._drop_resize_baseline()
        for nid in live:
            self._refresh_node_visual(nid)
        if poly_edit_in_base:
            # Оверлей вершин построен из `segmentation` узла, то есть из
            # ПРЕВЬЮ. Без пересборки следующая же запись контура
            # (`_poly_write_node`) вписала бы превью обратно — уже отдельным
            # шагом undo. Та же пересборка, что после undo/redo.
            self._poly_resync_overlay()
        return live

    def _revert_resize_preview(self):
        """Откатить брошенное превью: набор сменили или вышли из режима.

        Превью мутирует МОДЕЛЬ и в undo не пишет, поэтому без этого отката
        изменённая геометрия оставалась на схеме при пустом стеке отмены —
        вернуть её оператору было нечем (замер §48).
        """
        reverted = self._rollback_owned_preview()
        if not reverted:
            return
        # Штатное событие, не сбой: оператор увидит, почему рамки «вернулись».
        import logging
        logging.getLogger(__name__).info(
            "resize preview: превью не применено — набор (%d) возвращён к исходным "
            "размерам", len(reverted))

    def drop_uncommitted_preview(self) -> None:
        """Снять незафиксированное превью «Размеров». Два зовущих:

        • **запись графа** (1.5): `BaseGraphTab._save_graph` — превью идёт мимо
          стека команд, дёрти-флаг вкладки его не видит, а запись шла от модели
          КАК ЕСТЬ. Замер §50: «превью → Сохранить → выход» оставлял на сервере
          bbox [-10.0, 188.0, 80.0, 278.0] при исходных [25, 215, 45, 251];
        • **любая команда-на-месте** до того, как она снимет свою точку
          возврата (доработка 1.4 по ревизии связки). `SnapshotCommand._before`
          снимается с модели КАК ЕСТЬ, то есть вместе с превью: дальше
          `undo_mgr.revision` растёт, базлайн объявляется протухшим и лечение
          «протух → бросить без отката» превью уже не трогает. Замер §52:
          «превью → Авто-выравнивание → Ctrl+Z» возвращал 90×90 вместо 20×36,
          и то же уезжало на сервер. Поздним откатом это не лечится — `_before`
          отравлен, Ctrl+Z вернул бы превью и из чистой модели.

        Панель не сбрасывается (`_update_resize_panel` затёр бы набранные
        оператором ширину/высоту медианами) — только модель и жёлтые рамки.
        """
        self._revert_resize_preview()
        self._redraw_resize_frames()

    def _undo_point(self):
        """Снимок модели как точка возврата: превью снимается ДО снимка.

        Тот же порядок, что у четырёх кнопок редактора и у воронки OCR-слоя,
        только для путей, которые строят `SnapshotCommand._before` из готового
        снимка (правка полигона). Поздним откатом это не лечится: `_before`
        уже отравлен, и Ctrl+Z вернул бы превью даже из чистой модели.
        """
        self.drop_uncommitted_preview()
        return self.model.snapshot()

    def _apply_sizes_from_base(self, width, height, scale, kind):
        """Применить размеры к набору, отталкиваясь от зафиксированного базлайна.

        Идемпотентно: повторные вызовы (живой бегунок) не накапливают ни
        масштаб рамок, ни смещения пинов.
        """
        self._restore_resize_pins()
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
        # Что именно вписано — отпечаток для `_preview_still_in_model()`:
        # им сторож базлайна отвечает ПРЯМО, а не через `undo_mgr.revision`.
        self._resize_preview_geom = {
            nid: self._preview_geom_key(self.nodes[nid])
            for nid in self._resize_base if nid in self.nodes
        }
        # То же для пинов: `_rollback_owned_resize_pins` снимает след только
        # там, где он совпал с этим отпечатком (четвёртый возврат пункта),
        # и `_preview_still_in_model` этим же отпечатком судит, жив ли
        # базлайн со стороны пинов (пятый возврат, §110.18).
        self._resize_pin_preview = self._snapshot_resize_pins()

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
                 else self.CONNECTOR_DRAW_RADIUS)
            self.node_items[node_id].setRect(cx - r, cy - r, r * 2, r * 2)
        # форма узла изменилась — переставить подсветку сторон (П8)
        self._draw_side_marks(node_id)
        # подогнать скин под новый размер (живой резайз)
        if self.show_skins and node_id in self._skin_items:
            self._update_node_skin(node_id)
        # привязанные текст-блоки следуют за resize цели (гранулярно)
        if hasattr(self, "_refresh_ocr_layer_for_node"):
            self._refresh_ocr_layer_for_node(node_id)

    def preview_resize(self, width=None, height=None, scale=None):
        """Живое превью: визуально меняет размеры набора без перестройки связей.

        Связи/соседи пересчитываются только по кнопке «Применить» (apply_resize).
        """
        if not self._resize_sel:
            return
        kind = self._resize_kind()
        if kind in ('mixed', 'empty'):
            return
        if not self._resize_baseline_alive():
            # Базлайн целиком не годится — но у части набора превью ещё наше,
            # и пересъём ПОВЕРХ него сделал бы превью новой «нормой»
            # (на полигонах это масштаб в квадрате, §97.2). Снять своё,
            # и только потом снимать базлайн заново.
            self._rollback_owned_preview()
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
        # Э5: пины инцидентных рёбер — локальные смещения от центроида,
        # при смене рамки масштабируются (иначе якорь уползает с грани)
        if bb and len(bb) == 4:
            from ui.editors import port_model
            port_model.rescale_edge_pins(node, self.edges_data,
                                         bb, node['bbox'])

    def _resize_node_poly(self, node: dict, scale: float):
        """Масштабировать полигон вокруг центроида с сохранением формы."""
        seg = node.get('segmentation')
        if not seg or len(seg) < 6:
            return
        cy, cx = node['centroid'][0], node['centroid'][1]
        old_bb = list(node.get('bbox') or [])
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
        # Э5: пины инцидентных рёбер масштабируются вместе с формой
        if len(old_bb) == 4:
            from ui.editors import port_model
            port_model.rescale_edge_pins(node, self.edges_data,
                                         old_bb, node['bbox'])

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
        # Э2d: кого сдвинули — тем пересадить рёбра (раньше концы висели)
        return [nid for nid in active if nid not in growers]

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
        if not self._resize_baseline_alive():
            # То же, что в `preview_resize`: пересъём поверх живого превью
            # вмуровал бы его И в размер, И в точку возврата `cmd._before`
            # (замер §97.2: полигон 605×75 → ×2 → «Применить» ×2 → 2420×300,
            # и 605×75 недостижимы никаким числом Ctrl+Z).
            self._rollback_owned_preview()
            self._capture_resize_base()
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd._before = self._resize_model_base
        cmd.description = "Размер объектов"

        try:
            # Применить размеры из базлайна (идемпотентно — итог совпадает с превью).
            self._apply_sizes_from_base(width, height, scale, kind)
            grower_ids = [nid for nid in self._resize_sel if self.nodes.get(nid)]

            # развести наслоившихся соседей, затем довести рёбра до ортогональности
            pushed = self._spread_overlaps(grower_ids)
            # Э2d: рёбра всех затронутых узлов пересаживаются движком (только
            # ближние концы); раньше на layout-холсте концы сдвинутых соседей
            # оставались висеть (auto_fix ниже заперт замком Э4-00)
            for nid in dict.fromkeys(list(grower_ids) + pushed):
                self._reseat_after_resize(nid)
            # Э4-00: на холсте после авто-раскладки полнографный auto_fix даёт
            # регрессию (замер §1.1 EDITOR_AFTER_LAYOUT_PLAN) — тот же инструмент,
            # что заперт кнопкой «Авто-выравнивание»; расталкивание выше остаётся
            # локальным вокруг изменённых узлов.
            from modules.graph.core import canvas_state
            if not canvas_state.has_layout(self.graph_data or {}):
                auto_fix_graph(
                    self.nodes, self.edges_data,
                    equip_max_shift=self.EQUIP_MAX_SHIFT,
                    conn_max_shift=self.CONN_MAX_SHIFT,
                )
        finally:
            # Закрытие шага undo — в finally (пункт 1.x8, третий носитель формы
            # 1.1/1.x7). Все четыре мутатора выше правят узлы и рёбра НА МЕСТЕ,
            # и падение любого из них оставляло оператора с изменённым холстом,
            # без Ctrl+Z и без роста revision — вкладка считала, что
            # несохранённого нет, и закрывалась без вопроса (замер §73в).
            # Перерисовка здесь же: иначе сцена показывает прежнюю рамку поверх
            # уже изменённой модели. Порядок тот же, что на зелёном пути.
            self._redraw_all()
            self.model.rebuild_edge_data_index()
            cmd.finalize()
            self.undo_mgr.push_executed(cmd)

        # Превью зафиксировано командой — базлайн больше не точка возврата.
        # Снять его ДО сброса панели: та откатывает незафиксированное превью
        # и иначе отменила бы только что применённое.
        self._drop_resize_baseline()
        # Обновить панель/рамки от нового состояния.
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

    @staticmethod
    def _crop_alpha(pm: QPixmap) -> QPixmap:
        """Обрезать прозрачные поля PNG (у скинов они ~8px по краям).

        _fit_pixmap вписывает в bbox весь файл вместе с полями, поэтому графика
        садится уже бокса и труба до неё не доходит. После обрезки аспект файла
        сходится с aspect_hw из skin_geometry.json (по нему считается content_rect,
        куда pretransform сажает концы труб).
        """
        img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return pm
        try:
            import numpy as np
            arr = np.frombuffer(img.constBits(), np.uint8, count=h * img.bytesPerLine())
            alpha = arr.reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4)[:, :, 3]
            rows = np.flatnonzero(alpha.any(axis=1))
            cols = np.flatnonzero(alpha.any(axis=0))
        except Exception:
            logger.exception("crop_alpha: не удалось прочитать альфу, беру PNG как есть")
            return pm
        if not len(rows) or not len(cols):
            return pm
        return pm.copy(int(cols[0]), int(rows[0]),
                       int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1))

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
                    pm = self._crop_alpha(img)
        self._skin_pixmaps[class_name] = pm
        return pm

    def set_show_skins(self, on: bool):
        """Включить/выключить предпросмотр FXML: скины символов + цвет и толщина рёбер."""
        self.show_skins = bool(on)
        # Не только скины: при включённых скинах рёбра показывают свои
        # render_color/render_width, поэтому нужна полная перерисовка
        # (_redraw_all сам зовёт _redraw_skins).
        self._redraw_all()
        self.update_status("Скины " + ("показаны" if self.show_skins else "скрыты"))

    def _clear_skin_items(self):
        # _reset_scene_state НЕ чистит _skin_items, а _redraw_all снимает со
        # сцены всё с z>0 — к моменту следующего _redraw_skins ссылки здесь уже
        # отвязаны от сцены. Без проверки Qt печатает предупреждение на КАЖДЫЙ
        # скин («item's scene (0x0) is different from this scene»), забивая лог.
        for items in self._skin_items.values():
            for it in items:
                try:
                    if it.scene() is not None:
                        self.scene.removeItem(it)
                except RuntimeError:
                    pass          # C++-объект уже удалён (scene.clear())
        self._skin_items.clear()

    def _fit_pixmap(self, item: QGraphicsPixmapItem, pm: QPixmap,
                    x1: float, y1: float, w: float, h: float):
        """Растянуть пиксмап на ВЕСЬ bbox (решение заказчика 2026-08-01
        «символ тянется на рамку» — как контрол в FXML/SceneBuilder;
        letterbox давал трубы «внутри бокса» при непропорциональном
        resize, репро graph_edited_3edge)."""
        pw, ph = pm.width(), pm.height()
        if pw <= 0 or ph <= 0 or w <= 0 or h <= 0:
            return
        t = QTransform()
        t.scale(w / pw, h / ph)
        item.setTransform(t)
        item.setPos(x1, y1)

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
        """Обновить ориентацию/вписывание скина под текущий bbox."""
        items = self._skin_items.get(node_id)
        node = self.nodes.get(node_id)
        if not items or not node:
            return
        item = items[0]
        bb = node.get("bbox")
        if not bb or len(bb) != 4:
            return
        x1, y1, x2, y2 = bb
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            return
        # Стрелка napravlenie — полигон, а не пиксмап: пересобираем по новому bbox
        # (направление могло смениться вместе с рёбрами).
        if isinstance(item, QGraphicsPolygonItem):
            poly = self._napravlenie_polygon(node_id, node)
            if poly is not None:
                item.setPolygon(poly)
            return
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
        # Скин без подложки: прозрачный фон PNG пропускает лист, рёбра и рамку
        # бокса. Z ниже маркера центроида (3), но выше рамки (2) — иначе символ
        # закрывает собой центроид.
        item = QGraphicsPixmapItem()
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(2.5)
        self.scene.addItem(item)
        self._skin_items[node_id] = [item]
        self._apply_skin_geometry(node_id)

    def _napravlenie_direction(self, node_id: str, node: dict) -> str:
        """Направление стрелки: из графа, иначе по рёбрам (как в экспорте)."""
        direction = node.get("flow_direction") or node.get("direction")
        if direction in ("up", "down", "left", "right"):
            return direction
        # Ручной узел не проходит annotate_direction_nodes — выводим по рёбрам
        # тем же правилом, что и экспорт.
        from modules.graph_to_fxml import _infer_napravlenie_direction
        return _infer_napravlenie_direction(node, node_id, self.edges_data, self.nodes)

    def _napravlenie_polygon(self, node_id: str, node: dict):
        """QPolygonF стрелки napravlenie или None. Геометрия — из FXML-модуля,
        чтобы редактор и SceneBuilder рисовали один и тот же треугольник."""
        from modules.graph_to_fxml import napravlenie_triangle_points
        pts = napravlenie_triangle_points(
            node.get("bbox"), self._napravlenie_direction(node_id, node))
        if not pts:
            return None
        return QPolygonF([QPointF(x, y) for x, y in pts])

    def _napravlenie_fill(self, node_id: str, node: dict) -> QColor:
        """Заливка стрелки = цвет своей (входящей) трубы; иначе дефолт класса."""
        from modules.graph_to_fxml import (
            CLASS_COLORS, NAPRAVLENIE_COLOR, NAPRAVLENIE_CLASS_NAME,
            napravlenie_incoming_color,
        )
        rc = napravlenie_incoming_color(
            node, node_id, self.edges_data, self.nodes,
            self._napravlenie_direction(node_id, node))
        return QColor(rc or CLASS_COLORS.get(NAPRAVLENIE_CLASS_NAME, NAPRAVLENIE_COLOR))

    def _add_direction_arrow(self, node_id: str, node: dict):
        """Стрелка napravlenie: в FXML это Polygon-треугольник, а не скин."""
        poly = self._napravlenie_polygon(node_id, node)
        if poly is None:
            return
        item = QGraphicsPolygonItem(poly)
        item.setBrush(QBrush(self._napravlenie_fill(node_id, node)))
        item.setPen(QPen(QColor(_NAPRAVLENIE_STROKE), self.OUTLINE_WIDTH))
        item.setZValue(2.5)   # как у скинов: над рамкой, под центроидом
        self.scene.addItem(item)
        self._skin_items[node_id] = [item]

    def _redraw_skins(self):
        """Перерисовать все скины по текущему состоянию (вызывается в _redraw_all)."""
        self._clear_skin_items()
        if not self.show_skins:
            return
        for nid, node in self.nodes.items():
            if node.get("type") != "equipment":
                continue
            if node.get("class_name") == _NAPRAVLENIE_CLASS or node.get("direction_node"):
                self._add_direction_arrow(nid, node)
                continue
            pm = self._skin_pixmap_for(node.get("class_name"))
            if pm is None:
                continue
            self._add_node_skin(nid, node, pm)

    def _update_node_skin(self, node_id: str):
        """Подогнать скин узла под текущий bbox (для живого резайза)."""
        self._apply_skin_geometry(node_id)

    def _draw_single_node(self, node_id: str):
        """Отрисовка узла + скин для нового equipment-бокса (добавление/вставка)."""
        super()._draw_single_node(node_id)
        # защита от дубля: при полной перерисовке скины создаёт _redraw_skins
        if not self.show_skins or node_id in self._skin_items:
            return
        node = self.nodes.get(node_id)
        if not node or node.get('type') != 'equipment':
            return
        pm = self._skin_pixmap_for(node.get('class_name'))
        if pm is not None:
            self._add_node_skin(node_id, node, pm)

    def remove_node_items(self, node_id: str):
        """Удалить визуальные элементы узла вместе со скином (симметрично добавлению)."""
        super().remove_node_items(node_id)
        items = self._skin_items.pop(node_id, None)
        if items:
            for it in items:
                try:
                    self.scene.removeItem(it)
                except Exception:
                    pass

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
            # В состоянии ОКР выделенные блоки удаляются клавишей Delete
            # (Ctrl+ПКМ пачку больше не удаляет — только исключает из выделения).
            if self.display_regime == "ocr" and getattr(self, "_selected_ocr", None):
                self._delete_selected_ocr_blocks()
                self.update_status("Выделенные блоки удалены")
                return
            self.batch_delete()
            return
        elif event.key() == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return
        elif event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Пока активен призрак вставки — буфер менять нельзя (фиксация
            # вставляет текущий буфер; иначе призрак разойдётся с содержимым).
            if self._paste_ghost is not None:
                return
            # Копипаст взаимоисключающий по состоянию: ocr → блоки, base → узлы.
            if self.display_regime == "ocr":
                self._ocr_copy_blocks()
                return
            if self.display_regime == "base" and self._node_drag_allowed():
                self._copy_selected_nodes()
                return
        elif event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+V теперь показывает призрак вставки (фиксация — Ctrl+ЛКМ,
            # отмена — Esc); повторный Ctrl+V при активном призраке — игнор.
            if self.display_regime == "ocr":
                self._start_paste_ghost()
                return
            if self.display_regime == "base" and self._node_drag_allowed():
                self._start_paste_ghost()
                return
        elif event.key() == Qt.Key.Key_G:
            self.toggle_grid()
            return
        super().keyPressEvent(event)

    # =================================================================
    # Копипаст подсистемы: узлы + рёбра + тексты (Ctrl+C/Ctrl+V, base + idle)
    # =================================================================

    def _cursor_scene_pos(self):
        """Позиция курсора мыши в координатах сцены.

        Курсор вне вьюпорта → fallback в центр вьюпорта.
        """
        vp = self.viewport()
        pt = vp.mapFromGlobal(QCursor.pos())
        if not vp.rect().contains(pt):
            pt = vp.rect().center()
        return self.mapToScene(pt)

    def _node_for_copy_at(self, x: float, y: float) -> str | None:
        """Узел под курсором для копипаста/выделения (любой тип).

        Сначала штатный find_node_at (радиус от центроида — equipment И
        connector), затем попадание точки внутрь bbox оборудования (наименьший
        бокс — как _ocr_block_at), чтобы работало наведение на любую точку
        бокса, не только на центроид.
        """
        nid = self.find_node_at(x, y)
        if nid:
            return nid
        best_id, best_area = None, None
        for node_id, node in self.nodes.items():
            if node.get('type') != 'equipment':
                continue
            bb = node.get('bbox')
            if not bb or len(bb) != 4:
                continue
            if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]:
                area = abs((bb[2] - bb[0]) * (bb[3] - bb[1]))
                if best_area is None or area < best_area:
                    best_id, best_area = node_id, area
        return best_id

    def _copy_selected_nodes(self):
        """Ctrl+C: скопировать подсистему — узлы, рёбра между ними, тексты.

        Приоритет у узла под курсором (любого типа): узел вне выделения →
        выделение переключается на него; узел в составе выделения или курсор
        в пустоте → копируется текущее выделение. В буфер идут:
          • nodes — deepcopy ВСЕХ выделенных узлов (equipment и connector);
          • edges — рёбра из selected_edges, у которых ОБА конца копируются
            (остальные пропускаются и считаются);
          • texts — text_blocks, чья привязка указывает на копируемый
            узел/ребро (deepcopy блока + привязки с side/gap/text/kind).
        Скопированное остаётся выделенным (штатная подсветка).
        """
        pos = self._cursor_scene_pos()
        cur = self._node_for_copy_at(pos.x(), pos.y())
        if cur is not None and cur not in self.selected_nodes:
            # выделение = содержимое буфера: переключить на узел под курсором
            self.selected_nodes = {cur}
            self.selected_edges.clear()
            self._update_selection_visuals()
        ids = [nid for nid in self.selected_nodes if nid in self.nodes]
        if not ids:
            self.update_status("Копировать: выделите узлы или наведите курсор на узел")
            return
        nodes_clip = [deepcopy(self.nodes[nid]) for nid in ids]
        id_set = set(ids)

        # Рёбра: только с обоими концами среди копируемых узлов
        edges_clip = []
        skipped_edges = 0
        for key in self.selected_edges:
            ed = self.model.find_edge_data(key)
            if ed and ed.get('source') in id_set and ed.get('target') in id_set:
                edges_clip.append(deepcopy(ed))
            else:
                skipped_edges += 1

        # Тексты: привязки, указывающие на копируемые узлы/рёбра
        copied_keys = {self.model.edge_key(e['source'], e['target'])
                       for e in edges_clip}
        texts_clip = []
        for b in self.model.bindings:
            if not (b.get("node_id") in id_set
                    or self.model.binding_edge_key(b) in copied_keys):
                continue
            blk = self.model.find_text_block(b.get("block_id"))
            if not blk or blk.get("merged_into") is not None:
                continue
            bbox = blk.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            texts_clip.append((deepcopy(blk), deepcopy(b)))

        self._node_clipboard = {
            "nodes": nodes_clip, "edges": edges_clip, "texts": texts_clip,
        }
        msg = f"Скопировано: {len(nodes_clip)} узлов, {len(edges_clip)} рёбер"
        if skipped_edges:
            msg += f" (пропущено рёбер: {skipped_edges})"
        msg += f", {len(texts_clip)} текстов (Ctrl+V — вставить)"
        self.update_status(msg)

    def _clip_nodes_bbox(self, nodes: list) -> tuple:
        """Общий bbox набора узлов буфера: bbox оборудования или centroid ± r
        коннектора. По нему считается центр набора для призрака/вставки."""
        # Радиус здесь ГЕОМЕТРИЧЕСКИЙ (не DRAW): от него зависят координаты
        # вставки — ползунок «размер коннекторов» двигать их не должен.
        r = self.CONNECTOR_MARKER_RADIUS
        xs1, ys1, xs2, ys2 = [], [], [], []
        for n in nodes:
            bb = n.get('bbox')
            if n.get('type') == 'equipment' and bb and len(bb) == 4:
                xs1.append(float(bb[0])); ys1.append(float(bb[1]))
                xs2.append(float(bb[2])); ys2.append(float(bb[3]))
            else:
                c = n.get('centroid') or [0.0, 0.0]  # [y, x]
                xs1.append(float(c[1]) - r); ys1.append(float(c[0]) - r)
                xs2.append(float(c[1]) + r); ys2.append(float(c[0]) + r)
        return min(xs1), min(ys1), max(xs2), max(ys2)

    def _paste_node_clipboard(self, dx: float, dy: float):
        """Зафиксировать вставку буфера подсистемы со сдвигом (dx, dy).

        Вызывается фиксацией призрака (Ctrl+V → призрак → Ctrl+ЛКМ). Создаёт:
          • узлы (equipment И connector) с новыми id той же механикой, что
            фабрики graph_data (manual_node_counter / node_manual_{n}), все
            поля сохраняются, геометрия сдвигается;
          • рёбра между НОВЫМИ id (маппинг old→new, сдвиг
            source_point/target_point/waypoints);
          • тексты: create_text_block + set_binding на новую цель (side/gap
            сохраняются).
        Взаимное расположение сохраняется. Вся вставка — одна SnapshotCommand,
        т.е. отменяется одним Ctrl+Z.
        """
        from ui.editors.undo_manager import SnapshotCommand
        clip = self._node_clipboard
        if not clip or not clip.get("nodes"):
            self.update_status("Буфер узлов пуст — сначала Ctrl+C")
            return

        # Призрак вставки переживает смену инструмента (гасит его только
        # перерисовка сцены), поэтому фиксация достижима при живом превью
        # «Размеров»: Ctrl+V в базовом → кнопка «Размеры» → Ctrl+ЛКМ (§86.13).
        # Точка возврата — ниже (см. drop_uncommitted_preview); граница И5
        # соблюдена ранним выходом «буфер пуст» выше.
        self.drop_uncommitted_preview()
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd.execute()
        cmd.description = "Вставить узлы"

        # Узлы: deepcopy всех полей, новый id, сдвиг centroid/bbox/segmentation
        id_map: dict[str, str] = {}
        for src in clip["nodes"]:
            node = deepcopy(src)
            self.model.manual_node_counter += 1
            new_id = f"node_manual_{self.model.manual_node_counter}"
            node["id"] = new_id
            node["manual"] = True
            node["yolo_idx"] = None
            node["degree"] = 0  # пересчитается по вставленным рёбрам ниже
            c = node.get("centroid") or [0.0, 0.0]
            node["centroid"] = [float(c[0]) + dy, float(c[1]) + dx]  # [y, x]
            bb = node.get("bbox")
            if bb and len(bb) == 4:
                node["bbox"] = [float(bb[0]) + dx, float(bb[1]) + dy,
                                float(bb[2]) + dx, float(bb[3]) + dy]
            seg = node.get("segmentation")
            if seg and isinstance(seg, list):
                # segmentation — плоский список [x, y, x, y, ...]
                node["segmentation"] = [float(v) + (dx if i % 2 == 0 else dy)
                                        for i, v in enumerate(seg)]
            self.model.add_node(node)
            id_map[src["id"]] = new_id

        # Рёбра: между новыми id; точки в формате [y, x]
        new_edge_keys: list[tuple] = []
        for src_e in clip.get("edges", []):
            ns, nt = id_map.get(src_e.get("source")), id_map.get(src_e.get("target"))
            if not ns or not nt:
                continue  # копируются только рёбра с обоими концами в буфере
            e = deepcopy(src_e)
            self.model.manual_edge_counter += 1
            e["id"] = f"edge_manual_{self.model.manual_edge_counter}"
            e["source"], e["target"] = ns, nt
            e["manual"] = True
            for pt in (e.get("source_point"), e.get("target_point")):
                if pt and len(pt) == 2:
                    pt[0] += dy
                    pt[1] += dx
            for wp in e.get("waypoints") or []:
                wp[0] += dy
                wp[1] += dx
            self.model.add_edge(ns, nt, e)
            new_edge_keys.append(self.model.edge_key(ns, nt))
            for nid in (ns, nt):
                self.model.nodes[nid]["degree"] = \
                    self.model.nodes[nid].get("degree", 0) + 1

        # Тексты: новый блок + привязка на НОВУЮ цель (side/gap сохраняются)
        n_texts = 0
        for blk_src, bind_src in clip.get("texts", []):
            binding = {
                "kind": bind_src.get("kind"),
                "text": bind_src.get("text") or "",
            }
            if bind_src.get("node_id"):
                binding["node_id"] = id_map.get(bind_src["node_id"])
                if not binding["node_id"]:
                    continue
            elif bind_src.get("edge_key") and "|" in str(bind_src["edge_key"]):
                a, b = str(bind_src["edge_key"]).split("|", 1)
                na, nb = id_map.get(a), id_map.get(b)
                if not na or not nb:
                    continue
                binding["edge_key"] = f"{min(na, nb)}|{max(na, nb)}"
            else:
                continue
            bb = blk_src.get("bbox")
            blk = self.model.create_text_block(
                [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy],
                text=blk_src.get("text") or "",
                confidence=blk_src.get("confidence") or 0.0,
                source="manual")
            self.model.add_text_block(blk)
            binding["block_id"] = blk["id"]
            if bind_src.get("side") in ("top", "right", "left", "bottom"):
                binding["side"] = bind_src["side"]
                if bind_src.get("gap") is not None:
                    binding["gap"] = bind_src["gap"]
            self.model.set_binding(binding)
            n_texts += 1

        cmd.finalize()
        self.undo_mgr.push_executed(cmd)

        # Полная перерисовка (узлы + рёбра + OCR-слой) + выделение вставленных
        self._redraw_all()
        self.selected_nodes = set(id_map.values())
        self.selected_edges = set(new_edge_keys)
        self._update_selection_visuals()
        self.update_statistics()
        self.update_status(
            f"Вставлено: {len(id_map)} узлов, {len(new_edge_keys)} рёбер, "
            f"{n_texts} текстов")

    # =================================================================
    # Призрак вставки (Ctrl+V → следует за мышью → Ctrl+ЛКМ фиксирует)
    # =================================================================

    def _start_paste_ghost(self):
        """Ctrl+V: показать полупрозрачный призрак буфера, следующий за мышью.

        Вставка больше не мгновенная: Ctrl+ЛКМ фиксирует набор в позиции
        призрака, Esc отменяет. Повторный Ctrl+V при активном призраке — игнор.
        Пока призрак активен, все прочие жесты мыши в этом view отключены.
        """
        if self._paste_ghost is not None:
            return  # призрак уже активен
        items: list = []   # контурные item'ы (получают общий пунктирный pen)
        deco: list = []    # скины/подписи — со своим стилем, pen не трогаем
        if self.display_regime == "ocr":
            clip = getattr(self, "_ocr_clipboard", [])
            if not clip:
                self.update_status("Буфер блоков пуст — сначала Ctrl+C")
                return
            # Призрак OCR: рамки блоков + призрачная подпись с текстом
            for bbox, text in clip:
                items.append(QGraphicsRectItem(
                    bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1]))
                deco.extend(self._ghost_label_items(bbox, text))
            gx1 = min(b[0][0] for b in clip)
            gy1 = min(b[0][1] for b in clip)
            gx2 = max(b[0][2] for b in clip)
            gy2 = max(b[0][3] for b in clip)
            kind = "ocr"
        else:
            clip = self._node_clipboard
            if not clip or not clip.get("nodes"):
                self.update_status("Буфер узлов пуст — сначала Ctrl+C")
                return
            # Рамки bbox оборудования / кружки коннекторов
            for n in clip["nodes"]:
                bb = n.get('bbox')
                if n.get('type') == 'equipment' and bb and len(bb) == 4:
                    items.append(QGraphicsRectItem(
                        bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]))
                    w, h = bb[2] - bb[0], bb[3] - bb[1]
                    pm = self._skin_pixmap_for(n.get('class_name')) \
                        if self.show_skins else None
                    if pm is not None:
                        # скин-призрак: видно, какой блок едет (имя не дублируем);
                        # ориентация по аспекту — копия ещё не в графе, рёбер нет
                        disp = self._oriented_skin_pixmap(
                            pm, 'HORIZONTAL' if w > h else 'VERTICAL')
                        spi = QGraphicsPixmapItem(disp)
                        self._fit_pixmap(spi, disp, bb[0], bb[1], w, h)
                        deco.append(spi)
                    elif n.get('class_name'):
                        # без скина блок опознаётся по имени класса
                        lbl = QGraphicsSimpleTextItem(str(n['class_name']))
                        lbl.setFont(QFont(self.font().family(), 9))
                        lbl.setBrush(QBrush(self.COLOR_SELECTION))
                        lbl.setPos(bb[0], bb[1] - 16)
                        deco.append(lbl)
                else:
                    c = n.get('centroid') or [0.0, 0.0]  # [y, x]
                    r = self.CONNECTOR_DRAW_RADIUS
                    items.append(QGraphicsEllipseItem(
                        c[1] - r, c[0] - r, r * 2, r * 2))
            # Полилинии рёбер: source_point → waypoints → target_point
            for e in clip.get("edges", []):
                items.append(QGraphicsPathItem(self._build_edge_path(
                    e.get('source_point'), e.get('waypoints', []),
                    e.get('target_point'))))
            # Рамки текстов + призрачная подпись с текстом
            for blk, _b in clip.get("texts", []):
                bb = blk.get('bbox')
                if bb and len(bb) == 4:
                    items.append(QGraphicsRectItem(
                        bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]))
                    deco.extend(self._ghost_label_items(bb, blk.get('text')))
            gx1, gy1, gx2, gy2 = self._clip_nodes_bbox(clip["nodes"])
            kind = "nodes"

        # Общая группа: item'ы в исходных координатах, двигаем одним setPos
        # (не пересоздаём на каждый move); пунктир, полупрозрачно, поверх всего.
        pen = QPen(self.COLOR_SELECTION, 2, Qt.PenStyle.DashLine)
        group = QGraphicsItemGroup()
        for it in items:
            it.setPen(pen)
            group.addToGroup(it)
        for it in deco:   # скин/подписи — свой стиль, общий pen не применяем
            group.addToGroup(it)
        group.setZValue(200)
        group.setOpacity(0.5)
        self.scene.addItem(group)
        self._paste_ghost = {
            "kind": kind, "group": group,
            "center": ((gx1 + gx2) / 2.0, (gy1 + gy2) / 2.0),
        }
        pos = self._cursor_scene_pos()
        self._move_paste_ghost(pos.x(), pos.y())
        self.update_status("Ctrl+ЛКМ — вставить, Esc — отмена")

    def _move_paste_ghost(self, x: float, y: float):
        """Призрак следует за мышью: центр набора — под курсором."""
        g = self._paste_ghost
        if not g:
            return
        ccx, ccy = g["center"]
        g["group"].setPos(x - ccx, y - ccy)

    def _commit_paste_ghost(self):
        """Ctrl+ЛКМ: зафиксировать вставку в позиции призрака."""
        g = self._paste_ghost
        if not g:
            return
        offset = g["group"].pos()  # сдвиг призрака относительно оригинала
        kind = g["kind"]
        self._cancel_paste_ghost()
        if kind == "ocr":
            self._ocr_paste_blocks(offset.x(), offset.y())
        else:
            self._paste_node_clipboard(offset.x(), offset.y())

    def _cancel_paste_ghost(self):
        """Убрать призрак вставки со сцены (Esc / перед фиксацией)."""
        g = self._paste_ghost
        self._paste_ghost = None
        if g and g["group"].scene() is not None:
            self.scene.removeItem(g["group"])

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
        # Призрак вставки (Ctrl+V) — отменить первым приоритетом.
        if self._paste_ghost is not None:
            self._cancel_paste_ghost()
            self.update_status("Вставка отменена")
            return

        # Ручки изменения размера OCR-блока — снять первыми (не выходя из ОКР).
        if getattr(self, "_ocr_resize_overlay", None) is not None:
            self._hide_ocr_block_resize()
            return

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

    def _on_ctrl_lmb_click(self, x: float, y: float, node_id: str | None):
        """Ctrl+ЛКМ клик (без drag) на узле."""
        # Правка полигона: одиночный Ctrl+ЛКМ по вершине (без drag) — ничего.
        if self._poly_edit_node:
            return
        # Расширение выделения: отложенный клик по объекту, которого
        # find_node_at не видит (equipment по bbox / ребро) — цель взведена
        # в mousePressEvent только при активном выделении в base + idle.
        if self._ext_click_target is not None:
            kind, obj = self._ext_click_target
            self._ext_click_target = None
            if kind == "node":
                self.toggle_select_node(obj)
            else:
                self.toggle_select_edge(obj)
            return
        # Расширение выделения по центроиду: клик по НЕвыделенному узлу при
        # активном выделении добавляет его (base-состояние, инструмент idle).
        if (self.display_regime == "base" and self._node_drag_allowed()
                and (self.selected_nodes or self.selected_edges)
                and node_id is not None and node_id not in self.selected_nodes):
            self.toggle_select_node(node_id)
            return
        # Режим «Размер объектов»: Ctrl+ЛКМ добавляет экземпляр в набор.
        if self._current_mode == "resize_objects":
            self._resize_handle_ctrl_click(x, y)
            return
        # В режиме «Размер и цвет» клик по узлу ничего не красит — узлы только тащим.
        if self._current_mode in ("edit_edge_color", "edit_edge_size", "edit_edge_dash"):
            return
        # Ctrl+клик идёт в handler (add_edge, add_connector, delete_node и т.п.)
        if self._current_handler:
            self._current_handler.on_press(self, x, y, None)

    def _start_ctrl_drag(self, node_id: str):
        """Ctrl+ЛКМ drag → начать перетаскивание узла.

        Перетаскивание доступно во всех режимах, кроме «Размер объектов»
        (там Ctrl+ЛКМ только набирает экземпляры, узлы двигать нельзя).
        """
        # Жест ушёл в drag → отложенный клик расширения выделения отменяется
        # (для целей без node_id — bbox/ребро — drag не начинается, как раньше).
        self._ext_click_target = None
        # Правка полигона: Ctrl+ЛКМ по вершине → тянем вершину, а не узел.
        if (self._poly_edit_node and self._poly_overlay
                and self._poly_overlay.phase == "edit"):
            vtx = self._poly_overlay.find_vertex_at(
                self._ctrl_lmb_start_x, self._ctrl_lmb_start_y)
            if vtx is not None:
                self._poly_op_before = self._undo_point()
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
        # Призрак вставки: все прочие жесты мыши отключены;
        # Ctrl+ЛКМ фиксирует вставку в позиции призрака.
        if self._paste_ghost is not None:
            if event.button() == Qt.MouseButton.LeftButton and (
                    self.ctrl_pressed
                    or event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._commit_paste_ghost()
            else:
                self.update_status("Ctrl+ЛКМ — вставить, Esc — отмена")
            event.accept()
            return
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
        # Расширение выделения (base + idle): при активном выделении Ctrl+ЛКМ
        # клик должен добавлять и объекты, которых find_node_at не видит
        # (equipment по любой точке bbox, ребро) — заводим отложенный
        # клик/drag с той же механикой порога, что у базового класса.
        if (self.ctrl_pressed and event.button() == Qt.MouseButton.LeftButton
                and not self._poly_edit_node
                and self.display_regime == "base" and self._node_drag_allowed()
                and (self.selected_nodes or self.selected_edges)):
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            if self.find_node_at(x, y) is None:
                target = None
                nid = self._node_for_copy_at(x, y)
                if nid is not None and nid not in self.selected_nodes:
                    target = ("node", nid)
                elif nid is None:
                    edge_key, _ = self.find_nearest_edge(x, y)
                    if edge_key is not None and edge_key not in self.selected_edges:
                        target = ("edge", edge_key)
                if target is not None:
                    self._ext_click_target = target
                    self._ctrl_lmb_pending = True
                    self._ctrl_lmb_start_x = x
                    self._ctrl_lmb_start_y = y
                    self._ctrl_lmb_node = None  # drag за такую точку не начинается
                    self._ctrl_lmb_dragging = False
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """Пока активен призрак вставки — жесты мыши не доходят до редактора."""
        if self._paste_ghost is not None:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _enter_polygon_editing_mode(self, node_id: str):
        """Войти в правку точек полигона узла (Ctrl+2ЛКМ по полигону в базовом).

        Каждая операция (сдвиг/добавление/удаление вершины) — отдельный шаг undo.
        """
        node = self.nodes.get(node_id)
        seg = node.get('segmentation') if node else None
        if not (seg and isinstance(seg, list) and len(seg) >= 6):
            return
        # Инструмент «Размеры» этот вход НЕ гасит: `set_mode` тут не зовётся,
        # а ветка полигона в `mousePressEvent` стоит раньше сторожа резайза —
        # режим остаётся `resize_objects` с живым превью (§83.26). Оверлей
        # строится из `segmentation` узла, поэтому превью снимается ДО него:
        # иначе оператор правит вершины уже масштабированного контура, и
        # первая же операция впишет превью в свой шаг undo.
        self.drop_uncommitted_preview()
        seg = node.get('segmentation')      # откат мог вернуть контур к базлайну
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
        """Записать полигон из оверлея в узел + пересчитать bbox/центроид/визуал
        + ПЕРЕСАДИТЬ КОНЦЫ ЕГО РЁБЕР.

        Репро заказчика (graph_edited_size.json, 2026-08-02): «изменил размеры
        полигона — рёбра висят». Правка вершин меняет форму, рамку И центроид
        узла, а концы труб оставались на прежних местах — в воздухе рядом со
        старой границей (node_102: 12.8 и 14.6 px от новой фигуры). Лечилось
        это лишь миграцией при следующем ОТКРЫТИИ холста, то есть оператор
        видел висящие трубы всю сессию.
        """
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
        old_c = node.get('centroid') or [0.0, 0.0]
        new_c = [sum(ys) / len(ys), sum(xs) / len(xs)]  # [y, x]
        # Пины хранятся смещением ОТ ЦЕНТРОИДА: правка вершин двигает
        # центроид, но закреплённую оператором точку двигать не должна —
        # дельта компенсируется в смещениях пинов инцидентных рёбер.
        dcy, dcx = new_c[0] - float(old_c[0]), new_c[1] - float(old_c[1])
        if dcx or dcy:
            from ui.editors import port_model
            nid = node.get('id')
            for e in self.edges_data:
                for role in ('source', 'target'):
                    if e.get(role) != nid:
                        continue
                    pin = port_model.edge_pin(e, role)
                    if pin is not None:
                        pin['dx'] = float(pin['dx']) - dcx
                        pin['dy'] = float(pin['dy']) - dcy
        node['centroid'] = new_c
        node['area'] = (node['bbox'][2] - node['bbox'][0]) * \
                       (node['bbox'][3] - node['bbox'][1])
        self._refresh_node_visual(self._poly_edit_node)
        # Тот же мини-жест, что после resize: пересаживаются ТОЛЬКО ближние
        # концы (дальние неприкосновенны, C6), маршруты перестраивает
        # оконная libavoid-сессия; вход с пином держит seat_end. Стоит ДО
        # _poly_push у всех трёх вызывающих (двинул/добавил/удалил
        # вершину) — значит пересадка попадает в тот же шаг undo.
        self._reseat_after_resize(self._poly_edit_node)

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
        before = self._undo_point()
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

        # Призрак вставки следует за мышью; остальные жесты отключены.
        if self._paste_ghost is not None:
            self._move_paste_ghost(x, y)
            event.accept()
            return

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
                    # Взаимоисключение: перед правкой текста снять ручки размера.
                    if hasattr(self, "_hide_ocr_block_resize"):
                        self._hide_ocr_block_resize()
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
