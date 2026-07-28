"""
OCR Binding Editor — визуальный редактор привязки OCR текста к узлам/рёбрам графа.

Единый UX: Ctrl + drag OCR-бокса →
  - На другой OCR-бокс  = слияние
  - На узел equipment   = привязка к узлу
  - На ребро            = привязка к ребру
  - На пустое место     = отмена (бокс возвращается)

Ctrl + клик на привязанном боксе = отвязка.
Ctrl + двойной клик = редактирование текста.
ЛКМ без Ctrl = pan (ScrollHandDrag).
"""

import logging
import math
from typing import Optional

from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsRectItem,
    QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsPixmapItem,
    QGraphicsPathItem, QGraphicsItemGroup, QInputDialog,
)
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QBrush, QPen,
    QFont, QPainterPath, QFontMetricsF, QPolygonF, QCursor,
)
from PySide6.QtCore import Qt, QRectF, QPointF, Signal

logger = logging.getLogger(__name__)

# ─── Цвета ───────────────────────────────────────────────────────
COLOR_OCR_HIGH = QColor(0, 210, 0, 90)
COLOR_OCR_MED = QColor(255, 220, 0, 90)
COLOR_OCR_LOW = QColor(255, 140, 0, 90)
COLOR_OCR_BORDER_HIGH = QColor(0, 210, 0, 200)
COLOR_OCR_BORDER_MED = QColor(255, 220, 0, 200)
COLOR_OCR_BORDER_LOW = QColor(255, 140, 0, 200)

COLOR_OCR_BOUND_BORDER = QColor(30, 120, 255, 240)
OCR_BOUND_BORDER_WIDTH = 2

# П3: единая схема — золото (привязан) / голубой (нет), без заливки
COLOR_BOUND_GOLD = QColor(255, 200, 0, 255)
COLOR_UNBOUND_BLUE = QColor(80, 160, 255, 240)
BOUND_BORDER_WIDTH = 2.5
COLOR_EQUIP_GREY = QColor(235, 235, 235, 245)     # яркий светло-серый — контур/bbox оборудования
COLOR_CENTROID = QColor(52, 152, 219, 230)        # синяя закрашенная точка-центроид
COLOR_CONNECTOR = QColor(150, 150, 150, 200)      # серая точка коннектора (только вид)
COLOR_SELECT_GREEN = QColor(46, 204, 113, 255)    # выделение рамкой (Shift)
SELECT_BORDER_WIDTH = 2.5

COLOR_DROP_HIGHLIGHT = QColor(255, 230, 0, 160)

COLOR_NODE_EQUIPMENT = QColor("#3498db")
COLOR_NODE_HOVER = QColor("#f1c40f")
COLOR_NODE_BOUND = QColor("#e67e22")

COLOR_EDGE = QColor(0, 255, 220, 180)
COLOR_EDGE_HOVER = QColor("#f1c40f")
COLOR_EDGE_BOUND = QColor("#e67e22")

COLOR_BINDING_LINE = QColor(30, 120, 255, 160)
COLOR_TEXT_LABEL = QColor(255, 255, 255, 230)
COLOR_TEXT_BG = QColor(0, 0, 0, 140)

# Отступ привязанного блока от границы цели (авто-позиция при привязке), px.
# Синхронизировано с _BIND_GAP в ui/editors/ocr_layer_mixin.py.
OCR_BIND_GAP = 6.0
# Порог «вертикальный» блок: h > w * 1.3 (квадрат — горизонтальный).
# Продублировано из modules/graph_to_fxml.py:_TEXT_VERTICAL_RATIO —
# менять синхронно, иначе редактор разойдётся с итоговым FXML.
_TEXT_VERTICAL_RATIO = 1.3


def _block_is_vertical(x1: float, y1: float, x2: float, y2: float) -> bool:
    """Вертикальный текст-блок (текст пишется снизу вверх) — по аспекту bbox."""
    return (y2 - y1) > (x2 - x1) * _TEXT_VERTICAL_RATIO

# Diameter binding colors
COLOR_DIAMETER_BORDER = QColor(155, 89, 182, 240)       # #9B59B6
COLOR_DIAMETER_FILL = QColor(155, 89, 182, 50)
COLOR_DIAMETER_LINE = QColor(155, 89, 182, 160)
COLOR_DIAMETER_LABEL_BG = QColor(155, 89, 182, 200)
COLOR_DIAMETER_LABEL_TEXT = QColor(255, 255, 255, 240)
DIAMETER_BORDER_WIDTH = 1.5

# Conflict edge colors
COLOR_CONFLICT_EDGE = QColor(220, 40, 40, 220)
COLOR_CONFLICT_LABEL_BG = QColor(220, 40, 40, 200)
COLOR_CONFLICT_LABEL_TEXT = QColor(255, 255, 255, 240)

CONF_HIGH = 0.94
CONF_MED = 0.80

# Validation colors
COLOR_VALID_GREEN = QColor(46, 204, 113, 90)
COLOR_VALID_GREEN_BORDER = QColor(46, 204, 113, 220)
COLOR_VALID_YELLOW = QColor(241, 196, 15, 90)
COLOR_VALID_YELLOW_BORDER = QColor(241, 196, 15, 220)
COLOR_VALID_ORANGE = QColor(255, 165, 0, 60)
COLOR_VALID_ORANGE_BORDER = QColor(255, 165, 0, 180)
COLOR_CONFIRMED_BORDER = QColor(255, 215, 0, 255)      # яркое золото, full opaque
COLOR_CONFIRMED_FILL = QColor(46, 204, 113, 90)        # зелёная заливка (как green validation)
CONFIRMED_BORDER_WIDTH = 3.0

# KKS binding colors
COLOR_KKS_LINE = QColor(44, 62, 80, 160)
COLOR_KKS_LABEL_BG = QColor(44, 62, 80, 200)
COLOR_KKS_LABEL_TEXT = QColor(255, 255, 255, 240)

# KKS mode: заливка bbox оборудования
COLOR_KKS_NODE_UNBOUND = QColor(200, 50, 50, 80)          # красный — нет KKS
COLOR_KKS_NODE_UNBOUND_BORDER = QColor(200, 50, 50, 200)
COLOR_KKS_NODE_BOUND = QColor(50, 180, 50, 80)            # зелёный — есть KKS
COLOR_KKS_NODE_BOUND_BORDER = QColor(50, 180, 50, 200)


def _clean_text(text: str) -> str:
    """Единая нормализация (П5): делегирует modules.ocr.text_clean.cleanup
    (NFKC, html.unescape, снятие тегов/LaTeX, control-символы, единый вид)."""
    from modules.ocr.text_clean import cleanup
    return cleanup(text or "")


def _format_kks_display(kks_full: str, block: str = "", system: str = "",
                        fn: str = "", unit: str = "", num: str = "",
                        suffix: str = "") -> str:
    """Форматировать KKS для отображения: 10LAH04AA103 → 10 LAH 04 AA 103."""
    if block and system and fn and unit and num:
        parts = [block, system, fn, unit, num]
        if suffix:
            parts.append(suffix)
        return " ".join(parts)
    # Fallback: вставить пробелы по паттерну цифры↔буквы
    import re
    s = re.sub(r'(\d)([A-Za-z])', r'\1 \2', kks_full)
    s = re.sub(r'([A-Za-z])(\d)', r'\1 \2', s)
    return s


def _seg_intersects_rect(ax, ay, bx, by, rx1, ry1, rx2, ry2) -> bool:
    """Отрезок (ax,ay)-(bx,by) пересекает прямоугольник? (Cohen-Sutherland)."""
    def outcode(x, y):
        code = 0
        if x < rx1: code |= 1
        elif x > rx2: code |= 2
        if y < ry1: code |= 4
        elif y > ry2: code |= 8
        return code

    ca, cb = outcode(ax, ay), outcode(bx, by)
    for _ in range(10):
        if ca == 0 or cb == 0:
            return True  # хотя бы один конец внутри
        if ca & cb:
            return False  # оба по одну сторону
        co = ca if ca else cb
        dx, dy = bx - ax, by - ay
        if co & 8:
            x = ax + dx * (ry2 - ay) / dy if abs(dy) > 1e-12 else ax
            y = ry2
        elif co & 4:
            x = ax + dx * (ry1 - ay) / dy if abs(dy) > 1e-12 else ax
            y = ry1
        elif co & 2:
            y = ay + dy * (rx2 - ax) / dx if abs(dx) > 1e-12 else ay
            x = rx2
        else:
            y = ay + dy * (rx1 - ax) / dx if abs(dx) > 1e-12 else ay
            x = rx1
        if co == ca:
            ax, ay, ca = x, y, outcode(x, y)
        else:
            bx, by, cb = x, y, outcode(x, y)
    return False


def _ocr_colors(conf: float) -> tuple:
    if conf >= CONF_HIGH:
        return COLOR_OCR_HIGH, COLOR_OCR_BORDER_HIGH
    elif conf >= CONF_MED:
        return COLOR_OCR_MED, COLOR_OCR_BORDER_MED
    return COLOR_OCR_LOW, COLOR_OCR_BORDER_LOW


class OcrBindingEditor(QGraphicsView):

    binding_changed = Signal()
    blocks_changed = Signal()   # Emitted when _ocr_blocks list is modified (append/merge/delete)
    status_message = Signal(str)
    mode_changed = Signal(str)  # "idle", "add", "del", "move"
    validation_exit_requested = Signal()  # Esc в режиме валидации

    # Два радиуса узла разведены намеренно (зеркало П0 в base_graph_editor).
    # NODE_RADIUS — ГЕОМЕТРИЧЕСКИЙ: цель привязки у узла без bbox
    #   (_node_target_bbox) → _bind_side_of/_auto_bind_bbox пишут block["bbox"],
    #   а он уходит в артефакт ocr_binding и дальше в <Text> FXML.
    # NODE_DRAW_RADIUS — НАРИСОВАННЫЙ: только кружок центроида/коннектора,
    #   его крутит ползунок «размер узлов». Дефолты равны.
    NODE_RADIUS = 7
    NODE_DRAW_RADIUS = 7
    OCR_BORDER_WIDTH = 2
    BINDING_LINE_WIDTH = 2
    CLICK_THRESHOLD = 25
    EDGE_HIT_THRESHOLD = 25
    TEXT_FONT_SIZE = 9
    # Порог различия клик/drag для Ctrl+ЛКМ по блоку (px) —
    # синхронно с OcrBindHandler.OCR_CLICK_MOVE_THRESHOLD.
    OCR_CLICK_MOVE_THRESHOLD = 4.0
    # Порядок вращения стороны привязки по часовой стрелке (Ctrl+ЛКМ по узлу).
    _BIND_SIDE_CW = {"right": "bottom", "bottom": "left", "left": "top",
                     "top": "right"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene()
        self.setScene(self.scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)

        # Data
        self._ocr_blocks: list[dict] = []
        self._secondary_blocks: list[dict] = []  # secondary OCR annotations (grey, read-only)
        self._graph_nodes: list[dict] = []
        self._graph_edges: list[dict] = []
        self._bindings: list[dict] = []
        self._coco_data: dict = {}

        # Image
        self._img_width = self._img_height = 0
        self._data_loaded = False

        # Graphics items
        self._ocr_items: dict[int, QGraphicsRectItem] = {}
        self._ocr_text_items: dict[int, QGraphicsSimpleTextItem] = {}
        self._ocr_text_bg_items: dict[int, QGraphicsRectItem] = {}
        self._ocr_inner_text_items: dict[int, QGraphicsSimpleTextItem] = {}  # текст внутри фиктивных боксов
        self._node_items: dict[str, QGraphicsEllipseItem] = {}
        self._edge_items: dict[int, QGraphicsPathItem] = {}  # edge_idx → item
        self._edge_item_to_idx: dict[int, int] = {}  # id(QGraphicsPathItem) → edge index
        self._binding_lines: list[QGraphicsLineItem] = []
        self._flag_items: list = []  # [(rect, text)] flag labels at targets
        self._secondary_items: list = []  # visual items for secondary OCR blocks

        # Bound indices
        self._bound_ocr_indices: set[int] = set()
        self._bound_node_ids: set[str] = set()
        self._bound_edge_keys: set[str] = set()

        # Drag state
        self.ctrl_pressed = False
        self._drag_idx: Optional[int] = None       # OCR block being dragged
        self._drag_origin_bbox: list = []           # original bbox before drag
        self._drag_offset: tuple = (0, 0)           # cursor offset from bbox center
        self._drop_target_type: Optional[str] = None  # "ocr", "node", "edge", None
        self._drop_target_id = None                 # idx/str depending on type
        self._highlighted_node: Optional[str] = None
        self._highlighted_edge_idx: Optional[int] = None
        self._highlighted_ocr: Optional[int] = None

        # Add/Delete/Move mode (from toolbar buttons)
        self._add_mode: bool = False
        self._del_mode: bool = False
        self._move_mode: bool = False
        self._add_bbox_start = None      # П3: рисование бокса перетаскиванием
        self._add_bbox_preview = None
        self._selected_ocr: set = set()  # П3: выделенные боксы (Shift-рамка)
        self._ocr_clipboard: list = []   # буфер копипаста: [(bbox, text), ...]
        # Призрак вставки (Ctrl+V → следует за мышью → Ctrl+ЛКМ фиксирует):
        # {"group": QGraphicsItemGroup, "center": (cx, cy)}
        self._paste_ghost: dict | None = None
        self._ctrl_press_xy: tuple | None = None  # позиция press для клик/drag
        self._rb_start = None            # старт rubber-band
        self._rubber_band = None         # item рамки выделения
        self._node_contours: dict = {}   # ann_idx -> полигон контура узла
        # Настраиваемое оформление (панель ⚙)
        self._c_edge = COLOR_EDGE
        self._c_equip = COLOR_EQUIP_GREY
        self._c_textbox = COLOR_UNBOUND_BLUE
        self._label_pt = self.TEXT_FONT_SIZE
        self._bg_darkness = 0.47
        self._bg_item = None
        self._orig_qimage = None
        self._move_idx: Optional[int] = None  # block being moved
        self._move_origin_bbox: list = []
        self._drag_line = None  # жёлтый пунктир при drag

        # Undo stack
        self._undo_stack: list = []  # [(ocr_blocks_json, bindings_json)]
        self._MAX_UNDO = 30

        # Diameter bindings (edge-based, no ocr_block_idx)
        self._diameter_bindings: list[dict] = []
        self._propagated_diameters: list[dict] = []
        self._conflict_edges: list[dict] = []
        self._diameter_bound_edge_keys: set[str] = set()
        self._diameter_bound_ocr_indices: set[int] = set()
        self._conflict_edge_keys: set[str] = set()
        self._diameter_items: list = []  # visual items (lines, labels, rects)
        self._diameter_label_rects: list[tuple] = []  # [(x1,y1,x2,y2, edge_key, edge_idx, is_propagated)]
        self._diameter_matcher = None  # DiameterMatcher, set from tab
        self._text_binder = None       # TextBinder, set from tab

        # Validation state
        self._validation_results: list = []  # BlockClassification list
        self._validation_mode: bool = False

        # KKS bindings
        self._kks_bindings: list[dict] = []
        self._kks_bound_ocr_indices: set[int] = set()
        self._kks_bound_node_ids: set[str] = set()
        self._kks_items: list = []  # visual items (lines, labels)
        self._kks_label_rects: list[tuple] = []  # [(x1,y1,x2,y2, node_id, ocr_idx)]

        # B6.5: config dir for KKS normalization
        self._project_config_dir: str | None = None

        # Block filter: set of visible block_idx; None = show all
        self._block_filter: set[int] | None = None

        # Bind mode: restricts what targets are valid for drag-drop
        # "kks" = only nodes, "diameter" = only edges, None = all targets
        self._bind_mode: str | None = None

    # =================================================================
    # Public API
    # =================================================================

    def load_data(self, image_path: str, ocr_blocks: list[dict],
                  graph_data: dict, bindings: list[dict], coco_data: dict = None,
                  secondary_blocks: list[dict] = None, node_contours: dict = None):
        self._ocr_blocks = ocr_blocks
        self._secondary_blocks = secondary_blocks or []
        self._graph_nodes = graph_data.get("nodes", [])
        self._graph_edges = graph_data.get("links", [])
        self._bindings = bindings
        self._coco_data = coco_data or {}
        self._node_contours = node_contours or {}
        self._rebuild_bound_indices()

        img = QImage(image_path)
        if img.isNull():
            return
        self._img_width, self._img_height = img.width(), img.height()

        self.scene.clear()
        self._paste_ghost = None  # призрак вставки не переживает scene.clear()
        self._ocr_items.clear()
        self._ocr_text_items.clear()
        self._ocr_text_bg_items.clear()
        self._ocr_inner_text_items.clear()
        self._node_items.clear()
        self._connector_items = {}
        self._edge_items.clear()
        self._edge_item_to_idx.clear()
        self._binding_lines.clear()
        self._secondary_items.clear()

        # Затемнить изображение — рисуем поверх без копии
        if img.format() != QImage.Format.Format_ARGB32:
            img = img.convertToFormat(QImage.Format.Format_ARGB32)
        self._orig_qimage = img.copy()
        dark = img.copy()
        p = QPainter(dark)
        p.fillRect(dark.rect(), QColor(0, 0, 0, int(self._bg_darkness * 255)))
        p.end()
        self._bg_item = self.scene.addPixmap(QPixmap.fromImage(dark))
        self._bg_item.setZValue(0)
        self.scene.setSceneRect(QRectF(0, 0, self._img_width, self._img_height))

        self._draw_equipment_bboxes()
        self._draw_edges()
        self._draw_graph_nodes()
        self._draw_secondary_blocks()
        self._draw_ocr_blocks()
        self._draw_bindings()
        self._draw_diameter_bindings()

        self._data_loaded = True
        self.resetTransform()
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event):
        super().showEvent(event)
        if self._data_loaded:
            self.resetTransform()
            self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def get_bindings(self) -> list[dict]:
        return self._bindings

    def set_bindings(self, bindings: list[dict]):
        self._bindings = bindings
        self._rebuild_bound_indices()
        self._redraw_all_colors()
        self._redraw_bindings()
        self.binding_changed.emit()

    def clear_bindings(self):
        self.set_bindings([])

    def set_diameter_bindings(self, bindings: list[dict], propagated: list[dict] = None,
                                conflicts: list[dict] = None):
        """Установить привязки диаметров (от TextBinder) + распространённые + конфликты."""
        self._diameter_bindings = bindings
        self._propagated_diameters = propagated or []
        self._conflict_edges = conflicts or []
        self._rebuild_diameter_bound_indices()
        self._redraw_diameter_bindings()
        self._redraw_all_colors()

    def get_diameter_bindings(self) -> list[dict]:
        """Получить текущие привязки диаметров."""
        return self._diameter_bindings

    # =================================================================
    # Block filter API (sub-tab visibility)
    # =================================================================

    def set_block_filter(self, visible_indices: set[int] | None):
        """Установить фильтр видимости OCR-блоков.

        Args:
            visible_indices: множество block_idx которые должны быть видимы.
                None = показать все блоки (без фильтра).
        """
        self._block_filter = visible_indices
        self._apply_block_filter()

    def _is_block_visible(self, idx: int) -> bool:
        """Проверить, проходит ли блок текущий фильтр."""
        if self._block_filter is None:
            return True
        return idx in self._block_filter

    def _apply_block_filter(self):
        """Применить фильтр видимости ко всем OCR-блокам и перерисовать ТОЛЬКО текущий режим."""
        for idx in self._ocr_items:
            block = self._ocr_blocks[idx] if idx < len(self._ocr_blocks) else {}
            if block.get("merged_into") is not None:
                continue
            visible = self._is_block_visible(idx)
            # KKS-bound блоки скрываются в _redraw_kks_bindings
            if idx in self._kks_bound_ocr_indices:
                pass
            else:
                if idx in self._ocr_items:
                    self._ocr_items[idx].setVisible(visible)
                if idx in self._ocr_text_items:
                    self._ocr_text_items[idx].setVisible(visible)
                if idx in self._ocr_text_bg_items:
                    self._ocr_text_bg_items[idx].setVisible(visible)

        # Перерисовать ВСЕ типы привязок: каждый redraw сначала чистит старые items,
        # затем рисует только то, что нужно для текущего _bind_mode
        self._redraw_all_colors()
        self._redraw_bindings()
        self._redraw_diameter_bindings()
        self._redraw_kks_bindings()

    # =================================================================
    # Validation API
    # =================================================================

    def set_validation_results(self, classifications: list):
        """Установить результаты валидации, перекрасить блоки.

        В новой архитектуре validation_mode всегда True когда есть classifications,
        т.к. цвета = соответствие паттерну (не confidence).
        """
        self._validation_results = classifications
        self._validation_mode = True
        self._redraw_validation_colors()

    def exit_validation_mode(self):
        """Выйти из режима валидации (deprecated — в новой архитектуре validation всегда активна)."""
        # Не сбрасываем _validation_mode и _validation_results,
        # т.к. цвета = pattern match всегда.
        self._redraw_all_colors()
        self._redraw_bindings()
        self._redraw_diameter_bindings()
        self._redraw_kks_bindings()

    def refresh_validation_colors(self):
        """Обновить цвета после confirm/edit."""
        if self._validation_results:
            self._redraw_validation_colors()

    def _redraw_validation_colors(self):
        """Перекрасить OCR-боксы по результатам валидации + обновить подписи."""
        from modules.ocr_validation.result import ConfirmStatus
        cl_by_idx = {cl.block_idx: cl for cl in self._validation_results}
        font = QFont("DejaVu Sans", self._label_pt)
        fm = QFontMetricsF(font)

        for idx, rect in self._ocr_items.items():
            # Не трогать привязанные (KKS/diameter)
            if idx in self._kks_bound_ocr_indices:
                continue

            # Block filter
            if not self._is_block_visible(idx):
                continue

            cl = cl_by_idx.get(idx)
            if cl is None:
                continue

            if cl.confirm_status == ConfirmStatus.CONFIRMED:
                rect.setPen(QPen(COLOR_CONFIRMED_BORDER, CONFIRMED_BORDER_WIDTH))
                rect.setBrush(QBrush(COLOR_CONFIRMED_FILL))
            elif cl.confirm_status == ConfirmStatus.DELETED:
                rect.setVisible(False)
            else:
                fill, border = self._validation_color_pair(cl.color.value)
                rect.setPen(QPen(border, self.OCR_BORDER_WIDTH))
                rect.setBrush(QBrush(fill))

            # Обновить подпись — показать распознанный текст вместо OCR-оригинала
            display_text = None
            if cl.kks_full:
                kks_display = _format_kks_display(
                    cl.kks_full, cl.kks_block or "", cl.kks_system or "",
                    cl.kks_fn or "", cl.kks_unit or "", cl.kks_num or "",
                    cl.kks_suffix or "",
                )
                if cl.diameter_text:
                    display_text = f"{kks_display} {cl.diameter_text}"
                else:
                    display_text = kks_display
            elif cl.diameter_text:
                display_text = cl.diameter_text

            if display_text and idx in self._ocr_text_items:
                label = self._ocr_text_items[idx]
                label.setText(display_text)
                # Обновить фон подписи
                if idx in self._ocr_text_bg_items:
                    bg = self._ocr_text_bg_items[idx]
                    tw = fm.horizontalAdvance(display_text)
                    r = bg.rect()
                    bg.setRect(r.x(), r.y(), tw + 4, r.height())

    @staticmethod
    def _validation_color_pair(color_name: str):
        """Вернуть (fill, border) для validation color."""
        return {
            "green": (COLOR_VALID_GREEN, COLOR_VALID_GREEN_BORDER),
            "yellow": (COLOR_VALID_YELLOW, COLOR_VALID_YELLOW_BORDER),
            "orange": (COLOR_VALID_ORANGE, COLOR_VALID_ORANGE_BORDER),
        }.get(color_name, (QColor(200, 200, 200, 70), QColor(220, 220, 220, 220)))

    def _toggle_confirm(self, ocr_idx: int):
        """Клик по блоку в режиме валидации → подтвердить / отменить."""
        self._push_undo()
        from modules.ocr_validation.result import ConfirmStatus
        for cl in self._validation_results:
            if cl.block_idx == ocr_idx:
                if cl.confirm_status == ConfirmStatus.UNCONFIRMED:
                    cl.confirm_status = ConfirmStatus.CONFIRMED
                    self.status_message.emit(f"Подтверждён: {cl.original_text[:30]}")
                elif cl.confirm_status == ConfirmStatus.CONFIRMED:
                    cl.confirm_status = ConfirmStatus.UNCONFIRMED
                    self.status_message.emit("Отменено подтверждение")
                self._redraw_validation_colors()
                self._after_change()
                break

    def _reclassify_block(self, ocr_idx: int):
        """Переклассифицировать блок после редактирования текста."""
        try:
            from modules.ocr_validation.classifier import OcrBlockClassifier
            # Нужны оба matcher — берём из tab через сохранённые ссылки
            if not hasattr(self, '_ocr_classifier') or self._ocr_classifier is None:
                return
            block = self._ocr_blocks[ocr_idx]
            new_cl = self._ocr_classifier.reclassify_one(ocr_idx, block)
            # Заменить в _validation_results
            for i, cl in enumerate(self._validation_results):
                if cl.block_idx == ocr_idx:
                    new_cl.confirm_status = cl.confirm_status  # сохранить статус
                    self._validation_results[i] = new_cl
                    break
            else:
                self._validation_results.append(new_cl)
            self._redraw_validation_colors()
        except Exception:
            pass  # не ломать UX если reclassify не удалось

    # =================================================================
    # KKS Bindings API
    # =================================================================

    def set_kks_bindings(self, bindings: list[dict]):
        """Установить KKS-привязки."""
        self._kks_bindings = bindings
        self._kks_bound_ocr_indices = {b["ocr_block_idx"] for b in bindings}
        self._kks_bound_node_ids = {b["node_id"] for b in bindings}
        self._redraw_all_colors()
        self._redraw_kks_bindings()

    def get_kks_bindings(self) -> list[dict]:
        """Получить текущие KKS-привязки."""
        return list(self._kks_bindings)

    def _unbind_kks(self, ocr_idx: int):
        """Отвязать KKS от узла (по ocr_idx)."""
        self._push_undo()
        removed = [b for b in self._kks_bindings if b["ocr_block_idx"] == ocr_idx]
        self._kks_bindings = [b for b in self._kks_bindings if b["ocr_block_idx"] != ocr_idx]
        self._kks_bound_ocr_indices = {b["ocr_block_idx"] for b in self._kks_bindings}
        self._kks_bound_node_ids = {b["node_id"] for b in self._kks_bindings}
        # Восстановить видимость OCR-бокса
        self._restore_ocr_visibility(ocr_idx)
        self._after_change()
        text = removed[0].get("kks_full", "")[:20] if removed else ""
        self.status_message.emit(f"KKS отвязан: {text}")

    def _unbind_kks_by_node(self, node_id: str):
        """Отвязать KKS от узла (по node_id — клик на центроид/флажок)."""
        removed = [b for b in self._kks_bindings if b["node_id"] == node_id]
        if not removed:
            return
        self._push_undo()
        ocr_idx = removed[0]["ocr_block_idx"]
        self._kks_bindings = [b for b in self._kks_bindings if b["node_id"] != node_id]
        self._kks_bound_ocr_indices = {b["ocr_block_idx"] for b in self._kks_bindings}
        self._kks_bound_node_ids = {b["node_id"] for b in self._kks_bindings}
        self._restore_ocr_visibility(ocr_idx)
        self._after_change()
        text = removed[0].get("kks_full", "")[:20]
        self.status_message.emit(f"KKS отвязан: {text}")

    def _restore_ocr_visibility(self, ocr_idx: int):
        """Восстановить видимость OCR-бокса после отвязки."""
        if ocr_idx in self._ocr_items:
            self._ocr_items[ocr_idx].setVisible(True)
        if ocr_idx in self._ocr_text_items:
            self._ocr_text_items[ocr_idx].setVisible(True)
        if ocr_idx in self._ocr_text_bg_items:
            self._ocr_text_bg_items[ocr_idx].setVisible(True)
        if ocr_idx in self._ocr_inner_text_items:
            self._ocr_inner_text_items[ocr_idx].setVisible(True)

    def _redraw_kks_bindings(self):
        """Отрисовать KKS-привязки: скрыть OCR-боксы, линия от узла, флажок KKS."""
        # Удалить старые
        for item in self._kks_items:
            self.scene.removeItem(item)
        self._kks_items.clear()
        self._kks_label_rects.clear()

        if not self._kks_bindings:
            return

        # В режиме диаметра — не рисовать KKS-флажки (только скрыть OCR-боксы)
        if self._bind_mode == "diameter":
            for kb in self._kks_bindings:
                ocr_idx = kb["ocr_block_idx"]
                if ocr_idx in self._ocr_items:
                    self._ocr_items[ocr_idx].setVisible(False)
                if ocr_idx in self._ocr_text_items:
                    self._ocr_text_items[ocr_idx].setVisible(False)
                if ocr_idx in self._ocr_text_bg_items:
                    self._ocr_text_bg_items[ocr_idx].setVisible(False)
                if ocr_idx in self._ocr_inner_text_items:
                    self._ocr_inner_text_items[ocr_idx].setVisible(False)
            return

        pen = QPen(COLOR_CONFIRMED_BORDER, 1.5, Qt.PenStyle.DashLine)
        font = QFont("DejaVu Sans", self._label_pt)
        fm = QFontMetricsF(font)

        # Собрать все занятые прямоугольники (OCR-боксы, включая скрытые KKS)
        occupied_rects = []
        for idx, block in enumerate(self._ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            bbox = block.get("bbox")
            if bbox and len(bbox) == 4:
                occupied_rects.append(bbox)

        for kb in self._kks_bindings:
            ocr_idx = kb["ocr_block_idx"]
            node_id = kb["node_id"]

            if ocr_idx >= len(self._ocr_blocks):
                continue

            ncx, ncy = self._get_node_center(node_id)
            if ncx is None:
                continue

            # Скрыть OCR-бокс (рамка + подпись + внутренний текст)
            if ocr_idx in self._ocr_items:
                self._ocr_items[ocr_idx].setVisible(False)
            if ocr_idx in self._ocr_text_items:
                self._ocr_text_items[ocr_idx].setVisible(False)
            if ocr_idx in self._ocr_text_bg_items:
                self._ocr_text_bg_items[ocr_idx].setVisible(False)
            if ocr_idx in self._ocr_inner_text_items:
                self._ocr_inner_text_items[ocr_idx].setVisible(False)

            # Подготовить текст флажка
            label_text = _format_kks_display(kb.get("kks_full", ""))
            tw = fm.horizontalAdvance(label_text) + 8
            th = fm.height() + 4

            # Позиция флажка = позиция подписи над OCR-боксом
            block = self._ocr_blocks[ocr_idx]
            bbox = block.get("bbox", [0, 0, 0, 0])
            lx = bbox[0]
            ly = bbox[1] - th - 2
            if ly < 0:
                ly = bbox[3] + 2

            # Линия от центроида узла к флажку
            flag_cx = lx + tw / 2
            flag_cy = ly + th / 2
            line = self.scene.addLine(ncx, ncy, flag_cx, flag_cy, pen)
            line.setZValue(15)
            self._kks_items.append(line)

            # Флажок (фон + текст)
            bg = self.scene.addRect(
                lx, ly, tw, th,
                QPen(COLOR_CONFIRMED_BORDER, 1.0), QBrush(COLOR_KKS_LABEL_BG),
            )
            bg.setZValue(21)
            self._kks_items.append(bg)

            label = self.scene.addSimpleText(label_text, font)
            label.setBrush(QBrush(COLOR_KKS_LABEL_TEXT))
            label.setPos(lx + 4, ly + 2)
            label.setZValue(22)
            self._kks_items.append(label)

            # Индикатор mismatch
            if not kb.get("unit_valid", True):
                warn = self.scene.addSimpleText("⚠", font)
                warn.setBrush(QBrush(QColor(255, 80, 80)))
                warn.setPos(lx + tw + 2, ly + 2)
                warn.setZValue(22)
                self._kks_items.append(warn)

            # Запомнить rect для клик-детекции
            self._kks_label_rects.append((lx, ly, lx + tw, ly + th, node_id, ocr_idx))
            # Добавить в occupied чтобы следующие флажки не пересекались
            occupied_rects.append([lx, ly, lx + tw, ly + th])

        # Узлы с KKS — рамка оборудования обновляется в _redraw_all_colors_base

    def _find_non_overlapping_pos(
        self, cx: float, cy: float, w: float, h: float,
        occupied: list, max_attempts: int = 8,
    ) -> tuple[float, float]:
        """Найти позицию для флажка рядом с (cx,cy), не пересекающую occupied rects."""
        # Попробовать 8 направлений вокруг точки
        offsets = [
            (12, -h - 4),    # сверху-справа
            (12, 4),         # снизу-справа
            (-w - 12, -h - 4),  # сверху-слева
            (-w - 12, 4),    # снизу-слева
            (12, -h/2),      # справа
            (-w - 12, -h/2), # слева
            (-w/2, -h - 12), # сверху
            (-w/2, 12),      # снизу
        ]
        for dx, dy in offsets:
            lx, ly = cx + dx, cy + dy
            if not self._rect_overlaps_any(lx, ly, lx + w, ly + h, occupied):
                return lx, ly
        # Fallback: дальше вправо-вверх
        return cx + 20, cy - h - 10

    @staticmethod
    def _rect_overlaps_any(x1, y1, x2, y2, rects) -> bool:
        """Проверить пересечение прямоугольника с любым из списка."""
        for r in rects:
            rx1, ry1, rx2, ry2 = r[0], r[1], r[2], r[3]
            if x1 < rx2 and x2 > rx1 and y1 < ry2 and y2 > ry1:
                return True
        return False

    def _rebuild_diameter_bound_indices(self):
        """Пересчитать множества привязанных edges/OCR для диаметров."""
        self._diameter_bound_edge_keys = set()
        self._diameter_bound_ocr_indices = set()
        self._conflict_edge_keys = set()
        for db in self._diameter_bindings:
            ek = db.get("edge_key")
            if ek:
                self._diameter_bound_edge_keys.add(ek)
            ocr_idx = db.get("ocr_block_idx")
            if ocr_idx is not None:
                self._diameter_bound_ocr_indices.add(ocr_idx)
        for pd in self._propagated_diameters:
            ek = pd.get("edge_key")
            if ek:
                self._diameter_bound_edge_keys.add(ek)
        for cf in self._conflict_edges:
            ek = cf.get("edge_key")
            if ek:
                self._conflict_edge_keys.add(ek)

    def _repropagate_diameters(self):
        """Пересчитать распространение диаметров от текущих привязок."""
        if not self._diameter_bindings:
            self._propagated_diameters = []
            self._conflict_edges = []
            self._rebuild_diameter_bound_indices()
            return

        # Lazy-init _text_binder если ещё не создан
        if not self._text_binder:
            try:
                from modules.text_binding.config import TextRecognitionConfig
                from modules.text_binding.binder import TextBinder
                cfg = TextRecognitionConfig()
                if self._project_config_dir:
                    from pathlib import Path as _P
                    # Найти project yaml
                    cfg_dir = _P(self._project_config_dir)
                    for y in cfg_dir.glob("*.yaml"):
                        if "kks_config" in y.name or "class_to_kks" in y.name:
                            continue
                        try:
                            test_cfg = TextRecognitionConfig.from_project_yaml(str(y))
                            if test_cfg.diameter.patterns:
                                cfg = test_cfg
                                break
                        except Exception:
                            pass
                self._text_binder = TextBinder(cfg)
                if not self._diameter_matcher and cfg.diameter.patterns:
                    from modules.text_binding.matcher import DiameterMatcher
                    self._diameter_matcher = DiameterMatcher(cfg.diameter)
                logger.info("Lazy-created TextBinder for repropagation")
            except Exception as exc:
                logger.warning("Cannot create TextBinder for repropagation: %s", exc)
                self._propagated_diameters = []
                self._conflict_edges = []
                self._rebuild_diameter_bound_indices()
                return

        from modules.text_binding.binder import DiameterBinding
        bindings = []
        for db in self._diameter_bindings:
            bindings.append(DiameterBinding(
                ocr_block_idx=db.get("ocr_block_idx", -1),
                edge_idx=db.get("edge_idx", -1),
                edge_id=db.get("edge_id", ""),
                edge_key=db.get("edge_key", ""),
                text=db.get("text", ""),
                prefix=db.get("prefix", ""),
                diameter=db.get("diameter", 0),
                suffix=db.get("suffix", ""),
                confidence=db.get("confidence", 1.0),
                distance=db.get("distance", 0.0),
            ))

        try:
            prop_report = self._text_binder.propagate_diameters(
                self._graph_nodes, self._graph_edges, bindings
            )
        except Exception as exc:
            logger.warning("Diameter propagation failed: %s", exc)
            self._propagated_diameters = []
            self._conflict_edges = []
            self._rebuild_diameter_bound_indices()
            return

        self._propagated_diameters = []
        for pd in prop_report.propagated:
            # Нормализовать edge_key в sorted формат (min|max)
            ek = pd.edge_key
            parts = ek.split("|", 1)
            if len(parts) == 2:
                ek = f"{min(parts[0], parts[1])}|{max(parts[0], parts[1])}"
            self._propagated_diameters.append({
                "edge_idx": pd.edge_idx,
                "edge_id": pd.edge_id,
                "edge_key": ek,
                "text": pd.text,
                "prefix": pd.prefix,
                "diameter": pd.diameter,
                "suffix": pd.suffix,
                "confidence": pd.confidence,
                "propagated": True,
            })

        self._conflict_edges = []
        for cf in prop_report.conflicts:
            ek = cf.edge_key
            parts = ek.split("|", 1)
            if len(parts) == 2:
                ek = f"{min(parts[0], parts[1])}|{max(parts[0], parts[1])}"
            self._conflict_edges.append({
                "edge_idx": cf.edge_idx,
                "edge_id": cf.edge_id,
                "edge_key": ek,
                "candidates": cf.candidates,
            })

        self._rebuild_diameter_bound_indices()

    # =================================================================
    # Drawing
    # =================================================================

    @staticmethod
    def _poly_points(poly):
        """Точки контура из плоского [x0,y0,x1,y1,...] или вложенного [[x,y],...]."""
        if not poly:
            return []
        if isinstance(poly[0], (list, tuple)):
            return [(float(pt[0]), float(pt[1])) for pt in poly if len(pt) >= 2]
        return [(float(poly[i]), float(poly[i + 1]))
                for i in range(0, len(poly) - 1, 2)]

    def _draw_equipment_bboxes(self):
        color = self._c_equip
        self._equipment_bbox_items: dict[str, QGraphicsRectItem] = {}

        # Проверить: есть ли bbox в graph nodes (авторитетный источник после редактора)
        graph_has_bboxes = any(
            n.get("bbox") and len(n["bbox"]) == 4
            for n in self._graph_nodes if n.get("type") != "connector"
        )

        # COCO-аннотации рисуем только если в графе нет bbox
        # (после редактора граф содержит актуальные позиции, COCO — устаревшие)
        if not graph_has_bboxes:
            annotations = self._coco_data.get("annotations", [])
            for ann in annotations:
                bbox = ann.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x, y, w, h = bbox
                item = self.scene.addRect(x, y, w, h, QPen(color, 2))
                item.setZValue(3)

        # Маппинг node_id → rect (для KKS-заливки)
        for node in self._graph_nodes:
            if node.get("type") == "connector":
                continue
            nid = node.get("id", "")
            pts = self._poly_points(self._node_contours.get(node.get("ann_idx")))
            if len(pts) >= 3:
                qpoly = QPolygonF([QPointF(px, py) for px, py in pts])
                item = self.scene.addPolygon(qpoly, QPen(color, 2),
                                             QBrush(Qt.BrushStyle.NoBrush))
                item.setZValue(3)
                self._equipment_bbox_items[nid] = item
                continue
            nb = node.get("bbox")
            if not nb or len(nb) != 4:
                continue
            x1, y1, x2, y2 = nb
            item = self.scene.addRect(x1, y1, x2 - x1, y2 - y1, QPen(color, 2),
                                      QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(3)
            self._equipment_bbox_items[nid] = item

    def _draw_edges(self):
        for idx, edge in enumerate(self._graph_edges):
            src_id, tgt_id = edge.get("source"), edge.get("target")
            if not src_id or not tgt_id:
                continue
            sp, tp = edge.get("source_point"), edge.get("target_point")
            if not sp or not tp:
                sx, sy = self._get_node_center(src_id)
                tx, ty = self._get_node_center(tgt_id)
                if sx is None or tx is None:
                    continue
            else:
                sx, sy, tx, ty = sp[1], sp[0], tp[1], tp[0]
            path = QPainterPath()
            path.moveTo(sx, sy)
            for wp in edge.get("waypoints", []):
                path.lineTo(wp[1], wp[0])
            path.lineTo(tx, ty)
            ek = f"{min(src_id, tgt_id)}|{max(src_id, tgt_id)}"
            color = COLOR_BOUND_GOLD if ek in self._bound_edge_keys else self._c_edge
            item = self.scene.addPath(path, QPen(color, 5))
            item.setZValue(5)
            self._edge_items[idx] = item
            self._edge_item_to_idx[id(item)] = idx

    def _draw_secondary_blocks(self):
        """Draw secondary OCR blocks (grey, read-only annotations)."""
        if not self._secondary_blocks:
            return
        COLOR_SEC_FILL = QColor(180, 180, 180, 40)
        COLOR_SEC_BORDER = QColor(180, 180, 180, 120)
        COLOR_SEC_TEXT = QColor(200, 200, 200, 180)
        font = QFont("DejaVu Sans", self.TEXT_FONT_SIZE - 1)
        fm = QFontMetricsF(font)
        for block in self._secondary_blocks:
            if block.get("_removed"):
                continue
            bbox = block.get("bbox")
            text = _clean_text(block.get("text", ""))
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            rect = self.scene.addRect(
                x1, y1, x2 - x1, y2 - y1,
                QPen(COLOR_SEC_BORDER, 1.0, Qt.PenStyle.DotLine),
                QBrush(COLOR_SEC_FILL),
            )
            rect.setZValue(8)
            self._secondary_items.append(rect)
            if text:
                th = fm.height()
                lx, ly = x1, y1 - th - 2
                if ly < 0:
                    ly = y2 + 2
                label = self.scene.addSimpleText(text, font)
                label.setBrush(QBrush(COLOR_SEC_TEXT))
                label.setPos(lx, ly)
                label.setZValue(9)
                self._secondary_items.append(label)

    def set_edge_color(self, c):
        self._c_edge = QColor(c); self._redraw_all_colors()

    def set_box_border_color(self, c):
        """Цвет рамки/контура оборудования."""
        self._c_equip = QColor(c); self._redraw_all_colors()

    def set_text_border_color(self, c):
        """Цвет рамки непривязанного текст-бокса."""
        self._c_textbox = QColor(c); self._redraw_all_colors()

    def set_label_font_size(self, pt):
        self._label_pt = max(6, int(pt)); self.refresh_ocr_layer()

    def set_background_darkness(self, frac):
        self._bg_darkness = max(0.0, min(0.95, float(frac)))
        self._apply_bg_darkness()

    def _apply_bg_darkness(self):
        if self._orig_qimage is None or self._bg_item is None:
            return
        dark = self._orig_qimage.copy()
        p = QPainter(dark)
        p.fillRect(dark.rect(), QColor(0, 0, 0, int(self._bg_darkness * 255)))
        p.end()
        self._bg_item.setPixmap(QPixmap.fromImage(dark))

    def refresh_ocr_layer(self):
        """П3: полностью перерисовать слой OCR (после распознавания/массовых правок).
        Сначала убираем старые item'ы из сцены — иначе появляются дубли."""
        for items_dict in (self._ocr_items, self._ocr_text_items,
                           self._ocr_text_bg_items, self._ocr_inner_text_items):
            for it in items_dict.values():
                try:
                    self.scene.removeItem(it)
                except Exception:
                    pass
            items_dict.clear()
        self._rebuild_bound_indices()
        self._draw_ocr_blocks()
        self._redraw_all_colors()
        self._redraw_bindings()

    def _ocr_pen(self, idx):
        """Рамка текст-бокса: зелёный (выделен) / золото (привязан) / голубой (нет)."""
        if idx in self._selected_ocr:
            return QPen(COLOR_SELECT_GREEN, SELECT_BORDER_WIDTH)
        if idx in self._bound_ocr_indices:
            return QPen(COLOR_BOUND_GOLD, BOUND_BORDER_WIDTH)
        return QPen(self._c_textbox, BOUND_BORDER_WIDTH)

    def _draw_ocr_blocks(self):
        font = QFont("DejaVu Sans", self._label_pt)
        fm = QFontMetricsF(font)
        for idx, block in enumerate(self._ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            bbox = block.get("bbox")
            text = _clean_text(block.get("text", ""))
            conf = block.get("confidence", 0)
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            rect = self.scene.addRect(x1, y1, x2 - x1, y2 - y1, self._ocr_pen(idx),
                                      QBrush(Qt.BrushStyle.NoBrush))
            rect.setZValue(10)
            self._ocr_items[idx] = rect
            if text:
                th = fm.height()
                tw = fm.horizontalAdvance(text)
                lx, ly, rot, bg_rect = self._label_geometry(
                    x1, y1, x2, y2, tw, th)
                bg = self.scene.addRect(*bg_rect,
                                        QPen(Qt.PenStyle.NoPen), QBrush(COLOR_TEXT_BG))
                bg.setZValue(11)
                self._ocr_text_bg_items[idx] = bg
                label = self.scene.addSimpleText(text, font)
                label.setBrush(QBrush(COLOR_TEXT_LABEL))
                label.setPos(lx, ly)
                label.setRotation(rot)
                label.setZValue(12)
                self._ocr_text_items[idx] = label

    @staticmethod
    def _label_geometry(x1, y1, x2, y2, tw, th):
        """Геометрия подписи блока: (lx, ly, rotation, bg_rect).

        Горизонтальный блок — подпись над боксом (fallback вниз, если не
        влезает сверху). Вертикальный (h > w*1.3) — подпись у нижнего-левого
        угла с поворотом -90° (имитация Rotate angle="-90" сценбилдера):
        текст идёт снизу вверх вдоль левой стенки (fallback вдоль правой).
        Точка поворота — pos подписи (transform origin (0,0)).
        """
        if _block_is_vertical(x1, y1, x2, y2):
            lx, ly = x1 - th - 2, y2
            if lx < 0:
                lx = x2 + 2
            bl = min(tw + 4, (y2 - y1) + 4)
            return lx, ly, -90.0, (lx - 1, ly - bl + 1, th + 2, bl)
        lx, ly = x1, y1 - th - 2
        if ly < 0:
            ly = y2 + 2
        return lx, ly, 0.0, (lx - 1, ly - 1, min(tw + 4, (x2 - x1) + 4), th + 2)

    def _draw_graph_nodes(self):
        if not hasattr(self, "_connector_items"):
            self._connector_items = {}
        r = self.NODE_DRAW_RADIUS
        for node in self._graph_nodes:
            node_id = node.get("id", "")
            cx, cy = self._node_center(node)
            if cx is None:
                continue
            if node.get("type") == "connector":
                rr = max(2.0, r * 0.45)
                e = self.scene.addEllipse(cx - rr, cy - rr, rr * 2, rr * 2,
                                          QPen(COLOR_CONNECTOR.darker(120), 1.0),
                                          QBrush(COLOR_CONNECTOR))
                e.setZValue(18)
                self._connector_items[node_id] = e
                continue
            c = COLOR_BOUND_GOLD if node_id in self._bound_node_ids else COLOR_CENTROID
            e = self.scene.addEllipse(cx - r, cy - r, r * 2, r * 2,
                                      QPen(c.darker(130), 1.5), QBrush(c))
            e.setZValue(20)
            self._node_items[node_id] = e

    def _draw_bindings(self):
        """П3: привязка — тонкая золотая линия бокс→цель. Бокс остаётся видимым
        (золотая рамка ставится в _redraw_all_colors_base), цель золотится."""
        self._node_binding_label_rects = []
        if not self._bindings:
            return
        pen = QPen(COLOR_BOUND_GOLD, self.BINDING_LINE_WIDTH, Qt.PenStyle.DashLine)
        for b in self._bindings:
            ocr_idx = b.get("ocr_block_idx")
            if ocr_idx is None or ocr_idx >= len(self._ocr_blocks):
                continue
            block = self._ocr_blocks[ocr_idx]
            if block.get("merged_into") is not None:
                continue
            bbox = block.get("bbox", [0, 0, 0, 0])
            bcx, bcy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            node_id = b.get("node_id")
            edge_key = b.get("edge_key")
            if node_id:
                tcx, tcy = self._get_node_center(node_id)
            elif edge_key:
                tcx, tcy = self._get_edge_midpoint_by_key(edge_key)
            else:
                continue
            if tcx is None:
                continue
            line = self.scene.addLine(bcx, bcy, tcx, tcy, pen)
            line.setZValue(15)
            self._binding_lines.append(line)

    def _redraw_bindings(self):
        for l in self._binding_lines:
            self.scene.removeItem(l)
        self._binding_lines.clear()
        for item in self._flag_items:
            self.scene.removeItem(item)
        self._flag_items.clear()
        self._draw_bindings()

    def _redraw_all_colors(self):
        # П3: только схема золото/голубой; цветовая валидация отключена
        self._redraw_all_colors_base()

    def _redraw_all_colors_base(self):
        # П3: текст-боксы — только рамка (золото если привязан, голубой если нет), без заливки
        for idx, rect in self._ocr_items.items():
            block = self._ocr_blocks[idx] if idx < len(self._ocr_blocks) else {}
            deleted = block.get("merged_into") is not None
            visible = (not deleted) and self._is_block_visible(idx)
            rect.setVisible(visible)
            for items in (self._ocr_text_items, self._ocr_text_bg_items,
                          self._ocr_inner_text_items):
                if idx in items:
                    items[idx].setVisible(visible)
            if not visible:
                continue
            rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            rect.setPen(self._ocr_pen(idx))

        # Центроид — ЗАКРАШЕННАЯ точка (золото если привязан, иначе синяя)
        for nid, e in self._node_items.items():
            c = COLOR_BOUND_GOLD if nid in self._bound_node_ids else COLOR_CENTROID
            e.setBrush(QBrush(c))
            e.setPen(QPen(c.darker(130), 1.5))

        # Контур/bbox оборудования — только рамка (серый / золото)
        if hasattr(self, '_equipment_bbox_items'):
            for nid, rect_item in self._equipment_bbox_items.items():
                c = COLOR_BOUND_GOLD if nid in self._bound_node_ids else self._c_equip
                rect_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                rect_item.setPen(QPen(c, 2.0))

        # Рёбра — золото если привязан, иначе стандартный цвет
        for i, item in self._edge_items.items():
            if i >= len(self._graph_edges):
                continue
            ed = self._graph_edges[i]
            s_, t_ = ed.get('source', ''), ed.get('target', '')
            ek = f"{min(s_, t_)}|{max(s_, t_)}"
            item.setPen(QPen(COLOR_BOUND_GOLD if ek in self._bound_edge_keys else self._c_edge, 5))

    def _rebuild_bound_indices(self):
        self._bound_ocr_indices = set()
        self._bound_node_ids = set()
        self._bound_edge_keys = set()
        for b in self._bindings:
            idx = b.get("ocr_block_idx")
            if idx is not None:
                self._bound_ocr_indices.add(idx)
            if b.get("node_id"):
                self._bound_node_ids.add(b["node_id"])
            if b.get("edge_key"):
                self._bound_edge_keys.add(b["edge_key"])

    # =================================================================
    # Diameter bindings visualization
    # =================================================================

    def _draw_diameter_bindings(self):
        """Нарисовать привязки диаметров: скрыть OCR-бокс, линия от ребра, флажок с текстом."""
        # В режиме KKS — не рисовать метки диаметров
        if self._bind_mode == "kks":
            return

        pen_line = QPen(COLOR_DIAMETER_LINE, 1.5, Qt.PenStyle.DashLine)
        font = QFont("DejaVu Sans", self.TEXT_FONT_SIZE + 1)
        fm = QFontMetricsF(font)

        # Собрать occupied rects (все OCR bbox)
        occupied_rects = []
        for idx, block in enumerate(self._ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            bbox = block.get("bbox")
            if bbox and len(bbox) == 4:
                occupied_rects.append(bbox)

        # Собрать edge_keys с прямыми привязками
        direct_edge_keys = set()

        for db in self._diameter_bindings:
            edge_key = db.get("edge_key")
            ocr_idx = db.get("ocr_block_idx")
            text = db.get("text", "")
            diam_value = db.get("diameter", "")
            edge_idx = db.get("edge_idx")

            ecx, ecy = None, None
            if edge_key:
                ecx, ecy = self._get_edge_midpoint_by_key(edge_key)
                direct_edge_keys.add(edge_key)
            if ecx is None:
                continue

            # Скрыть OCR-бокс
            if ocr_idx is not None and ocr_idx < len(self._ocr_blocks):
                if ocr_idx in self._ocr_items:
                    self._ocr_items[ocr_idx].setVisible(False)
                if ocr_idx in self._ocr_text_items:
                    self._ocr_text_items[ocr_idx].setVisible(False)
                if ocr_idx in self._ocr_text_bg_items:
                    self._ocr_text_bg_items[ocr_idx].setVisible(False)
                if ocr_idx in self._ocr_inner_text_items:
                    self._ocr_inner_text_items[ocr_idx].setVisible(False)

            # Текст флажка
            label_text = str(diam_value) if diam_value else text
            if not label_text:
                continue
            tw = fm.horizontalAdvance(label_text) + 8
            th = fm.height() + 4

            # Позиция флажка: над OCR-боксом (как KKS)
            if ocr_idx is not None and ocr_idx < len(self._ocr_blocks):
                block = self._ocr_blocks[ocr_idx]
                bbox = block.get("bbox", [0, 0, 0, 0])
                lx = bbox[0]
                ly = bbox[1] - th - 2
                if ly < 0:
                    ly = bbox[3] + 2
            else:
                lx = ecx - tw / 2
                ly = ecy - th - 8

            # Линия от середины ребра к флажку
            flag_cx = lx + tw / 2
            flag_cy = ly + th / 2
            line = self.scene.addLine(ecx, ecy, flag_cx, flag_cy, pen_line)
            line.setZValue(15)
            self._diameter_items.append(line)

            # Флажок (фон + текст)
            bg = self.scene.addRect(
                lx, ly, tw, th,
                QPen(COLOR_DIAMETER_BORDER, 1.0), QBrush(COLOR_DIAMETER_LABEL_BG),
            )
            bg.setZValue(16)
            self._diameter_items.append(bg)

            label = self.scene.addSimpleText(label_text, font)
            label.setBrush(QBrush(COLOR_DIAMETER_LABEL_TEXT))
            label.setPos(lx + 4, ly + 2)
            label.setZValue(17)
            self._diameter_items.append(label)

            # Запомнить rect для клик-детекции
            self._diameter_label_rects.append(
                (lx, ly, lx + tw, ly + th, edge_key, edge_idx, False)
            )
            occupied_rects.append([lx, ly, lx + tw, ly + th])

        # Propagated diameters — метка на ребре (полупрозрачная, без скрытия бокса)
        prop_bg = QColor(155, 89, 182, 140)
        prop_border = QColor(155, 89, 182, 180)
        font_prop = QFont("DejaVu Sans", self._label_pt)
        fm_prop = QFontMetricsF(font_prop)

        for pd in self._propagated_diameters:
            edge_key = pd.get("edge_key")
            if not edge_key or edge_key in direct_edge_keys:
                continue

            ecx, ecy = self._get_edge_midpoint_by_key(edge_key)
            if ecx is None:
                continue

            diam_value = pd.get("diameter", "")
            label_text = str(diam_value) if diam_value else pd.get("text", "")
            edge_idx = pd.get("edge_idx")
            self._draw_edge_diameter_label(fm_prop, font_prop, label_text, ecx, ecy,
                                           prop_bg, prop_border, edge_idx, edge_key, True)

        # Conflict edges — красная метка
        font_conf = QFont("DejaVu Sans", self.TEXT_FONT_SIZE + 1)
        font_conf.setBold(True)
        fm_conf = QFontMetricsF(font_conf)

        for cf in self._conflict_edges:
            edge_key = cf.get("edge_key")
            edge_idx = cf.get("edge_idx")
            candidates = cf.get("candidates", [])
            if not edge_key or not candidates:
                continue

            ecx, ecy = self._get_edge_midpoint_by_key(edge_key)
            if ecx is None:
                continue

            label_text = "? " + " | ".join(str(c) for c in candidates)
            self._draw_edge_diameter_label(fm_conf, font_conf, label_text, ecx, ecy,
                                           COLOR_CONFLICT_LABEL_BG, COLOR_CONFLICT_EDGE,
                                           edge_idx, edge_key, False)

    def _draw_edge_diameter_label(self, fm, font, text, ecx, ecy, bg_color, border_color,
                                    edge_idx=None, edge_key=None, is_propagated=False):
        """Нарисовать метку диаметра рядом с серединой ребра, не перекрывая ребро и боксы."""
        if not text:
            return
        tw = fm.horizontalAdvance(text) + 6
        th = fm.height() + 2
        OFFSET = 4  # отступ от центра ребра (вплотную)

        # Определить ориентацию ребра → смещаем перпендикулярно
        orient = None
        if edge_idx is not None and edge_idx < len(self._graph_edges):
            from modules.text_binding.geometry import edge_orientation
            orient = edge_orientation(self._graph_edges[edge_idx])

        if orient == "V":
            # Вертикальное ребро → метка слева
            lx = ecx - tw - OFFSET
            ly = ecy - th / 2
        else:
            # Горизонтальное или диагональное → метка сверху
            lx = ecx - tw / 2
            ly = ecy - th - OFFSET

        # Проверить не перекрывает ли OCR-бокс, если да — сместить на другую сторону
        label_rect = [lx, ly, lx + tw, ly + th]
        for block in self._ocr_blocks:
            if block.get("merged_into") is not None:
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            # Перекрытие?
            if not (label_rect[2] < bbox[0] or label_rect[0] > bbox[2] or
                    label_rect[3] < bbox[1] or label_rect[1] > bbox[3]):
                # Перекрывается → отзеркалить
                if orient == "V":
                    lx = ecx + OFFSET  # справа
                else:
                    ly = ecy + OFFSET  # снизу
                break

        # #64: propagated → dashed border
        if is_propagated:
            pen = QPen(border_color, 1.0, Qt.PenStyle.DashLine)
        else:
            pen = QPen(border_color, 0.5)
        bg_rect = self.scene.addRect(
            lx, ly, tw, th,
            pen,
            QBrush(bg_color),
        )
        bg_rect.setZValue(16)
        self._diameter_items.append(bg_rect)

        label = self.scene.addSimpleText(text, font)
        label.setBrush(QBrush(COLOR_DIAMETER_LABEL_TEXT))
        label.setPos(lx + 3, ly + 1)
        label.setZValue(17)
        self._diameter_items.append(label)

        # Запомнить позицию метки для клика
        self._diameter_label_rects.append(
            (lx, ly, lx + tw, ly + th, edge_key, edge_idx, is_propagated)
        )

    def _redraw_diameter_bindings(self):
        """Перерисовать визуализацию диаметров."""
        for item in self._diameter_items:
            self.scene.removeItem(item)
        self._diameter_items.clear()
        self._diameter_label_rects.clear()
        self._draw_diameter_bindings()

    # =================================================================
    # Hit testing
    # =================================================================

    def _find_ocr_at(self, x, y, exclude=None) -> Optional[int]:
        best, best_area = None, float("inf")
        for idx, block in enumerate(self._ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            if exclude is not None and idx == exclude:
                continue
            # Block filter
            if not self._is_block_visible(idx):
                continue
            # П3: привязанные боксы видимы и кликабельны — не пропускаем
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            if x1 <= x <= x2 and y1 <= y <= y2:
                a = (x2 - x1) * (y2 - y1)
                if a < best_area:
                    best_area, best = a, idx
        return best

    def _find_secondary_at(self, x, y) -> Optional[int]:
        """Найти secondary-блок под координатами (x, y)."""
        best, best_area = None, float("inf")
        for i, block in enumerate(self._secondary_blocks):
            if block.get("_removed"):
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            if x1 <= x <= x2 and y1 <= y <= y2:
                a = (x2 - x1) * (y2 - y1)
                if a < best_area:
                    best_area, best = a, i
        return best

    def _promote_secondary_block(self, sec_idx: int) -> int:
        """Промотировать secondary-блок в основной массив.

        1. Копирует данные блока в _ocr_blocks
        2. Перерисовывает secondary-блоки (без удалённого)
        3. Отрисовывает как обычный OCR-блок
        4. Эмитит blocks_changed

        Returns: новый индекс в _ocr_blocks
        """
        block = self._secondary_blocks[sec_idx]
        new_idx = len(self._ocr_blocks)

        # Добавить в основной массив
        self._ocr_blocks.append({
            "bbox": list(block.get("bbox", [0, 0, 0, 0])),
            "text": block.get("text", ""),
            "confidence": block.get("confidence", 0),
            "source": block.get("source", "secondary"),
            "origin": "promoted_secondary",
        })

        # Пометить как удалённый в secondary
        self._secondary_blocks[sec_idx] = {"_removed": True}

        # Перерисовать secondary блоки
        self._redraw_secondary_blocks()

        # Отрисовать как обычный OCR-блок
        self._draw_single_ocr_block(new_idx)

        # Уведомить tab
        self.blocks_changed.emit()
        text_preview = block.get("text", "")[:40]
        self.status_message.emit(f"📝 Блок промотирован: «{text_preview}»")

        return new_idx

    def _draw_single_ocr_block(self, idx: int):
        """Отрисовать один OCR-блок по индексу."""
        block = self._ocr_blocks[idx]
        bbox = block.get("bbox")
        text = _clean_text(block.get("text", ""))
        conf = block.get("confidence", 0)
        if not bbox or len(bbox) != 4:
            return
        x1, y1, x2, y2 = bbox
        fill, border = _ocr_colors(conf)
        is_bound = idx in self._bound_ocr_indices
        pen = QPen(COLOR_OCR_BOUND_BORDER, OCR_BOUND_BORDER_WIDTH) if is_bound \
            else QPen(border, self.OCR_BORDER_WIDTH)
        rect = self.scene.addRect(x1, y1, x2 - x1, y2 - y1, pen, QBrush(fill))
        rect.setZValue(10)
        self._ocr_items[idx] = rect

        if text:
            font = QFont("DejaVu Sans", self._label_pt)
            fm = QFontMetricsF(font)
            th = fm.height()
            lx, ly = x1, y1 - th - 2
            if ly < 0:
                ly = y2 + 2
            tw = fm.horizontalAdvance(text)
            bg = self.scene.addRect(
                lx - 1, ly - 1, min(tw + 4, x2 - x1 + 4), th + 2,
                QPen(Qt.PenStyle.NoPen), QBrush(COLOR_TEXT_BG),
            )
            bg.setZValue(11)
            self._ocr_text_bg_items[idx] = bg
            label = self.scene.addSimpleText(text, font)
            label.setBrush(QBrush(COLOR_TEXT_LABEL))
            label.setPos(lx, ly)
            label.setZValue(12)
            self._ocr_text_items[idx] = label

    def _redraw_secondary_blocks(self):
        """Перерисовать secondary блоки (после promote)."""
        for item in self._secondary_items:
            self.scene.removeItem(item)
        self._secondary_items.clear()
        self._draw_secondary_blocks()

    def _find_node_at(self, x, y) -> Optional[str]:
        best_id, best_dist = None, self.CLICK_THRESHOLD
        for node in self._graph_nodes:
            if node.get("type") == "connector":
                continue
            ncx, ncy = self._node_center(node)
            if ncx is None:
                continue
            d = math.hypot(x - ncx, y - ncy)
            if d < best_dist:
                best_dist, best_id = d, node.get("id")
        return best_id

    def _find_edge_at(self, x, y) -> Optional[int]:
        """Найти ребро под курсором.

        Приоритет у рёбер где курсор проецируется НА сам отрезок
        (t ∈ [0,1]), а не на его продолжение. Это решает проблему
        когда курсор над длинной трубой, но математически ближе
        к короткой перемычке которая заканчивается рядом.
        """
        best_idx = None
        best_dist = self.EDGE_HIT_THRESHOLD
        best_on_segment = False

        for idx, edge in enumerate(self._graph_edges):
            d, on_seg = self._point_to_edge_dist_ex(x, y, edge)
            if d is None or d >= self.EDGE_HIT_THRESHOLD:
                continue
            # on_segment всегда побеждает off_segment
            if on_seg and not best_on_segment:
                best_dist, best_idx, best_on_segment = d, idx, True
            elif on_seg == best_on_segment and d < best_dist:
                best_dist, best_idx = d, idx
        return best_idx

    def _find_edge_by_bbox(self, bx1, by1, bx2, by2) -> Optional[int]:
        """Найти ближайшее ребро к bbox OCR-бокса.

        Двухуровневый приоритет:
        1) Ребро пересекает bbox (проходит ВНУТРИ бокса) → ближайшее к центру
        2) Нет пересечений → минимальное расстояние от границ bbox до ребра
        Порог для фазы 2: MAX_GAP пикселей.
        """
        bcx, bcy = (bx1 + bx2) / 2, (by1 + by2) / 2
        MAX_GAP = 40.0

        # Точки на границе bbox для расчёта расстояния (углы + середины сторон)
        border_pts = [
            (bx1, by1), (bx2, by1), (bx2, by2), (bx1, by2),
            (bcx, by1), (bx2, bcy), (bcx, by2), (bx1, bcy),
        ]

        best_inside_idx = None
        best_inside_dist = float("inf")
        best_outside_idx = None
        best_outside_dist = float("inf")

        for idx, edge in enumerate(self._graph_edges):
            if self._edge_seg_hits_rect(edge, bx1, by1, bx2, by2):
                # Фаза 1: пересекает bbox → сортируем по расстоянию до центра
                d = self._point_to_edge_dist(bcx, bcy, edge)
                if d is not None and d < best_inside_dist:
                    best_inside_dist = d
                    best_inside_idx = idx
            else:
                # Фаза 2: не пересекает → min расстояние от границы bbox до ребра
                min_d = float("inf")
                for px, py in border_pts:
                    d = self._point_to_edge_dist(px, py, edge)
                    if d is not None and d < min_d:
                        min_d = d
                if min_d < best_outside_dist and min_d < MAX_GAP:
                    best_outside_dist = min_d
                    best_outside_idx = idx

        return best_inside_idx if best_inside_idx is not None else best_outside_idx

    def _edge_seg_hits_rect(self, edge, rx1, ry1, rx2, ry2) -> bool:
        """Любой сегмент ребра пересекает прямоугольник?"""
        sp, tp = edge.get("source_point"), edge.get("target_point")
        if not sp or not tp:
            sx, sy = self._get_node_center(edge.get("source", ""))
            tx, ty = self._get_node_center(edge.get("target", ""))
            if sx is None or tx is None:
                return False
        else:
            sx, sy, tx, ty = sp[1], sp[0], tp[1], tp[0]
        pts = [(sx, sy)] + [(w[1], w[0]) for w in edge.get("waypoints", [])] + [(tx, ty)]
        for i in range(len(pts) - 1):
            if _seg_intersects_rect(*pts[i], *pts[i + 1], rx1, ry1, rx2, ry2):
                return True
        return False

    def _point_to_edge_dist(self, px, py, edge) -> Optional[float]:
        d, _ = self._point_to_edge_dist_ex(px, py, edge)
        return d

    def _point_to_edge_dist_ex(self, px, py, edge) -> tuple[Optional[float], bool]:
        """Расстояние от точки до ребра + on_segment флаг."""
        sp, tp = edge.get("source_point"), edge.get("target_point")
        if not sp or not tp:
            sx, sy = self._get_node_center(edge.get("source", ""))
            tx, ty = self._get_node_center(edge.get("target", ""))
            if sx is None or tx is None:
                return None, False
        else:
            sx, sy, tx, ty = sp[1], sp[0], tp[1], tp[0]
        pts = [(sx, sy)] + [(w[1], w[0]) for w in edge.get("waypoints", [])] + [(tx, ty)]
        min_dist = float("inf")
        any_on = False
        for i in range(len(pts) - 1):
            d, on = self._pt_seg_ex(px, py, *pts[i], *pts[i + 1])
            if d < min_dist:
                min_dist, any_on = d, on
            elif d == min_dist and on:
                any_on = True
        return (min_dist if min_dist < float("inf") else None), any_on

    @staticmethod
    def _pt_seg(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 < 1e-9:
            return math.hypot(px - ax, py - ay)
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / l2))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    @staticmethod
    def _pt_seg_ex(px, py, ax, ay, bx, by) -> tuple[float, bool]:
        """Расстояние + on_segment: True если проекция на отрезок (t ∈ [0,1])."""
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 < 1e-9:
            return math.hypot(px - ax, py - ay), True
        t = ((px - ax) * dx + (py - ay) * dy) / l2
        on_segment = 0.0 <= t <= 1.0
        tc = max(0.0, min(1.0, t))
        return math.hypot(px - (ax + tc * dx), py - (ay + tc * dy)), on_segment

    # =================================================================
    # Helpers
    # =================================================================

    def _find_node(self, nid):
        for n in self._graph_nodes:
            if n.get("id") == nid:
                return n
        return None

    def _node_center(self, node):
        c = node.get("centroid")
        if c and len(c) >= 2:
            return c[1], c[0]
        b = node.get("bbox")
        if b and len(b) == 4:
            return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return None, None

    def _get_node_center(self, nid):
        n = self._find_node(nid)
        return self._node_center(n) if n else (None, None)

    def _get_edge_midpoint_by_key(self, ek):
        parts = ek.split("|")
        if len(parts) != 2:
            return None, None
        s, t = parts
        for e in self._graph_edges:
            if (e.get("source") == s and e.get("target") == t) or \
               (e.get("source") == t and e.get("target") == s):
                sp, tp = e.get("source_point"), e.get("target_point")
                if not sp or not tp:
                    sx, sy = self._get_node_center(e.get("source", ""))
                    tx, ty = self._get_node_center(e.get("target", ""))
                else:
                    sx, sy, tx, ty = sp[1], sp[0], tp[1], tp[0]
                if sx is None:
                    return None, None
                wps = e.get("waypoints", [])
                if wps:
                    m = wps[len(wps) // 2]
                    return m[1], m[0]
                return (sx + tx) / 2, (sy + ty) / 2
        return None, None

    def _edge_key_str(self, idx):
        if idx >= len(self._graph_edges):
            return None
        e = self._graph_edges[idx]
        s, t = e.get('source', ''), e.get('target', '')
        return f"{min(s, t)}|{max(s, t)}"

    def _move_ocr_visuals(self, idx, x1, y1, x2, y2, text=None):
        """Переместить rect + text + bg для OCR-бокса."""
        if idx in self._ocr_items:
            self._ocr_items[idx].setRect(x1, y1, x2 - x1, y2 - y1)
        if text is None:
            block = self._ocr_blocks[idx] if idx < len(self._ocr_blocks) else {}
            text = block.get("text", "").strip()

        # Подпись: горизонтальный блок — сверху; вертикальный — снизу вверх
        # вдоль левой стенки (поворот -90°, см. _label_geometry)
        font = QFont("DejaVu Sans", self._label_pt)
        fm = QFontMetricsF(font)
        th = fm.height()

        if text:
            tw = fm.horizontalAdvance(text)
            lx, ly, rot, bg_rect = self._label_geometry(x1, y1, x2, y2, tw, th)
            if idx in self._ocr_text_items:
                item = self._ocr_text_items[idx]
                item.setText(text)
                item.setPos(lx, ly)
                item.setRotation(rot)   # сброс/установка поворота при переиспользовании
                item.setVisible(True)
            else:
                label = self.scene.addSimpleText(text, font)
                label.setBrush(QBrush(COLOR_TEXT_LABEL))
                label.setPos(lx, ly)
                label.setRotation(rot)
                label.setZValue(12)
                self._ocr_text_items[idx] = label

            if idx in self._ocr_text_bg_items:
                self._ocr_text_bg_items[idx].setRect(*bg_rect)
                self._ocr_text_bg_items[idx].setVisible(True)
            else:
                bg = self.scene.addRect(*bg_rect,
                                        QPen(Qt.PenStyle.NoPen), QBrush(COLOR_TEXT_BG))
                bg.setZValue(11)
                self._ocr_text_bg_items[idx] = bg
        else:
            # Текст пустой — скрыть подписи
            if idx in self._ocr_text_items:
                self._ocr_text_items[idx].setVisible(False)
            if idx in self._ocr_text_bg_items:
                self._ocr_text_bg_items[idx].setVisible(False)

    # =================================================================
    # Drop highlight
    # =================================================================

    def _clear_highlights(self):
        if self._highlighted_node and self._highlighted_node in self._node_items:
            e = self._node_items[self._highlighted_node]
            is_b = self._highlighted_node in self._bound_node_ids
            c = COLOR_NODE_BOUND if is_b else COLOR_NODE_EQUIPMENT
            e.setBrush(QBrush(c))
            e.setPen(QPen(c.darker(130), 1.5))
            # вернуть перо контура оборудования (рамка/полигон), если подсвечивали
            rect = getattr(self, "_equipment_bbox_items", {}).get(self._highlighted_node)
            pen = getattr(self, "_highlighted_bbox_pen", None)
            if rect is not None and pen is not None:
                rect.setPen(pen)
            self._highlighted_bbox_pen = None
        self._highlighted_node = None

        if self._highlighted_edge_idx is not None and self._highlighted_edge_idx in self._edge_items:
            ed = self._graph_edges[self._highlighted_edge_idx]
            s, t = ed.get('source', ''), ed.get('target', ''); ek = f"{min(s, t)}|{max(s, t)}"
            c = COLOR_EDGE_BOUND if ek in self._bound_edge_keys else COLOR_EDGE
            self._edge_items[self._highlighted_edge_idx].setPen(QPen(c, 5))
        self._highlighted_edge_idx = None

        if self._highlighted_ocr is not None and self._highlighted_ocr in self._ocr_items:
            block = self._ocr_blocks[self._highlighted_ocr]
            conf = block.get("confidence", 0)
            fill, border = _ocr_colors(conf)
            rect = self._ocr_items[self._highlighted_ocr]
            rect.setBrush(QBrush(fill))
            is_b = self._highlighted_ocr in self._bound_ocr_indices
            rect.setPen(QPen(COLOR_OCR_BOUND_BORDER, OCR_BOUND_BORDER_WIDTH) if is_b
                        else QPen(border, self.OCR_BORDER_WIDTH))
        self._highlighted_ocr = None

    def _highlight_node(self, nid):
        self._clear_highlights()
        if nid and nid in self._node_items:
            self._highlighted_node = nid
            self._node_items[nid].setBrush(QBrush(COLOR_DROP_HIGHLIGHT))
            self._node_items[nid].setPen(QPen(COLOR_DROP_HIGHLIGHT.darker(130), 2.5))
            # подсветить весь контур оборудования (рамка бокса / полигон) —
            # ровно то, к чему привяжется; перо запоминаем для восстановления
            rect = getattr(self, "_equipment_bbox_items", {}).get(nid)
            if rect is not None:
                self._highlighted_bbox_pen = rect.pen()
                rect.setPen(QPen(COLOR_DROP_HIGHLIGHT, 3))

    def _highlight_edge(self, idx):
        self._clear_highlights()
        if idx is not None and idx in self._edge_items:
            self._highlighted_edge_idx = idx
            self._edge_items[idx].setPen(QPen(COLOR_DROP_HIGHLIGHT, 7))

    def _highlight_ocr(self, idx):
        self._clear_highlights()
        if idx is not None and idx in self._ocr_items:
            self._highlighted_ocr = idx
            self._ocr_items[idx].setBrush(QBrush(COLOR_DROP_HIGHLIGHT))
            self._ocr_items[idx].setPen(QPen(COLOR_DROP_HIGHLIGHT, 3))

    # =================================================================
    # Operations
    # =================================================================

    def _node_target_bbox(self, node):
        """bbox узла для авто-позиции блока: реальный или бокс вокруг центроида."""
        b = node.get("bbox")
        if b and len(b) == 4:
            return [float(v) for v in b]
        cx, cy = self._node_center(node)
        if cx is None:
            return None
        r = self.NODE_RADIUS
        return [cx - r, cy - r, cx + r, cy + r]

    @staticmethod
    def _bind_side_of(block_bbox, target_bbox) -> str:
        """Сторона цели по «выходам» центра блока за грани bbox цели.

        Одна ось → та сторона; обе (угловая зона) → большее смещение;
        внутри → ближайшая изнутри; равенство (в т.ч. вырожденная цель) →
        right. Правило синхронно с ocr_layer_mixin._nearest_bind_side.
        """
        px = (block_bbox[0] + block_bbox[2]) / 2.0
        py = (block_bbox[1] + block_bbox[3]) / 2.0
        tx1, ty1, tx2, ty2 = target_bbox
        dx = (px - tx2) if px > tx2 else (px - tx1) if px < tx1 else 0.0
        dy = (py - ty2) if py > ty2 else (py - ty1) if py < ty1 else 0.0
        if dx or dy:
            if abs(dx) >= abs(dy):
                return "right" if dx > 0 else "left"
            return "bottom" if dy > 0 else "top"
        side, best = "right", tx2 - px
        for s, d in (("left", px - tx1), ("top", py - ty1),
                     ("bottom", ty2 - py)):
            if d < best:
                side, best = s, d
        return side

    def _auto_bind_bbox(self, ocr_idx, target_bbox, side=None):
        """Авто-позиция блока по центру выбранной стороны цели с отступом
        OCR_BIND_GAP.

        Объекты графа в «бусине» статичны — позиция считается один раз в
        момент привязки (следование не нужно). Сторона — от ИСХОДНОГО
        положения блока (во время drag bbox блока не мутирует — двигаются
        только визуалы), место броска не влияет: привязка детерминирована.
        Параметр side форсирует сторону (вращение привязок), иначе она
        считается правилом «выходов» (_bind_side_of). Размер блока
        сохраняется; bbox блока и визуалы обновляются. Возвращает новый bbox
        или None (цель без геометрии — блок остаётся на месте).
        """
        if target_bbox is None:
            return None
        block = self._ocr_blocks[ocr_idx]
        bb = block.get("bbox")
        if not bb or len(bb) != 4:
            return None
        w = max(1.0, bb[2] - bb[0])
        h = max(1.0, bb[3] - bb[1])
        tx1, ty1, tx2, ty2 = target_bbox
        tcx, tcy = (tx1 + tx2) / 2.0, (ty1 + ty2) / 2.0
        if side is None:
            side = self._bind_side_of(bb, target_bbox)
        if side == "top":
            nx1, ny1 = tcx - w / 2.0, ty1 - OCR_BIND_GAP - h
        elif side == "bottom":
            nx1, ny1 = tcx - w / 2.0, ty2 + OCR_BIND_GAP
        elif side == "left":
            nx1, ny1 = tx1 - OCR_BIND_GAP - w, tcy - h / 2.0
        else:  # right
            nx1, ny1 = tx2 + OCR_BIND_GAP, tcy - h / 2.0
        nb = [nx1, ny1, nx1 + w, ny1 + h]
        block["bbox"] = nb
        self._move_ocr_visuals(ocr_idx, *nb)
        return nb

    def _rotate_node_bindings(self, node_id) -> bool:
        """Повернуть ВСЕ привязки узла на следующую сторону по часовой.

        right → bottom → left → top → right. Текущая сторона каждого блока —
        правилом «выходов» от центра его bbox к bbox узла (_bind_side_of);
        блок переставляется по центру следующей стороны с отступом
        OCR_BIND_GAP. Один _push_undo на весь узел. Returns True, если было
        что вращать.
        """
        binds = [b for b in self._bindings if b.get("node_id") == node_id]
        if not binds:
            return False
        node = self._find_node(node_id)
        tb = self._node_target_bbox(node) if node else None
        if tb is None:
            return False
        self._push_undo()
        n = 0
        for b in binds:
            idx = b.get("ocr_block_idx")
            if idx is None or idx >= len(self._ocr_blocks):
                continue
            block = self._ocr_blocks[idx]
            bb = block.get("bbox")
            if not bb or len(bb) != 4:
                continue
            cur = self._bind_side_of(bb, tb)
            nb = self._auto_bind_bbox(idx, tb, side=self._BIND_SIDE_CW[cur])
            if nb is not None:
                b["bbox"] = nb
                n += 1
        self._after_change()
        self.status_message.emit(f"Привязки узла повернуты по часовой ({n})")
        return True

    def _bind_to_node(self, ocr_idx, node_id):
        # П3: простая привязка текст->узел (без KKS-нормализации/потока).
        # Блок автоматически встаёт по центру стороны bbox/центроида узла
        # (сторона — от исходного положения блока).
        self._push_undo()
        block = self._ocr_blocks[ocr_idx]
        text = _clean_text(block.get("text", ""))
        n = self._find_node(node_id)
        if self._auto_bind_bbox(
                ocr_idx, self._node_target_bbox(n) if n else None) is None:
            # цель без геометрии — вернуть визуалы на bbox блока (как раньше)
            bb = block.get("bbox")
            if bb and len(bb) == 4:
                self._move_ocr_visuals(ocr_idx, *bb)
        self._bindings = [b for b in self._bindings if b.get("ocr_block_idx") != ocr_idx]
        self._bindings.append({
            "node_id": node_id, "text": text,
            "ocr_block_idx": ocr_idx, "bbox": block.get("bbox", []),
        })
        self._after_change()
        cls = n.get("class_name", node_id[:12]) if n else node_id[:12]
        self.status_message.emit(f"Привязано → узел {cls}")

    def _bind_to_edge(self, ocr_idx, edge_idx):
        # П3: простая привязка текст->ребро (как у узла, без диаметра/потока).
        # Блок автоматически встаёт у midpoint'а ребра (сторона — от
        # исходного положения блока).
        self._push_undo()
        block = self._ocr_blocks[ocr_idx]
        text = _clean_text(block.get("text", ""))
        ek = self._edge_key_str(edge_idx)
        mx, my = self._get_edge_midpoint_by_key(ek) if ek else (None, None)
        target_bbox = [mx, my, mx, my] if mx is not None else None
        if self._auto_bind_bbox(ocr_idx, target_bbox) is None:
            bb = block.get("bbox")
            if bb and len(bb) == 4:
                self._move_ocr_visuals(ocr_idx, *bb)
        self._bindings = [b for b in self._bindings if b.get("ocr_block_idx") != ocr_idx]
        self._bindings.append({
            "edge_key": ek, "text": text,
            "ocr_block_idx": ocr_idx, "bbox": block.get("bbox", []),
        })
        self._after_change()
        self.status_message.emit("Привязано → ребро")

    def _unbind(self, ocr_idx):
        self._push_undo()
        removed = [b for b in self._bindings if b.get("ocr_block_idx") == ocr_idx]
        if not removed:
            return
        self._bindings = [b for b in self._bindings if b.get("ocr_block_idx") != ocr_idx]
        self._after_change()
        self.status_message.emit(f"Отвязано: «{removed[0].get('text', '')[:30]}»")

    def _unbind_diameter_by_edge(self, edge_key: str):
        """Отвязать диаметр от ребра по edge_key и пересчитать поток."""
        self._push_undo()
        removed = [db for db in self._diameter_bindings if db.get("edge_key") == edge_key]
        if not removed:
            return
        self._diameter_bindings = [
            db for db in self._diameter_bindings if db.get("edge_key") != edge_key
        ]
        # Восстановить видимость OCR-бокса
        for db in removed:
            ocr_idx = db.get("ocr_block_idx")
            if ocr_idx is not None:
                self._restore_ocr_visibility(ocr_idx)
        self._repropagate_diameters()
        self._after_change()
        text = removed[0].get("text", "")[:30]
        self.status_message.emit(f"Диаметр отвязан: «{text}» (поток пересчитан)")

    def _merge_blocks(self, src_idx, tgt_idx):
        self._push_undo()
        src, tgt = self._ocr_blocks[src_idx], self._ocr_blocks[tgt_idx]
        if src.get("merged_into") is not None or tgt.get("merged_into") is not None:
            return
        bboxes = [src.get("bbox", [0, 0, 0, 0]), tgt.get("bbox", [0, 0, 0, 0])]
        texts = []
        for i in [tgt_idx, src_idx]:
            b = self._ocr_blocks[i]
            t = b.get("text", "").strip()
            if t:
                bb = b.get("bbox", [0, 0, 0, 0])
                texts.append(((bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2, t))
        # #60: bucket sort — texts on the same line (y within tolerance) sort by x
        if texts:
            avg_h = sum(abs(bboxes[i][3] - bboxes[i][1]) for i in range(len(bboxes))) / len(bboxes)
            tol = max(avg_h * 0.4, 5)
            texts.sort(key=lambda t: (round(t[0] / tol) * tol, t[1]))
        merged_text = " ".join(t[2] for t in texts)
        x1 = min(b[0] for b in bboxes)
        y1 = min(b[1] for b in bboxes)
        x2 = max(b[2] for b in bboxes)
        y2 = max(b[3] for b in bboxes)
        confs = [self._ocr_blocks[i].get("confidence", 0) for i in [src_idx, tgt_idx]
                 if self._ocr_blocks[i].get("text", "").strip()]
        tgt["bbox"] = [x1, y1, x2, y2]
        tgt["text"] = merged_text
        tgt["confidence"] = round(min(confs) if confs else 0, 4)
        tgt["source"] = "merged"
        src["merged_into"] = tgt_idx
        # Update regular bindings: src → tgt
        for b in self._bindings:
            if b.get("ocr_block_idx") == src_idx:
                b["ocr_block_idx"] = tgt_idx
                b["text"] = merged_text
                b["bbox"] = [x1, y1, x2, y2]
        # Update kks bindings: src → tgt
        for kb in self._kks_bindings:
            if kb.get("ocr_block_idx") == src_idx:
                kb["ocr_block_idx"] = tgt_idx
        self._kks_bound_ocr_indices = {b["ocr_block_idx"] for b in self._kks_bindings}
        # Hide source visuals
        for items in (self._ocr_items, self._ocr_text_items, self._ocr_text_bg_items, self._ocr_inner_text_items):
            if src_idx in items:
                items[src_idx].setVisible(False)
        # Update target visuals
        self._move_ocr_visuals(tgt_idx, x1, y1, x2, y2, merged_text)
        self._after_change()
        self.blocks_changed.emit()
        self.status_message.emit(f"Объединено: «{merged_text[:40]}»")

        # В режиме валидации — переклассифицировать объединённый блок
        if self._validation_mode and self._validation_results:
            self._reclassify_block(tgt_idx)
            # Удалить classification для src (он merged)
            self._validation_results = [
                cl for cl in self._validation_results
                if cl.block_idx != src_idx
            ]

    def _edit_text(self, ocr_idx):
        self._push_undo()
        block = self._ocr_blocks[ocr_idx]
        current_text = block.get("text", "")

        # В режиме валидации — предложить распознанный вариант
        suggestion = current_text
        dialog_label = f"Блок #{ocr_idx}:"
        if self._validation_mode and self._validation_results:
            for cl in self._validation_results:
                if cl.block_idx == ocr_idx:
                    recognized = None
                    if cl.kks_full:
                        kks_display = _format_kks_display(
                            cl.kks_full, cl.kks_block or "", cl.kks_system or "",
                            cl.kks_fn or "", cl.kks_unit or "", cl.kks_num or "",
                            cl.kks_suffix or "",
                        )
                        if cl.diameter_text:
                            recognized = f"{kks_display} {cl.diameter_text}"
                        else:
                            recognized = kks_display
                    elif cl.diameter_text:
                        recognized = cl.diameter_text
                    if recognized:
                        suggestion = recognized
                        dialog_label = (
                            f"Блок #{ocr_idx} — распознано: {recognized}\n"
                            f"Примите или исправьте:"
                        )
                    break

        new, ok = QInputDialog.getText(self, "Редактирование OCR",
                                       dialog_label, text=suggestion)
        if not ok:
            return
        new = _clean_text(new)
        block["text"] = new
        bbox = block.get("bbox", [0, 0, 0, 0])
        self._move_ocr_visuals(ocr_idx, *bbox, new)

        # Обновить текст в обычных привязках
        for b in self._bindings:
            if b.get("ocr_block_idx") == ocr_idx:
                b["text"] = new

        self._after_change()
        self.status_message.emit(f"Текст: «{new[:40]}»")

        # В режиме валидации — переклассифицировать блок
        if self._validation_mode and self._validation_results:
            self._reclassify_block(ocr_idx)

    def _edit_diameter_label_at(self, x, y) -> bool:
        """Редактировать диаметр по клику на метке визуализации. Возвращает True если нашёл."""
        for lx1, ly1, lx2, ly2, edge_key, edge_idx, is_propagated in self._diameter_label_rects:
            if lx1 <= x <= lx2 and ly1 <= y <= ly2:
                # Проверить: это конфликтное ребро?
                conflict = None
                for cf in self._conflict_edges:
                    if cf.get("edge_key") == edge_key:
                        conflict = cf
                        break
                if conflict:
                    self._resolve_conflict(conflict)
                else:
                    self._edit_diameter_on_edge(edge_key, edge_idx, is_propagated)
                return True
        return False

    def _resolve_conflict(self, conflict: dict):
        """Диалог выбора диаметра для конфликтного ребра."""
        candidates = conflict.get("candidates", [])
        edge_key = conflict.get("edge_key", "")
        edge_idx = conflict.get("edge_idx")
        if not candidates:
            return

        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Конфликт диаметров")
        msg.setText(f"Несколько диаметров претендуют на ребро.\nВыберите правильный:")
        buttons = []
        for c in candidates:
            btn = msg.addButton(f"Ø {c}", QMessageBox.ButtonRole.ActionRole)
            buttons.append((btn, c))
        cancel_btn = msg.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == cancel_btn or clicked is None:
            return

        chosen_diameter = None
        for btn, c in buttons:
            if clicked == btn:
                chosen_diameter = c
                break
        if chosen_diameter is None:
            return

        self._push_undo()

        edge = self._graph_edges[edge_idx] if edge_idx is not None and edge_idx < len(self._graph_edges) else {}
        dm_text = f"Dy{chosen_diameter}"

        # Привязать диаметр напрямую к ребру
        self._diameter_bindings.append({
            "ocr_block_idx": None,
            "edge_idx": edge_idx or 0,
            "edge_id": edge.get("id", ""),
            "edge_key": edge_key,
            "text": dm_text, "prefix": "Dy",
            "diameter": chosen_diameter, "suffix": "",
            "confidence": 1.0,
        })
        self._repropagate_diameters()
        self._after_change()
        self.status_message.emit(f"Конфликт решён: Ø{chosen_diameter}")

    def _create_diameter_on_edge(self, x, y, edge_idx):
        """Ctrl+2×клик по ребру — ввести диаметр и привязать к ребру."""
        new_val, ok = QInputDialog.getText(
            self, "Текст на ребре", "Диаметр (число):", text=""
        )
        if not ok or not new_val.strip():
            return
        self._push_undo()
        new_val = _clean_text(new_val)

        # Создать OCR-бокс для визуализации
        avg_w, avg_h = 60, 30
        new_bbox = [x - avg_w / 2, y - avg_h / 2, x + avg_w / 2, y + avg_h / 2]
        diam_idx = len(self._ocr_blocks)
        self._ocr_blocks.append({
            "bbox": new_bbox, "text": new_val, "confidence": 1.0,
        })
        self._draw_single_ocr_block(diam_idx)

        # Добавить в текущий block_filter
        if self._block_filter is not None:
            self._block_filter.add(diam_idx)

        edge = self._graph_edges[edge_idx] if edge_idx < len(self._graph_edges) else {}
        _s, _t = edge.get('source', ''), edge.get('target', '')
        ek = f"{min(_s, _t)}|{max(_s, _t)}"

        # Чистое число → диаметр без prefix
        import re
        if re.fullmatch(r'\d+', new_val):
            from modules.text_binding.matcher import DiameterMatch
            dm = DiameterMatch(prefix="", diameter=int(new_val), suffix="",
                               text=new_val, confidence=1.0, pattern_name="manual")
        else:
            dm = self._diameter_matcher.match(new_val) if self._diameter_matcher else None

        if dm:
            # Убрать старую привязку на это ребро
            self._diameter_bindings = [
                db for db in self._diameter_bindings if db.get("edge_key") != ek
            ]
            self._diameter_bindings.append({
                "ocr_block_idx": diam_idx,
                "edge_idx": edge_idx,
                "edge_id": edge.get("id", ""),
                "edge_key": ek,
                "text": dm.text, "prefix": dm.prefix,
                "diameter": dm.diameter, "suffix": dm.suffix,
                "confidence": dm.confidence,
            })
            self._repropagate_diameters()
            self._after_change()
            self.blocks_changed.emit()
            self.status_message.emit(f"Диаметр {dm.text} → ребро (+ поток)")
        else:
            # Обычная привязка к ребру
            self._bindings.append({
                "edge_key": ek, "text": new_val,
                "ocr_block_idx": diam_idx, "bbox": new_bbox,
            })
            self._after_change()
            self.blocks_changed.emit()
            self.status_message.emit(f"Текст «{new_val}» → ребро")

    def _edit_diameter_on_edge(self, edge_key, edge_idx, is_propagated):
        """Открыть диалог редактирования диаметра на ребре."""
        current_text = ""
        current_diameter = 0
        if not is_propagated:
            for db in self._diameter_bindings:
                if db.get("edge_key") == edge_key:
                    current_text = db.get("text", "")
                    current_diameter = db.get("diameter", 0)
                    break
        else:
            for pd in self._propagated_diameters:
                if pd.get("edge_key") == edge_key:
                    current_text = pd.get("text", "")
                    current_diameter = pd.get("diameter", 0)
                    break

        new_val, ok = QInputDialog.getText(
            self, "Диаметр ребра",
            f"Диаметр (число):",
            text=current_text or str(current_diameter),
        )
        if not ok or not new_val.strip():
            return

        self._push_undo()
        new_val = _clean_text(new_val)

        dm = self._diameter_matcher.match(new_val) if self._diameter_matcher else None
        if not dm:
            try:
                d = int(new_val.strip())
                from modules.text_binding.matcher import DiameterMatch
                dm = DiameterMatch(prefix="", diameter=d, suffix="",
                                   text=str(d), confidence=1.0, pattern_name="manual")
            except ValueError:
                self.status_message.emit(f"Не удалось распознать диаметр: «{new_val}»")
                return

        # Убрать старую привязку на это ребро и добавить новую
        old_ocr_idx = None
        for db in self._diameter_bindings:
            if db.get("edge_key") == edge_key:
                old_ocr_idx = db.get("ocr_block_idx")
                break
        self._diameter_bindings = [
            db for db in self._diameter_bindings if db.get("edge_key") != edge_key
        ]
        edge = self._graph_edges[edge_idx] if edge_idx is not None and edge_idx < len(self._graph_edges) else {}
        self._diameter_bindings.append({
            "ocr_block_idx": old_ocr_idx,
            "edge_idx": edge_idx or 0,
            "edge_id": edge.get("id", ""),
            "edge_key": edge_key or "",
            "text": dm.text, "prefix": dm.prefix,
            "diameter": dm.diameter, "suffix": dm.suffix,
            "confidence": dm.confidence,
        })

        self._repropagate_diameters()
        self._after_change()
        self.status_message.emit(f"Диаметр ребра → {dm.diameter}")

    def _push_undo(self):
        """Сохранить снимок состояния в undo стек."""
        import json
        snap_blocks = json.dumps(self._ocr_blocks, ensure_ascii=False)
        snap_bindings = json.dumps(self._bindings, ensure_ascii=False)
        snap_diameter = json.dumps(self._diameter_bindings, ensure_ascii=False)
        snap_kks = json.dumps(self._kks_bindings, ensure_ascii=False)
        # Validation results — сохраняем как list of dicts
        snap_validation = None
        if self._validation_results:
            from dataclasses import asdict
            snap_validation = json.dumps(
                [asdict(cl) for cl in self._validation_results],
                ensure_ascii=False,
            )
        self._undo_stack.append((snap_blocks, snap_bindings, snap_diameter, snap_kks, snap_validation))
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)

    def _undo(self):
        """Откатить последнее действие."""
        import json
        if not self._undo_stack:
            self.status_message.emit("Нечего отменять")
            return
        snap = self._undo_stack.pop()
        # Обратная совместимость: старый формат (3 элемента) vs новый (5)
        if len(snap) == 3:
            snap_blocks, snap_bindings, snap_diameter = snap
            snap_kks, snap_validation = "[]", None
        else:
            snap_blocks, snap_bindings, snap_diameter, snap_kks, snap_validation = snap

        self._ocr_blocks = json.loads(snap_blocks)
        self._bindings = json.loads(snap_bindings)
        self._diameter_bindings = json.loads(snap_diameter)
        self._kks_bindings = json.loads(snap_kks)
        self._kks_bound_ocr_indices = {b["ocr_block_idx"] for b in self._kks_bindings}
        self._kks_bound_node_ids = {b["node_id"] for b in self._kks_bindings}

        # Восстановить validation results
        if snap_validation and self._validation_mode:
            from modules.ocr_validation.result import (
                BlockClassification, BlockType, MatchQuality,
                ValidationColor, ConfirmStatus,
            )
            raw_list = json.loads(snap_validation)
            self._validation_results = []
            for d in raw_list:
                cl = BlockClassification(
                    block_idx=d["block_idx"],
                    block_type=BlockType(d["block_type"]),
                    match_quality=MatchQuality(d["match_quality"]),
                    color=ValidationColor(d["color"]),
                    confirm_status=ConfirmStatus(d.get("confirm_status", "unconfirmed")),
                )
                cl.kks_full = d.get("kks_full")
                cl.kks_block = d.get("kks_block")
                cl.kks_system = d.get("kks_system")
                cl.kks_fn = d.get("kks_fn")
                cl.kks_unit = d.get("kks_unit")
                cl.kks_num = d.get("kks_num")
                cl.kks_suffix = d.get("kks_suffix")
                cl.kks_span = tuple(d["kks_span"]) if d.get("kks_span") else None
                cl.diameter_text = d.get("diameter_text")
                cl.diameter_value = d.get("diameter_value")
                cl.diameter_prefix = d.get("diameter_prefix")
                cl.diameter_suffix = d.get("diameter_suffix")
                cl.remaining_text = d.get("remaining_text", "")
                cl.original_text = d.get("original_text", "")
                cl.corrected_text = d.get("corrected_text", "")
                self._validation_results.append(cl)

        # Пересчитать propagation
        self._repropagate_diameters()
        # Полная перерисовка OCR слоя
        self._rebuild_bound_indices()
        # Удалить старые OCR items
        for items_dict in (self._ocr_items, self._ocr_text_items, self._ocr_text_bg_items, self._ocr_inner_text_items):
            for item in items_dict.values():
                self.scene.removeItem(item)
            items_dict.clear()
        for line in self._binding_lines:
            self.scene.removeItem(line)
        self._binding_lines.clear()
        for item in self._flag_items:
            self.scene.removeItem(item)
        self._flag_items.clear()
        # Перерисовать всё
        self._draw_ocr_blocks()
        self._redraw_all_colors()
        self._draw_bindings()
        self._redraw_diameter_bindings()
        self._redraw_kks_bindings()
        self.binding_changed.emit()
        self.status_message.emit("↩ Отменено")

    def _add_ocr_block_bbox(self, x1, y1, x2, y2):
        """П3: добавить ПУСТОЙ OCR-бокс по нарисованному прямоугольнику (без диалога).
        Текст появится после кнопки «Распознать»."""
        self._push_undo()
        idx = len(self._ocr_blocks)
        self._ocr_blocks.append({
            "bbox": [x1, y1, x2, y2], "text": "",
            "confidence": 0.0, "source": "manual",
        })
        self._draw_single_ocr_block(idx)
        if self._block_filter is not None:
            self._block_filter.add(idx)
        self._after_change()
        self.blocks_changed.emit()
        self.status_message.emit(f"Добавлен пустой блок #{idx} — нажмите «Распознать»")

    def _cursor_scene_pos(self) -> QPointF:
        """Позиция курсора мыши в координатах сцены.

        Курсор вне вьюпорта → fallback в центр вьюпорта.
        """
        vp = self.viewport()
        pt = vp.mapFromGlobal(QCursor.pos())
        if not vp.rect().contains(pt):
            pt = vp.rect().center()
        return self.mapToScene(pt)

    def _copy_ocr_blocks(self):
        """Ctrl+C: приоритет у блока под курсором.

        Блок под курсором вне выделения → выделение переключается на него;
        блок в составе выделения или курсор в пустоте → копируется текущее
        выделение. Скопированное остаётся выделенным (зелёная рамка) —
        видно, что именно в буфере; Esc для смены копируемого не нужен.
        """
        pos = self._cursor_scene_pos()
        cur = self._find_ocr_at(pos.x(), pos.y())
        if cur is not None and cur not in self._selected_ocr:
            self._selected_ocr = {cur}
            self._redraw_all_colors()
        idxs = sorted(self._selected_ocr)
        clip = []
        for idx in idxs:
            if not isinstance(idx, int) or idx >= len(self._ocr_blocks):
                continue
            block = self._ocr_blocks[idx]
            if block.get("merged_into") is not None:
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            clip.append(([float(v) for v in bbox], block.get("text", "") or ""))
        if not clip:
            self.status_message.emit("Копировать: выделите блоки или наведите курсор на блок")
            return
        self._ocr_clipboard = clip
        self.status_message.emit(f"Скопировано блоков: {len(clip)} (Ctrl+V — вставить)")

    def _start_paste_ghost(self):
        """Ctrl+V: показать полупрозрачный призрак буфера, следующий за мышью.

        Вставка больше не мгновенная: Ctrl+ЛКМ фиксирует набор в позиции
        призрака, Esc отменяет. Повторный Ctrl+V при активном призраке — игнор.
        Пока призрак активен, все прочие жесты мыши в этом view отключены.
        """
        if self._paste_ghost is not None:
            return  # призрак уже активен
        clip = self._ocr_clipboard
        if not clip:
            self.status_message.emit("Буфер блоков пуст — сначала Ctrl+C")
            return
        # Рамки блоков в исходных координатах, двигаем одной группой
        # (не пересоздаём на каждый move); пунктир, полупрозрачно, поверх всего.
        # У непустых блоков — призрачная подпись с текстом (геометрия как у
        # реальных: верх-лево / низ-лево с -90°, см. _label_geometry).
        pen = QPen(self._c_textbox, 2, Qt.PenStyle.DashLine)
        font = QFont("DejaVu Sans", self._label_pt)
        fm = QFontMetricsF(font)
        group = QGraphicsItemGroup()
        for bbox, text in clip:
            it = QGraphicsRectItem(
                bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
            it.setPen(pen)
            group.addToGroup(it)
            t = (text or "").strip()
            if t:
                th = fm.height()
                tw = fm.horizontalAdvance(t)
                lx, ly, rot, bg_rect = self._label_geometry(
                    bbox[0], bbox[1], bbox[2], bbox[3], tw, th)
                bg = QGraphicsRectItem(*bg_rect)
                bg.setPen(QPen(Qt.PenStyle.NoPen))
                bg.setBrush(QBrush(COLOR_TEXT_BG))
                group.addToGroup(bg)
                label = QGraphicsSimpleTextItem(t)
                label.setFont(font)
                label.setBrush(QBrush(COLOR_TEXT_LABEL))
                label.setPos(lx, ly)
                label.setRotation(rot)
                group.addToGroup(label)
        group.setZValue(100)
        group.setOpacity(0.5)
        self.scene.addItem(group)
        gx1 = min(b[0][0] for b in clip)
        gy1 = min(b[0][1] for b in clip)
        gx2 = max(b[0][2] for b in clip)
        gy2 = max(b[0][3] for b in clip)
        self._paste_ghost = {
            "group": group,
            "center": ((gx1 + gx2) / 2.0, (gy1 + gy2) / 2.0),
        }
        pos = self._cursor_scene_pos()
        self._move_paste_ghost(pos.x(), pos.y())
        self.status_message.emit("Ctrl+ЛКМ — вставить, Esc — отмена")

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
        self._cancel_paste_ghost()
        self._paste_ocr_blocks(offset.x(), offset.y())

    def _cancel_paste_ghost(self):
        """Убрать призрак вставки со сцены (Esc / перед фиксацией)."""
        g = self._paste_ghost
        self._paste_ghost = None
        if g and g["group"].scene() is not None:
            self.scene.removeItem(g["group"])

    def _paste_ocr_blocks(self, dx: float, dy: float):
        """Зафиксировать вставку буфера блоков со сдвигом (dx, dy).

        Вызывается фиксацией призрака (Ctrl+V → призрак → Ctrl+ЛКМ).
        Копии создаются без привязок. Один снимок undo ДО всех мутаций —
        вся вставка отменяется одним Ctrl+Z.
        """
        clip = self._ocr_clipboard
        if not clip:
            self.status_message.emit("Буфер блоков пуст — сначала Ctrl+C")
            return
        self._push_undo()  # снапшот ДО правки (семантика как в _add_ocr_block_bbox)
        new_idxs = []
        for bbox, text in clip:
            idx = len(self._ocr_blocks)
            self._ocr_blocks.append({
                "bbox": [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy],
                "text": text,
                "confidence": 0.0, "source": "manual",
            })
            self._draw_single_ocr_block(idx)
            if self._block_filter is not None:
                self._block_filter.add(idx)
            # Классифицировать копию с текстом (цвет в режиме валидации)
            if text and getattr(self, "_ocr_classifier", None):
                self._reclassify_block(idx)
            new_idxs.append(idx)
        self._selected_ocr = set(new_idxs)
        self._after_change()
        self.blocks_changed.emit()  # таб выставит _saved = False (как при добавлении блока)
        self.status_message.emit(f"Вставлено блоков: {len(new_idxs)}")

    def _add_ocr_block(self, x: float, y: float):
        """Добавить новый OCR-бокс в позиции (x, y)."""
        self._push_undo()
        w, h = 120, 30
        x1, y1 = x - w / 2, y - h / 2
        x2, y2 = x + w / 2, y + h / 2
        idx = len(self._ocr_blocks)
        self._ocr_blocks.append({
            "bbox": [x1, y1, x2, y2], "text": "",
            "confidence": 1.0,
        })
        self._draw_single_ocr_block(idx)

        # Диалог ввода текста (блокирующий)
        new_text, ok = QInputDialog.getText(self, "Новый блок", "Текст:", text="")
        if not ok or not new_text.strip():
            # Отмена — удалить блок
            self._ocr_blocks[idx]["merged_into"] = -1
            if idx in self._ocr_items:
                self._ocr_items[idx].setVisible(False)
            return
        new_text = _clean_text(new_text)
        self._ocr_blocks[idx]["text"] = new_text

        # Подогнать размер бокса под текст
        font = QFont("DejaVu Sans", self._label_pt)
        fm = QFontMetricsF(font)
        tw = fm.horizontalAdvance(new_text) + 12
        th = fm.height() + 8
        new_w = max(tw, 40)
        new_h = max(th, 20)
        nx1 = x - new_w / 2
        ny1 = y - new_h / 2
        nx2 = x + new_w / 2
        ny2 = y + new_h / 2
        self._ocr_blocks[idx]["bbox"] = [nx1, ny1, nx2, ny2]
        self._move_ocr_visuals(idx, nx1, ny1, nx2, ny2, new_text)

        # Классифицировать новый блок (для цвета)
        if hasattr(self, '_ocr_classifier') and self._ocr_classifier:
            self._reclassify_block(idx)

        # Добавить в текущий block_filter чтобы блок был виден сразу
        if self._block_filter is not None:
            self._block_filter.add(idx)

        self._after_change()
        self.blocks_changed.emit()
        self.status_message.emit(f"Добавлен блок #{idx}: «{new_text[:30]}»")

    def _draw_single_ocr_block(self, idx):
        """Нарисовать один OCR-бокс."""
        block = self._ocr_blocks[idx]
        bbox = block.get("bbox")
        text = _clean_text(block.get("text", ""))
        conf = block.get("confidence", 0)
        if not bbox or len(bbox) != 4:
            return
        x1, y1, x2, y2 = bbox
        rect = self.scene.addRect(x1, y1, x2 - x1, y2 - y1, self._ocr_pen(idx),
                                  QBrush(Qt.BrushStyle.NoBrush))
        rect.setZValue(10)
        self._ocr_items[idx] = rect
        if text:
            font = QFont("DejaVu Sans", self._label_pt)
            fm = QFontMetricsF(font)
            th = fm.height()
            tw = fm.horizontalAdvance(text)
            lx, ly, rot, bg_rect = self._label_geometry(x1, y1, x2, y2, tw, th)
            bg = self.scene.addRect(*bg_rect,
                                    QPen(Qt.PenStyle.NoPen), QBrush(COLOR_TEXT_BG))
            bg.setZValue(11)
            self._ocr_text_bg_items[idx] = bg
            label = self.scene.addSimpleText(text, font)
            label.setBrush(QBrush(COLOR_TEXT_LABEL))
            label.setPos(lx, ly)
            label.setRotation(rot)
            label.setZValue(12)
            self._ocr_text_items[idx] = label

    def _delete_selected_ocr_blocks(self):
        """Удалить все выделенные блоки — клавиша Delete.

        (Раньше пачку удалял Ctrl+ПКМ по выделенному; теперь Ctrl+ПКМ только
        исключает блок из выделения.)
        """
        if not self._selected_ocr:
            self.status_message.emit("Нет выделенных блоков")
            return
        for i in sorted(self._selected_ocr):
            self._delete_ocr_block(i)
        self._selected_ocr.clear()

    def _delete_ocr_block(self, idx: int):
        """Удалить OCR-бокс и все его привязки."""
        self._push_undo()
        # Убрать привязки
        self._bindings = [b for b in self._bindings if b.get("ocr_block_idx") != idx]
        # Полностью удалить визуалы из сцены (не просто скрыть)
        for items in (self._ocr_items, self._ocr_text_items, self._ocr_text_bg_items, self._ocr_inner_text_items):
            it = items.pop(idx, None)
            if it is not None:
                try:
                    self.scene.removeItem(it)
                except Exception:
                    pass
        # Пометить как удалённый
        if idx < len(self._ocr_blocks):
            self._ocr_blocks[idx]["merged_into"] = -1  # -1 = deleted
        self._after_change()
        self.blocks_changed.emit()
        self.status_message.emit(f"Удалён блок #{idx}")

        # В режиме валидации — убрать classification
        if self._validation_mode and self._validation_results:
            self._validation_results = [
                cl for cl in self._validation_results
                if cl.block_idx != idx
            ]

    def _after_change(self):
        self._rebuild_bound_indices()
        self._rebuild_diameter_bound_indices()
        self._redraw_all_colors()

        # Перерисовать ВСЕ типы привязок: каждый redraw сначала чистит старые items
        self._redraw_bindings()
        self._redraw_diameter_bindings()
        self._redraw_kks_bindings()

        # Скрыть merged блоки
        for idx in list(self._ocr_text_items.keys()):
            if idx >= len(self._ocr_blocks):
                continue
            if self._ocr_blocks[idx].get("merged_into") is not None:
                for items in (self._ocr_items, self._ocr_text_items,
                              self._ocr_text_bg_items, self._ocr_inner_text_items):
                    if idx in items:
                        items[idx].setVisible(False)

        self.binding_changed.emit()

    # =================================================================
    # Events
    # =================================================================

    def wheelEvent(self, event):
        f = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def _any_mode_active(self) -> bool:
        return self._add_mode or self._del_mode or self._move_mode

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = True
            if not self._any_mode_active():
                self.setDragMode(QGraphicsView.DragMode.NoDrag)
                self.setCursor(Qt.CursorShape.CrossCursor)
        elif event.key() == Qt.Key.Key_Z and self.ctrl_pressed:
            self._undo()
        elif event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Пока активен призрак вставки — буфер менять нельзя (фиксация
            # вставляет текущий буфер; иначе призрак разойдётся с содержимым).
            if self._paste_ghost is not None:
                return
            # Копипаст блоков: проверяем модификатор события (не флаг ctrl_pressed)
            self._copy_ocr_blocks()
        elif event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+V показывает призрак вставки (фиксация — Ctrl+ЛКМ,
            # отмена — Esc); повторный Ctrl+V при активном призраке — игнор.
            self._start_paste_ghost()
        elif event.key() == Qt.Key.Key_Delete:
            # Удаление выделенных блоков — только клавишей Delete.
            self._delete_selected_ocr_blocks()
        elif event.key() == Qt.Key.Key_Escape:
            # Призрак вставки — отменить первым приоритетом.
            if self._paste_ghost is not None:
                self._cancel_paste_ghost()
                self.status_message.emit("Вставка отменена")
                return
            self._abort_drag()
            self._add_mode = False
            self._del_mode = False
            self._move_mode = False
            self.ctrl_pressed = False
            if self._selected_ocr:
                self._selected_ocr.clear()
                self._redraw_all_colors()
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.mode_changed.emit("idle")
            self.status_message.emit("")
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Control:
            self.ctrl_pressed = False
            self._abort_drag()
            if not self._any_mode_active():
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            super().keyReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        # B6.1: DoubleClick без Ctrl для редактирования (унификация с graph_editor)
        # Не срабатывает в спец-режимах (add/del/move — обрабатываются в mousePressEvent)
        if self._add_mode or self._del_mode or self._move_mode:
            super().mouseDoubleClickEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            # П3: отменить возможный drag, начатый первым кликом Ctrl+ЛКМ
            self._drag_idx = None
            if getattr(self, '_drag_line', None):
                self.scene.removeItem(self._drag_line)
                self._drag_line = None
            self._clear_highlights()
            idx = self._find_ocr_at(x, y)
            if idx is None:
                sec_idx = self._find_secondary_at(x, y)
                if sec_idx is not None:
                    idx = self._promote_secondary_block(sec_idx)
            # П3: редактирование текста только по Ctrl+2ЛКМ
            if idx is not None and self.ctrl_pressed:
                self._edit_text(idx)
            self.ctrl_pressed = False
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        # Призрак вставки: все прочие жесты мыши отключены;
        # Ctrl+ЛКМ фиксирует вставку в позиции призрака.
        if self._paste_ghost is not None:
            if event.button() == Qt.MouseButton.LeftButton and (
                    self.ctrl_pressed
                    or event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._commit_paste_ghost()
            else:
                self.status_message.emit("Ctrl+ЛКМ — вставить, Esc — отмена")
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            # Ctrl+RMB unbind handled in mouseReleaseEvent
            if self.ctrl_pressed and event.button() == Qt.MouseButton.RightButton:
                event.accept()
                return
            super().mousePressEvent(event)
            return

        pos = self.mapToScene(event.pos())
        x, y = pos.x(), pos.y()

        # П3: Add mode — начать рисование прямоугольника (как добавление узла)
        if self._add_mode:
            self._add_bbox_start = (x, y)
            _pen = QPen(QColor(0, 200, 0, 220))
            _pen.setWidth(2)
            _pen.setStyle(Qt.PenStyle.DashLine)
            _pen.setCosmetic(True)
            self._add_bbox_preview = self.scene.addRect(
                x, y, 0, 0, _pen, QBrush(QColor(0, 200, 0, 40)))
            self._add_bbox_preview.setZValue(50)
            event.accept()
            return

        # Del mode: клик на боксе → удалить
        if self._del_mode:
            idx = self._find_ocr_at(x, y)
            if idx is not None:
                self._delete_ocr_block(idx)
            event.accept()
            return

        # Move mode: начать перемещение бокса
        if self._move_mode:
            idx = self._find_ocr_at(x, y)
            if idx is not None:
                block = self._ocr_blocks[idx]
                bbox = block.get("bbox", [0, 0, 0, 0])
                self._move_idx = idx
                self._move_origin_bbox = bbox.copy()
                self._push_undo()
                bcx, bcy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                self._drag_offset = (x - bcx, y - bcy)
            event.accept()
            return

        # П3: Shift+ЛКМ — выделение (toggle бокса под курсором или рамка на пустом)
        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) and not self.ctrl_pressed:
            idx = self._find_ocr_at(x, y)
            if idx is not None:
                if idx in self._selected_ocr:
                    self._selected_ocr.discard(idx)
                else:
                    self._selected_ocr.add(idx)
                self._redraw_all_colors()
            else:
                self._rb_start = (x, y)
                _rp = QPen(COLOR_SELECT_GREEN, 1, Qt.PenStyle.DashLine)
                self._rubber_band = self.scene.addRect(
                    x, y, 0, 0, _rp, QBrush(QColor(46, 204, 113, 40)))
                self._rubber_band.setZValue(60)
            event.accept()
            return

        # Ctrl+LMB: drag to bind / Ctrl+Shift: add box
        if self.ctrl_pressed:
            idx = self._find_ocr_at(x, y)

            # Промотировать secondary блок если основной не найден
            if idx is None:
                sec_idx = self._find_secondary_at(x, y)
                if sec_idx is not None:
                    idx = self._promote_secondary_block(sec_idx)

            # Ctrl+Shift+Click на пустом месте → новый бокс
            if idx is None and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._add_ocr_block(x, y)
                event.accept()
                return

            if idx is not None:
                # Начать drag OCR-бокса (клик/drag различаем на release по порогу)
                block = self._ocr_blocks[idx]
                bbox = block.get("bbox", [0, 0, 0, 0])
                self._drag_idx = idx
                self._drag_origin_bbox = bbox.copy()
                bcx, bcy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                self._drag_offset = (x - bcx, y - bcy)
                self._ctrl_press_xy = (x, y)
                text = block.get("text", "")[:25]
                self.status_message.emit(f"Тяните «{text}» на цель...")
            else:
                # Блока нет: Ctrl+ЛКМ по центроиду узла с привязками —
                # повернуть его привязки на следующую сторону по часовой.
                nid = self._find_node_at(x, y)
                if nid is not None:
                    self._rotate_node_bindings(nid)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Призрак вставки следует за мышью; остальные жесты отключены.
        if self._paste_ghost is not None:
            pos = self.mapToScene(event.pos())
            self._move_paste_ghost(pos.x(), pos.y())
            event.accept()
            return
        # П3: Shift rubber-band — тянем рамку выделения
        if self._rubber_band is not None and self._rb_start is not None:
            pos = self.mapToScene(event.pos())
            sx, sy = self._rb_start
            self._rubber_band.setRect(min(sx, pos.x()), min(sy, pos.y()),
                                      abs(pos.x() - sx), abs(pos.y() - sy))
            event.accept()
            return
        # П3: Add mode — тянем прямоугольник (рисование бокса)
        if self._add_mode and self._add_bbox_start is not None and self._add_bbox_preview is not None:
            pos = self.mapToScene(event.pos())
            sx, sy = self._add_bbox_start
            self._add_bbox_preview.setRect(
                min(sx, pos.x()), min(sy, pos.y()),
                abs(pos.x() - sx), abs(pos.y() - sy))
            event.accept()
            return
        # Move mode: перемещение бокса (без привязки, просто двигаем)
        if self._move_mode and self._move_idx is not None:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            block = self._ocr_blocks[self._move_idx]
            bbox = block.get("bbox", [0, 0, 0, 0])
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            ox, oy = self._drag_offset
            ncx, ncy = x - ox, y - oy
            nx1, ny1 = ncx - bw / 2, ncy - bh / 2
            nx2, ny2 = ncx + bw / 2, ncy + bh / 2
            block["bbox"] = [nx1, ny1, nx2, ny2]
            self._move_ocr_visuals(self._move_idx, nx1, ny1, nx2, ny2)
            event.accept()
            return

        if self.ctrl_pressed and self._drag_idx is not None:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            block = self._ocr_blocks[self._drag_idx]
            bbox = block.get("bbox", [0, 0, 0, 0])
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            ox, oy = self._drag_offset
            ncx, ncy = x - ox, y - oy
            nx1, ny1 = ncx - bw / 2, ncy - bh / 2
            nx2, ny2 = ncx + bw / 2, ncy + bh / 2

            # Двигаем сам бокс
            self._move_ocr_visuals(self._drag_idx, nx1, ny1, nx2, ny2)

            # Убрать предыдущую drag-линию
            if hasattr(self, '_drag_line') and self._drag_line:
                self.scene.removeItem(self._drag_line)
                self._drag_line = None

            # П3: текущая привязка только из обычных _bindings
            current_target = None
            for b in self._bindings:
                if b.get("ocr_block_idx") == self._drag_idx:
                    if b.get("node_id"):
                        current_target = self._get_node_center(b["node_id"])
                    elif b.get("edge_key"):
                        current_target = self._get_edge_midpoint_by_key(b["edge_key"])
                    break

            self._clear_highlights()
            nid = None
            new_target = None
            allow_node = True
            allow_edge = True

            nid = self._find_node_at(x, y) if allow_node else None
            if nid:
                self._highlight_node(nid)
                self._drop_target_type, self._drop_target_id = "node", nid
                new_target = self._get_node_center(nid)
            else:
                eidx = self._find_edge_by_bbox(nx1, ny1, nx2, ny2) if allow_edge else None
                if eidx is not None:
                    self._highlight_edge(eidx)
                    self._drop_target_type, self._drop_target_id = "edge", eidx
                    ek = self._edge_key_str(eidx)
                    if ek:
                        new_target = self._get_edge_midpoint_by_key(ek)
                else:
                    ocr_t = self._find_ocr_at(x, y, exclude=self._drag_idx)
                    if ocr_t is not None:
                        self._highlight_ocr(ocr_t)
                        self._drop_target_type, self._drop_target_id = "ocr", ocr_t
                    else:
                        self._drop_target_type, self._drop_target_id = None, None

            # Жёлтый пунктир: к новой цели если есть, иначе к текущей привязке
            draw_target = new_target if (new_target and new_target[0] is not None) else current_target
            if draw_target and draw_target[0] is not None:
                pen_drag = QPen(QColor(255, 230, 0, 220), 2, Qt.PenStyle.DashLine)
                self._drag_line = self.scene.addLine(
                    ncx, ncy, draw_target[0], draw_target[1], pen_drag
                )
                self._drag_line.setZValue(25)

            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # Пока активен призрак вставки — жесты мыши не доходят до редактора.
        if self._paste_ghost is not None:
            event.accept()
            return
        # П3: Shift rubber-band — завершить выделение (боксы внутри рамки → зелёные)
        if self._rubber_band is not None and self._rb_start is not None:
            pos = self.mapToScene(event.pos())
            sx, sy = self._rb_start
            rx1, ry1 = min(sx, pos.x()), min(sy, pos.y())
            rx2, ry2 = max(sx, pos.x()), max(sy, pos.y())
            self.scene.removeItem(self._rubber_band)
            self._rubber_band = None
            self._rb_start = None
            for i, blk in enumerate(self._ocr_blocks):
                if blk.get("merged_into") is not None:
                    continue
                bb = blk.get("bbox")
                if not bb or len(bb) != 4:
                    continue
                if bb[0] <= rx2 and bb[2] >= rx1 and bb[1] <= ry2 and bb[3] >= ry1:
                    self._selected_ocr.add(i)
            self._redraw_all_colors()
            self.status_message.emit(f"Выделено: {len(self._selected_ocr)} (Delete — удалить)")
            event.accept()
            return
        # П3: Add mode — завершить рисование → ПУСТОЙ блок (текст по кнопке «Распознать»)
        if self._add_mode and self._add_bbox_start is not None:
            pos = self.mapToScene(event.pos())
            sx, sy = self._add_bbox_start
            self._add_bbox_start = None
            if self._add_bbox_preview is not None:
                self.scene.removeItem(self._add_bbox_preview)
                self._add_bbox_preview = None
            if abs(pos.x() - sx) >= 5 and abs(pos.y() - sy) >= 5:
                self._add_ocr_block_bbox(
                    min(sx, pos.x()), min(sy, pos.y()),
                    max(sx, pos.x()), max(sy, pos.y()))
            event.accept()
            return
        # Move mode: завершить перемещение
        if self._move_mode and self._move_idx is not None:
            idx = self._move_idx
            self._move_idx = None
            self._move_origin_bbox = []
            # Обновить bbox в привязках
            block = self._ocr_blocks[idx]
            new_bbox = block.get("bbox", [])
            for b in self._bindings:
                if b.get("ocr_block_idx") == idx:
                    b["bbox"] = new_bbox
            self._after_change()
            self.status_message.emit("Бокс перемещён")
            event.accept()
            return

        if self._drag_idx is not None:
            idx = self._drag_idx
            tt, tid = self._drop_target_type, self._drop_target_id
            self._clear_highlights()
            self._drag_idx = None
            self._drop_target_type = self._drop_target_id = None
            # Убрать drag-линию
            if self._drag_line:
                self.scene.removeItem(self._drag_line)
                self._drag_line = None

            pos = self.mapToScene(event.pos())
            # Клик без сдвига (порог OCR_CLICK_MOVE_THRESHOLD): при активном
            # выделении добавляет НЕвыделенный блок в выделение;
            # drag/привязка — только с реальным движением.
            px, py = self._ctrl_press_xy or (pos.x(), pos.y())
            self._ctrl_press_xy = None
            moved = math.hypot(pos.x() - px, pos.y() - py) \
                >= self.OCR_CLICK_MOVE_THRESHOLD
            if not moved and self._selected_ocr and idx not in self._selected_ocr:
                self._restore_ocr_pos(idx)  # вернуть микросдвиг визуалов
                self._selected_ocr.add(idx)
                self._redraw_all_colors()
                self.status_message.emit(
                    f"Выделено блоков: {len(self._selected_ocr)} (Delete — удалить)")
                event.accept()
                return
            if tt == "node":
                # привязка ставит блок по центру выбранной стороны цели —
                # сторона от исходного положения блока, место броска не влияет
                self._drag_origin_bbox = []
                self._bind_to_node(idx, tid)
            elif tt == "edge":
                self._drag_origin_bbox = []
                self._bind_to_edge(idx, tid)
            else:
                # перемещение на пустое место — зафиксировать позицию (с undo)
                self._push_undo()
                self._commit_drag_pos(idx, pos.x(), pos.y())
                self._after_change()
                self.status_message.emit("Бокс перемещён")

            event.accept()
            return

        # П3: Ctrl+ПКМ — при активном выделении только исключает блок из
        #     выделения (удаление выделенных — клавишей Delete);
        #     без выделения: привязан → отвязать, не привязан → удалить
        if self.ctrl_pressed and event.button() == Qt.MouseButton.RightButton:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            if self._selected_ocr:
                idx = self._find_ocr_at(x, y)
                if idx is not None and idx in self._selected_ocr:
                    self._selected_ocr.discard(idx)
                    self._redraw_all_colors()
                    self.status_message.emit(
                        f"Блок исключён из выделения (осталось {len(self._selected_ocr)})")
                # По любому другому объекту при активном выделении — ничего.
                event.accept()
                return
            idx = self._find_ocr_at(x, y)
            if idx is None:
                sec_idx = self._find_secondary_at(x, y)
                if sec_idx is not None:
                    idx = self._promote_secondary_block(sec_idx)
            if idx is not None:
                if idx in self._bound_ocr_indices:
                    self._unbind_ocr(idx)
                else:
                    self._delete_ocr_block(idx)
            else:
                self.status_message.emit("Нет блока под курсором")
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _restore_ocr_pos(self, idx):
        if self._drag_origin_bbox:
            x1, y1, x2, y2 = self._drag_origin_bbox
            self._move_ocr_visuals(idx, x1, y1, x2, y2)
        self._drag_origin_bbox = []

    def _commit_drag_pos(self, idx, x, y):
        """П3: зафиксировать новое положение бокса после Ctrl+ЛКМ перетаскивания."""
        if not self._drag_origin_bbox:
            return
        ox1, oy1, ox2, oy2 = self._drag_origin_bbox
        bw, bh = ox2 - ox1, oy2 - oy1
        offx, offy = self._drag_offset
        ncx, ncy = x - offx, y - offy
        nb = [ncx - bw / 2, ncy - bh / 2, ncx + bw / 2, ncy + bh / 2]
        if idx < len(self._ocr_blocks):
            self._ocr_blocks[idx]["bbox"] = nb
        for b in self._bindings:
            if b.get("ocr_block_idx") == idx:
                b["bbox"] = nb
        self._move_ocr_visuals(idx, *nb)
        self._drag_origin_bbox = []

    def _unbind_ocr(self, idx):
        """П3: снять привязку текст-блока (рамка снова голубая)."""
        self._push_undo()
        self._bindings = [b for b in self._bindings if b.get("ocr_block_idx") != idx]
        self._after_change()
        self.status_message.emit("Привязка снята")

    def _abort_drag(self):
        if self._drag_idx is not None:
            self._restore_ocr_pos(self._drag_idx)
            self._clear_highlights()
            self._drag_idx = None
            self._drop_target_type = self._drop_target_id = None
        if self._drag_line:
            self.scene.removeItem(self._drag_line)
            self._drag_line = None
