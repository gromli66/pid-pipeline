"""
OCR Layer Mixin — слой текст-блоков и привязки для AdvancedGraphEditor.

Портирует функционал вкладки «бусина привязка» в единый редактор графа:
  • добавление текст-блоков рамкой (режим add_ocr_block);
  • перетаскивание блока на узел/ребро → привязка (режим ocr_bind);
  • блоки как во вкладке привязки: рамка золото=привязан / синяя=не привязан,
    без заливки, подпись над боксом с тёмной подложкой;
  • раскраска узлов/рёбер по привязке (золото=привязан) — как в «бусине»;
  • линии привязки блок→узел/ребро; подсветка цели при перетаскивании;
  • распознавание добавленных блоков (асинхронно — запускается из таба).

Данные живут в общем графе (GraphDataModel.text_blocks / bindings),
поэтому Undo/Save/Export общие с остальной правкой графа.

Слой активен и виден ТОЛЬКО в состоянии display_regime == 'ocr'.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen, QBrush, QFont, QFontMetricsF
from PySide6.QtWidgets import (
    QGraphicsRectItem, QGraphicsSimpleTextItem, QGraphicsLineItem,
)

from ui.editors.mode_handlers.base_handler import ModeHandler


# ── Цвета блоков — РОВНО как в «бусине» (_ocr_pen): цвет = состояние
#    привязки, БЕЗ заливки. Золото=привязан, синий=не привязан, зелёный=выделен.
_COLOR_BOUND = QColor(255, 200, 0, 255)       # золото — привязан (COLOR_BOUND_GOLD)
_COLOR_UNBOUND = QColor(80, 160, 255, 240)    # синий — не привязан (COLOR_UNBOUND_BLUE)
_COLOR_SELECT = QColor(46, 204, 113, 255)     # зелёный — выделен (COLOR_SELECT_GREEN)
_COLOR_DROP = QColor(255, 230, 0, 160)        # подсветка цели при drag
_COLOR_TEXT = QColor(255, 255, 255, 230)      # текст подписи (COLOR_TEXT_LABEL)
_COLOR_TEXT_BG = QColor(0, 0, 0, 140)         # подложка под текстом (COLOR_TEXT_BG)
# Узлы/рёбра в состоянии ОКР — как в «бусине».
_NODE_CENTROID = QColor(52, 152, 219, 230)    # синий центроид (COLOR_CENTROID)
_NODE_EQUIP_GREY = QColor(235, 235, 235, 245) # серый bbox оборудования (COLOR_EQUIP_GREY)
_NODE_CONNECTOR = QColor(150, 150, 150, 200)  # серый коннектор (COLOR_CONNECTOR)
_LABEL_PT = 9

_MIN_BLOCK_SIZE = 5.0
_BLOCK_Z = 50.0
_LINE_Z = 48.0
# Отступ привязанного блока от границы цели (авто-позиция side+gap), px.
_BIND_GAP = 6.0
# Порог «вертикальный» блок: h > w * 1.3 (квадрат — горизонтальный).
# Продублировано из modules/graph_to_fxml.py:_TEXT_VERTICAL_RATIO —
# менять синхронно, иначе редактор разойдётся с итоговым FXML.
_TEXT_VERTICAL_RATIO = 1.3


def _block_is_vertical(x1: float, y1: float, x2: float, y2: float) -> bool:
    """Вертикальный текст-блок (текст пишется снизу вверх) — по аспекту bbox."""
    return (y2 - y1) > (x2 - x1) * _TEXT_VERTICAL_RATIO


class OcrLayerMixin:
    """Слой OCR текст-блоков и привязок. Подмешивается в AdvancedGraphEditor.

    Не переопределяет события мыши базового редактора — вся интерактивность
    идёт через ModeHandler'ы (AddOcrBlockHandler, OcrBindHandler).
    """

    # Толщина рамки всего, что относится к слою ОКР: текст-блоки и рамки
    # ОКР-объектов. Поле экземпляра (не модульная константа) — его крутит
    # ползунок «толщина рамки текст-боксов» через _size_factors. Применяется
    # ВСЕГДА с _ocr_vis_scale(): в холсте блок ~15x6 px, без множителя рамка
    # задавила бы сам блок.
    OCR_BORDER_W = 2.0

    def _ocr_vis_scale(self) -> float:
        """Множитель размеров ВНУТРИ текст-блока (рамка, шрифт, линия привязки).

        Символы в холсте имеют фиксированный размер, а текст-блоки — нет: они
        ужаты вместе с растром (bbox × s), поэтому типичный блок ~50x20 px в
        оригинале становится ~15x6 px. Константы ниже подобраны под сцену=растр,
        и без этого множителя рамка со шрифтом перекрывают сам блок.
        Вне холста — 1.0 (прежний вид).
        """
        if not getattr(self, "_canvas_mode", False):
            return 1.0
        return getattr(self, "_bg_scale", 1.0) or 1.0

    # -----------------------------------------------------------------
    # Инициализация / состояние
    # -----------------------------------------------------------------
    def _init_ocr_layer(self):
        # База для ползунка «толщина рамки текст-боксов» (см. _apply_visuals).
        self._size_base_extra["OCR_BORDER_W"] = self.OCR_BORDER_W
        # block_id -> {"rect":..., "text":..., "line":...}
        self._ocr_block_items: dict[str, dict] = {}
        self._ocr_drag_id: str | None = None
        self._ocr_drag_dx: float = 0.0
        self._ocr_drag_dy: float = 0.0
        self._ocr_drag_cmd = None
        self._ocr_add_preview: QGraphicsRectItem | None = None
        self._ocr_add_start: tuple[float, float] | None = None
        self._ocr_hl_restore: list = []
        self._selected_ocr: set[str] = set()  # Shift-выделенные блоки (зелёные)
        self._ocr_clipboard: list[tuple[list, str]] = []  # буфер копипаста: [(bbox, text), ...]
        # Ручки изменения размера текст-блока (только в состоянии 'ocr').
        self._ocr_resize_overlay = None       # ResizableNodeOverlay | None
        self._ocr_resize_block_id: str | None = None
        self._ocr_resize_lock = False         # тело не двигать, пока показаны ручки
        # Отложенный Ctrl+ЛКМ-клик по узлу (вращение привязок по часовой).
        self._ocr_rotate_nid: str | None = None

    # -----------------------------------------------------------------
    # Отрисовка
    # -----------------------------------------------------------------
    def refresh_ocr_layer(self):
        """Перерисовать все текст-блоки и линии привязки из модели."""
        self._clear_ocr_items()
        visible = getattr(self, "display_regime", "base") == "ocr"
        for blk in self.model.text_blocks:
            if blk.get("merged_into") is not None:
                continue
            self._draw_ocr_block(blk, visible)
        if visible:
            self._apply_ocr_node_colors()

    def _clear_ocr_items(self):
        for pair in self._ocr_block_items.values():
            for it in pair.values():
                if it is not None and it.scene() is not None:
                    self.scene.removeItem(it)
        self._ocr_block_items.clear()

    def _draw_ocr_block(self, blk: dict, visible: bool = True):
        bbox = blk.get("bbox")
        if not bbox or len(bbox) != 4:
            return
        binding = self.model.find_binding(blk.get("id"))
        bound = binding is not None

        # Привязка с side/gap: позиция блока производная от ТЕКУЩЕЙ геометрии
        # цели (следование за перемещением/resize). Производную синкаем в
        # blk['bbox'], чтобы hit-test/выделение/сохранение видели актуальное
        # положение. Старые привязки без side — блок лежит где лежал.
        if bound and binding.get("side"):
            w0 = max(1.0, float(bbox[2]) - float(bbox[0]))
            h0 = max(1.0, float(bbox[3]) - float(bbox[1]))
            eff = self._bound_block_bbox(binding, w0, h0)
            if eff is not None:
                blk["bbox"] = eff
                bbox = eff
        x1, y1, x2, y2 = [float(v) for v in bbox]
        w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)

        text = (blk.get("text") or "").strip()

        # Линия привязки блок → цель — золотой пунктир (как в «бусине»).
        line = None
        if bound:
            tgt = self._binding_target_center(binding)
            if tgt is not None:
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                line = QGraphicsLineItem(cx, cy, tgt[0], tgt[1])
                pen = QPen(_COLOR_BOUND, 1.5 * self._ocr_vis_scale())
                pen.setStyle(Qt.PenStyle.DashLine)
                line.setPen(pen)
                line.setZValue(_LINE_Z)
                line.setVisible(visible)
                self.scene.addItem(line)

        # Рамка: золото = привязан, синий = не привязан. БЕЗ заливки.
        rect = QGraphicsRectItem(x1, y1, w, h)
        if blk.get("id") in self._selected_ocr:
            border = _COLOR_SELECT          # выделен (Shift)
        elif bound:
            border = _COLOR_BOUND           # привязан
        else:
            border = _COLOR_UNBOUND         # не привязан
        rect.setPen(QPen(border, self.OCR_BORDER_W * self._ocr_vis_scale()))
        rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect.setZValue(_BLOCK_Z)
        rect.setVisible(visible)
        self.scene.addItem(rect)

        # Подпись с тёмной подложкой (унифицировано с «бусиной»).
        # Горизонтальный блок — над боксом; вертикальный (h > w*1.3) — снизу
        # вверх вдоль левой стенки с поворотом -90° (имитация Rotate angle="-90"
        # сценбилдера).
        label = None
        bg = None
        if text:
            font = QFont("DejaVu Sans")
            font.setPointSizeF(max(0.5, _LABEL_PT * self._ocr_vis_scale()))
            fm = QFontMetricsF(font)
            th = fm.height()
            tw = fm.horizontalAdvance(text)
            if _block_is_vertical(x1, y1, x2, y2):
                # Точка поворота — pos подписи (origin (0,0)): текст от нижнего-
                # левого угла блока идёт вверх, толщина строки — вправо.
                lx, ly = x1 - th - 2, y2
                if lx < 0:
                    lx = x2 + 2   # не влезла слева — вдоль правой стенки
                rot = -90.0
                bl = min(tw + 4, h + 4)
                bg = QGraphicsRectItem(lx - 1, ly - bl + 1, th + 2, bl)
            else:
                lx, ly = x1, y1 - th - 2
                if ly < 0:
                    ly = y2 + 2
                rot = 0.0
                bg = QGraphicsRectItem(lx - 1, ly - 1, min(tw + 4, w + 4), th + 2)
            bg.setPen(QPen(Qt.PenStyle.NoPen))
            bg.setBrush(QBrush(_COLOR_TEXT_BG))
            bg.setZValue(_BLOCK_Z + 1)
            bg.setVisible(visible)
            self.scene.addItem(bg)
            label = QGraphicsSimpleTextItem(text)
            label.setBrush(QBrush(_COLOR_TEXT))
            label.setFont(font)
            label.setPos(lx, ly)
            label.setRotation(rot)
            label.setZValue(_BLOCK_Z + 2)
            label.setVisible(visible)
            self.scene.addItem(label)

        self._ocr_block_items[blk.get("id")] = {
            "rect": rect, "text": label, "bg": bg, "line": line,
        }

    def _ocr_shift_press(self, x: float, y: float):
        """Shift+ЛКМ в ОКР: клик по блоку — toggle выделения; пусто — rubber-band."""
        bid = self._ocr_block_at(x, y)
        if bid is not None:
            if bid in self._selected_ocr:
                self._selected_ocr.discard(bid)
            else:
                self._selected_ocr.add(bid)
            self.refresh_ocr_layer()
            self.update_status(
                f"Выделено блоков: {len(self._selected_ocr)} (Delete — удалить)"
            )
        else:
            self._start_rubber_band(x, y)

    def _ocr_rubber_select(self, x1: float, y1: float, x2: float, y2: float,
                           extend: bool = False):
        """Выделить блоки, пересекающие рамку (Shift-протяжка) в ОКР."""
        rx, ry = min(x1, x2), min(y1, y2)
        ex, ey = max(x1, x2), max(y1, y2)
        if not extend:
            self._selected_ocr.clear()
        for blk in self.model.text_blocks:
            if blk.get("merged_into") is not None:
                continue
            bb = blk.get("bbox")
            if not bb or len(bb) != 4:
                continue
            bx1, by1, bx2, by2 = bb
            if bx1 <= ex and bx2 >= rx and by1 <= ey and by2 >= ry:
                self._selected_ocr.add(blk.get("id"))
        self.refresh_ocr_layer()
        self.update_status(f"Выделено блоков: {len(self._selected_ocr)}")

    def _delete_selected_ocr_blocks(self):
        """Удалить все выделенные блоки — клавиша Delete."""
        if not self._selected_ocr:
            return
        cmd = self._ocr_push_snapshot("Удалить выделенные блоки")
        for bid in list(self._selected_ocr):
            self.model.remove_text_block(bid)
        self._selected_ocr.clear()
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()

    def _clear_ocr_selection(self):
        if self._selected_ocr:
            self._selected_ocr.clear()
            self.refresh_ocr_layer()

    def _apply_ocr_node_colors(self):
        """Раскрасить узлы/рёбра по привязке — как в «бусине» (_redraw_all_colors).

        Центроид оборудования: золото=привязан / синий. bbox оборудования:
        золото=привязан / серый (без заливки). Коннектор: серый. Ребро:
        золото=привязан. Вызывается только в состоянии ОКР, поверх обычного рендера.
        """
        bnodes = {b.get("node_id") for b in self.model.bindings if b.get("node_id")}
        bedges = {str(b.get("edge_key")) for b in self.model.bindings if b.get("edge_key")}
        poly = getattr(self, "polygon_items", {})
        # Рамки ОКР-объектов — на ручке слоя ОКР, а не на общей OUTLINE_WIDTH:
        # решение заказчика 2026-07-28 (всё, что относится к слою ОКР, крутится
        # одним регулятором «толщина рамки текст-боксов»).
        ocr_w = self.OCR_BORDER_W * self._ocr_vis_scale()
        # центроид-маркеры
        for nid, marker in self.node_items.items():
            is_equip = (nid in self.bbox_items) or (nid in poly)
            if not is_equip:
                c = _NODE_CONNECTOR
            else:
                c = _COLOR_BOUND if nid in bnodes else _NODE_CENTROID
            try:
                marker.setBrush(QBrush(c))
                marker.setPen(QPen(c.darker(130), self.OUTLINE_WIDTH))
            except Exception:
                pass
        # bbox оборудования — только рамка
        for nid, rect_item in self.bbox_items.items():
            c = _COLOR_BOUND if nid in bnodes else _NODE_EQUIP_GREY
            try:
                rect_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                rect_item.setPen(QPen(c, ocr_w))
            except Exception:
                pass
        for nid, poly_item in poly.items():
            c = _COLOR_BOUND if nid in bnodes else _NODE_EQUIP_GREY
            try:
                poly_item.setPen(QPen(c, ocr_w))
            except Exception:
                pass
        # рёбра — золото если привязано
        for key, item in self.edge_items.items():
            ek = f"{key[0]}|{key[1]}"
            if ek in bedges:
                try:
                    item.setPen(QPen(_COLOR_BOUND, self.EDGE_WIDTH * 1.25))
                except Exception:
                    pass

    def _binding_target_center(self, binding: dict):
        """Центр цели привязки (x, y) — узел или ребро; None если не найдено."""
        node_id = binding.get("node_id")
        if node_id and node_id in self.nodes:
            c = self.nodes[node_id].get("centroid")
            if c and len(c) == 2:
                return (float(c[1]), float(c[0]))  # centroid = [y, x]
        edge_key = binding.get("edge_key")
        if edge_key and "|" in str(edge_key):
            a, b = str(edge_key).split("|", 1)
            edge_data = self.model.find_edge_data(self.model.edge_key(a, b))
            if edge_data:
                sp = edge_data.get("source_point")
                tp = edge_data.get("target_point")
                if sp and tp:
                    return ((sp[1] + tp[1]) / 2.0, (sp[0] + tp[0]) / 2.0)
        return None

    def _binding_target_bbox(self, binding: dict):
        """bbox цели привязки [x1, y1, x2, y2].

        Узел — реальный bbox оборудования или виртуальный бокс коннектора
        (_get_node_bbox); ребро — вырожденный бокс в midpoint (согласовано
        с _binding_target_center). None — цель не найдена.
        """
        node_id = binding.get("node_id")
        if node_id and node_id in self.nodes:
            return [float(v) for v in self._get_node_bbox(node_id)]
        if binding.get("edge_key"):
            c = self._binding_target_center(binding)
            if c is not None:
                return [c[0], c[1], c[0], c[1]]
        return None

    @staticmethod
    def _nearest_bind_side(target_bbox: list, ref_bbox: list) -> str:
        """Сторона цели для авто-позиции: по «выходам» центра ИСХОДНОГО
        положения текста (ref_bbox) за грани bbox цели. Не зависит от формы
        бокса и от места броска — привязка детерминирована:
          • выход только по одной оси → та сторона;
          • по обеим осям (угловая зона) → большее смещение побеждает;
          • центр внутри бокса → ближайшая изнутри грань;
          • равенство (в т.ч. вырожденная цель) → right.
        """
        tx1, ty1, tx2, ty2 = target_bbox
        cx = (ref_bbox[0] + ref_bbox[2]) / 2.0
        cy = (ref_bbox[1] + ref_bbox[3]) / 2.0
        dx = (cx - tx2) if cx > tx2 else (cx - tx1) if cx < tx1 else 0.0
        dy = (cy - ty2) if cy > ty2 else (cy - ty1) if cy < ty1 else 0.0
        if dx or dy:
            if abs(dx) >= abs(dy):
                return "right" if dx > 0 else "left"
            return "bottom" if dy > 0 else "top"
        side, best = "right", tx2 - cx
        for s, d in (("left", cx - tx1), ("top", cy - ty1), ("bottom", ty2 - cy)):
            if d < best:
                side, best = s, d
        return side

    def _bound_block_bbox(self, binding: dict, w: float, h: float):
        """Производный bbox привязанного блока: у стороны side цели с отступом gap.

        Блок центрируется по стороне, размер (w, h) сохраняется. None — если
        side не задан или цель не найдена (рисуем по blk['bbox'] как раньше).
        """
        side = binding.get("side")
        if side not in ("top", "right", "left", "bottom"):
            return None
        tb = self._binding_target_bbox(binding)
        if tb is None:
            return None
        gap = float(binding.get("gap", _BIND_GAP))
        tcx = (tb[0] + tb[2]) / 2.0
        tcy = (tb[1] + tb[3]) / 2.0
        if side == "top":
            x1, y1 = tcx - w / 2.0, tb[1] - gap - h
        elif side == "bottom":
            x1, y1 = tcx - w / 2.0, tb[3] + gap
        elif side == "left":
            x1, y1 = tb[0] - gap - w, tcy - h / 2.0
        else:  # right
            x1, y1 = tb[2] + gap, tcy - h / 2.0
        return [x1, y1, x1 + w, y1 + h]

    def _attach_binding_position(self, binding: dict, blk: dict,
                                 ref_bbox: list | None = None) -> bool:
        """Момент привязки: выбрать сторону цели, записать side+gap в binding
        и поставить блок по центру этой стороны (размер сохраняется).

        Сторона считается от ИСХОДНОГО положения блока (ref_bbox — bbox,
        снятый в момент захвата), а не от места броска. Без ref_bbox — от
        текущего bbox блока. Returns True, если авто-позиция вычислена.
        """
        bbox = blk.get("bbox")
        if not bbox or len(bbox) != 4:
            return False
        tb = self._binding_target_bbox(binding)
        if tb is None:
            return False
        ref = ref_bbox if (ref_bbox and len(ref_bbox) == 4) else bbox
        binding["side"] = self._nearest_bind_side(tb, [float(v) for v in ref])
        binding["gap"] = _BIND_GAP
        w = max(1.0, float(bbox[2]) - float(bbox[0]))
        h = max(1.0, float(bbox[3]) - float(bbox[1]))
        eff = self._bound_block_bbox(binding, w, h)
        if eff is not None:
            blk["bbox"] = eff
        return True

    # Порядок вращения стороны привязки по часовой стрелке.
    _BIND_SIDE_CW = {"right": "bottom", "bottom": "left", "left": "top",
                     "top": "right"}

    def _rotate_node_bindings(self, node_id: str) -> bool:
        """Повернуть ВСЕ привязки узла на следующую сторону по часовой.

        right → bottom → left → top → right. Привязка без side сначала
        получает сторону от ТЕКУЩЕГО положения блока (правило «выходов»
        _nearest_bind_side), затем поворачивается. Позиция блока производная
        (side+gap) — перерисовка сама переставит блок к новой стороне.
        Один шаг undo на весь узел. Returns True, если было что вращать.
        """
        binds = [b for b in self.model.bindings if b.get("node_id") == node_id]
        if not binds:
            return False
        cmd = self._ocr_push_snapshot("Повернуть привязки")
        for b in binds:
            side = b.get("side")
            if side not in self._BIND_SIDE_CW:
                side = "right"
                blk = self.model.find_text_block(b.get("block_id"))
                tb = self._binding_target_bbox(b)
                bb = blk.get("bbox") if blk else None
                if tb is not None and bb and len(bb) == 4:
                    side = self._nearest_bind_side(tb, [float(v) for v in bb])
            b["side"] = self._BIND_SIDE_CW[side]
            if b.get("gap") is None:
                b["gap"] = _BIND_GAP
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()
        self.update_status(
            f"Привязки узла {node_id} повернуты по часовой ({len(binds)})")
        return True

    def _refresh_ocr_layer_for_node(self, node_id: str):
        """Гранулярно перерисовать блоки, привязанные к узлу/его рёбрам.

        Для живого следования при drag узла: полный refresh_ocr_layer на каждый
        кадр пересоздаёт ВЕСЬ слой (лаги на софт-рендере Astra) — здесь
        пересоздаются item'ы только затронутых блоков. Вне состояния 'ocr'
        слой скрыт — ничего не делаем (производная позиция пересчитается при
        ближайшем refresh_ocr_layer/_redraw_all).
        """
        if getattr(self, "display_regime", "base") != "ocr":
            return
        for b in self.model.bindings:
            if not b.get("side"):
                continue
            hit = b.get("node_id") == node_id
            if not hit:
                ek = b.get("edge_key")
                if ek and "|" in str(ek):
                    hit = node_id in str(ek).split("|", 1)
            if hit and b.get("block_id"):
                self._redraw_single_ocr_block(b.get("block_id"))

    def _refresh_ocr_layer_visibility(self):
        """Показать/скрыть слой под текущее состояние (ocr — видно)."""
        visible = getattr(self, "display_regime", "base") == "ocr"
        for pair in self._ocr_block_items.values():
            for it in pair.values():
                if it is not None:
                    it.setVisible(visible)

    # -----------------------------------------------------------------
    # Hit-test
    # -----------------------------------------------------------------
    def _ocr_block_at(self, x: float, y: float) -> str | None:
        """Найти наименьший блок, содержащий точку (x, y)."""
        best_id = None
        best_area = None
        for blk in self.model.text_blocks:
            if blk.get("merged_into") is not None:
                continue
            bbox = blk.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            if x1 <= x <= x2 and y1 <= y <= y2:
                area = abs((x2 - x1) * (y2 - y1))
                if best_area is None or area < best_area:
                    best_area = area
                    best_id = blk.get("id")
        return best_id

    # -----------------------------------------------------------------
    # Undo helper
    # -----------------------------------------------------------------
    def _ocr_push_snapshot(self, description: str):
        from ui.editors.undo_manager import SnapshotCommand
        cmd = SnapshotCommand(self.model, self._redraw_all)
        cmd.execute()
        cmd.description = description
        return cmd

    def _ocr_commit(self, cmd):
        cmd.finalize()
        self.undo_mgr.push_executed(cmd)

    # -----------------------------------------------------------------
    # Операции
    # -----------------------------------------------------------------
    def add_ocr_block_from_rect(self, x1: float, y1: float, x2: float, y2: float):
        """Создать пустой текст-блок из нарисованной рамки."""
        bx1, bx2 = min(x1, x2), max(x1, x2)
        by1, by2 = min(y1, y2), max(y1, y2)
        _min = _MIN_BLOCK_SIZE * self._ocr_vis_scale()
        if (bx2 - bx1) < _min or (by2 - by1) < _min:
            self.update_status("Слишком маленькая рамка — блок не создан")
            return
        cmd = self._ocr_push_snapshot("Добавить блок")
        blk = self.model.create_text_block([bx1, by1, bx2, by2], text="", source="manual")
        self.model.add_text_block(blk)
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()
        self.update_status("Блок добавлен — нажмите «Распознать добавленные»")

    def _ocr_copy_blocks(self):
        """Ctrl+C в ОКР: приоритет у блока под курсором.

        Блок под курсором вне выделения → выделение переключается на него;
        блок в составе выделения или курсор в пустоте → копируется текущее
        выделение. Скопированное остаётся выделенным (зелёная рамка) —
        видно, что именно в буфере; Esc для смены копируемого не нужен.
        """
        pos = self._cursor_scene_pos()
        cur = self._ocr_block_at(pos.x(), pos.y())
        if cur is not None and cur not in self._selected_ocr:
            self._selected_ocr = {cur}
            self.refresh_ocr_layer()
        clip = []
        for bid in list(self._selected_ocr):
            blk = self.model.find_text_block(bid)
            if not blk or blk.get("merged_into") is not None:
                continue
            bbox = blk.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            clip.append(([float(v) for v in bbox], blk.get("text") or ""))
        if not clip:
            self.update_status("Копировать: выделите блоки или наведите курсор на блок")
            return
        self._ocr_clipboard = clip
        self.update_status(f"Скопировано блоков: {len(clip)} (Ctrl+V — вставить)")

    def _ocr_paste_blocks(self, dx: float, dy: float):
        """Зафиксировать вставку буфера блоков в ОКР со сдвигом (dx, dy).

        Вызывается фиксацией призрака (Ctrl+V → призрак → Ctrl+ЛКМ).
        Копии всегда непривязанные (binding не наследуется). Вся вставка —
        один snapshot, т.е. отменяется одним Ctrl+Z.
        """
        clip = self._ocr_clipboard
        if not clip:
            self.update_status("Буфер блоков пуст — сначала Ctrl+C")
            return
        cmd = self._ocr_push_snapshot("Копировать блоки")
        new_ids = []
        for bbox, text in clip:
            blk = self.model.create_text_block(
                [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy],
                text=text, source="manual")
            # set_binding НЕ вызываем — копия создаётся непривязанной
            self.model.add_text_block(blk)
            new_ids.append(blk["id"])
        self._ocr_commit(cmd)
        self._selected_ocr = set(new_ids)
        self.refresh_ocr_layer()
        self.update_status(f"Вставлено блоков: {len(new_ids)}")

    def _ghost_label_items(self, bbox: list, text: str) -> list:
        """Item'ы призрачной подписи текст-блока (подложка + текст) для
        предпросмотра вставки. Прозрачность даёт группа призрака.

        Геометрия синхронна с _draw_ocr_block: горизонтальный — верх-лево
        над рамкой (fallback вниз), вертикальный (h > w*1.3) — низ-лево
        вдоль левой стенки с поворотом -90° (fallback вдоль правой).
        """
        text = (text or "").strip()
        if not text or not bbox or len(bbox) != 4:
            return []
        x1, y1, x2, y2 = [float(v) for v in bbox]
        font = QFont("DejaVu Sans")
        font.setPointSizeF(max(0.5, _LABEL_PT * self._ocr_vis_scale()))
        fm = QFontMetricsF(font)
        th = fm.height()
        tw = fm.horizontalAdvance(text)
        if _block_is_vertical(x1, y1, x2, y2):
            lx, ly = x1 - th - 2, y2
            if lx < 0:
                lx = x2 + 2
            rot = -90.0
            bl = min(tw + 4, (y2 - y1) + 4)
            bg = QGraphicsRectItem(lx - 1, ly - bl + 1, th + 2, bl)
        else:
            lx, ly = x1, y1 - th - 2
            if ly < 0:
                ly = y2 + 2
            rot = 0.0
            bg = QGraphicsRectItem(lx - 1, ly - 1,
                                   min(tw + 4, (x2 - x1) + 4), th + 2)
        bg.setPen(QPen(Qt.PenStyle.NoPen))
        bg.setBrush(QBrush(_COLOR_TEXT_BG))
        label = QGraphicsSimpleTextItem(text)
        label.setBrush(QBrush(_COLOR_TEXT))
        label.setFont(font)
        label.setPos(lx, ly)
        label.setRotation(rot)
        return [bg, label]

    def move_ocr_block(self, block_id: str, new_x1: float, new_y1: float):
        """Переместить блок (верхний левый угол в new_x1,new_y1)."""
        blk = self.model.find_text_block(block_id)
        if not blk:
            return
        x1, y1, x2, y2 = blk["bbox"]
        w, h = x2 - x1, y2 - y1
        blk["bbox"] = [new_x1, new_y1, new_x1 + w, new_y1 + h]

    def get_pending_ocr_boxes(self):
        """(ids, boxes) для пустых (не распознанных) активных блоков.

        Боксы — в координатах ОРИГИНАЛЬНОГО растра: сервер режет по ним оригинал
        (worker/tasks/ocr.py), а сцена в WYSIWYG-режиме живёт в холсте 1920x1080.
        Разворачиваем тем же преобразованием, которым вписан фон: orig=(canvas−off)/s.
        В legacy-режиме s=1, off=0 → конверсия тождественна.

        Текст возвращается по id, геометрии в ответе нет (см. apply_ocr_results),
        поэтому обратная конверсия не нужна.
        """
        s = getattr(self, "_bg_scale", 1.0) or 1.0
        offx = getattr(self, "_bg_offx", 0.0)
        offy = getattr(self, "_bg_offy", 0.0)
        ids, boxes = [], []
        for blk in self.model.text_blocks:
            if blk.get("merged_into") is not None:
                continue
            if (blk.get("text") or "").strip():
                continue
            bbox = blk.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            ids.append(blk.get("id"))
            boxes.append([
                int(round((x1 - offx) / s)), int(round((y1 - offy) / s)),
                int(round((x2 - offx) / s)), int(round((y2 - offy) / s)),
            ])
        return ids, boxes

    def apply_ocr_results(self, block_ids: list, results: list) -> int:
        """Проставить распознанный текст в блоки по id. Returns кол-во непустых.

        Синхронно обновляет text привязки блока (если есть) — binding['text']
        источник KKS и diameter_text в FXML (как в edit_ocr_block_text).
        """
        cmd = self._ocr_push_snapshot("Распознать блоки")
        n = 0
        for bid, res in zip(block_ids, results):
            blk = self.model.find_text_block(bid)
            if not blk:
                continue
            txt = (res.get("text") or "").strip() if isinstance(res, dict) else ""
            blk["text"] = txt
            binding = self.model.find_binding(bid)
            if binding is not None:
                binding["text"] = txt
            if isinstance(res, dict) and res.get("confidence") is not None:
                blk["confidence"] = float(res.get("confidence") or 0.0)
            if txt:
                n += 1
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()
        return n

    def bind_block_at(self, block_id: str, x: float, y: float,
                      origin: list | None = None) -> bool:
        """Привязать блок к узлу/ребру под точкой (x, y).

        Привязка ставит блок по центру выбранной стороны цели с отступом
        (_BIND_GAP); side+gap пишутся в binding — дальше позиция производная
        и следует за перемещением/resize цели. Сторона считается от ИСХОДНОГО
        положения блока (origin — bbox в момент захвата), место броска не
        влияет: привязка детерминирована. Если авто-позицию вычислить нельзя —
        бокс возвращается на origin, как раньше. Если цели нет — блок
        остаётся на новом месте (перемещение).
        Снимок для undo делает вызывающий (жест целиком) — здесь не снимаем.
        """
        blk = self.model.find_text_block(block_id)
        if not blk:
            return False
        text = (blk.get("text") or "").strip()

        node_id = self.find_node_at(x, y)
        if node_id:
            binding = {
                "block_id": block_id, "node_id": node_id,
                "kind": "node", "text": text,
            }
            if not self._attach_binding_position(binding, blk, origin) \
                    and origin is not None:
                blk["bbox"] = list(origin)      # fallback: цель без геометрии
            self.model.set_binding(binding)
            self.refresh_ocr_layer()
            self._redraw_all()
            self.update_status(f"Блок привязан к узлу {node_id}")
            return True

        edge_key, _pt = self.find_nearest_edge(x, y, threshold=20.0)
        if edge_key:
            binding = {
                "block_id": block_id,
                "edge_key": f"{edge_key[0]}|{edge_key[1]}",
                "kind": "edge", "text": text,
            }
            if not self._attach_binding_position(binding, blk, origin) \
                    and origin is not None:
                blk["bbox"] = list(origin)
            self.model.set_binding(binding)
            self.refresh_ocr_layer()
            self._redraw_all()
            self.update_status("Блок привязан к ребру")
            return True

        # Цели нет — блок остаётся на новом месте (перемещение).
        self.refresh_ocr_layer()
        return False

    def _sync_bindings_to_graph(self):
        """Перенести привязки РЁБЕР в edge.diameter_text — для downstream.

        Узлы (KKS) НЕ синхронизируем в узел: единственный источник истины для
        FXML — graph.bindings (см. graph_to_fxml.build_node_kks_map). В узел
        ничего не пишем.
        Рёбра: edge_key + text -> edge.diameter_text (как в «бусине»).
        Вызывается ПРИ СОХРАНЕНИИ, а не при привязке.
        """
        for b in self.model.bindings:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            ek = b.get("edge_key")
            if ek and "|" in str(ek):
                a, c = str(ek).split("|", 1)
                ed = self.model.find_edge_data(self.model.edge_key(a, c))
                if ed is not None:
                    ed["diameter_text"] = text

    def unbind_ocr_block(self, block_id: str):
        """Убрать привязку блока."""
        if self.model.find_binding(block_id) is None:
            return
        cmd = self._ocr_push_snapshot("Отвязать блок")
        self.model.remove_binding(block_id)
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()
        self._redraw_all()

    def delete_ocr_block(self, block_id: str):
        """Удалить блок и его привязку."""
        cmd = self._ocr_push_snapshot("Удалить блок")
        self.model.remove_text_block(block_id)
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()

    def edit_ocr_block_text(self, block_id: str):
        """Ctrl+2ЛКМ по блоку — редактировать его текст (как в «бусине»).

        Меняет текст блока и синхронно текст его привязки (если есть), чтобы
        kks/диаметр не разошлись с блоком. Правка под Undo.
        """
        from PySide6.QtWidgets import QInputDialog
        blk = self.model.find_text_block(block_id)
        if blk is None:
            return
        current = (blk.get("text") or "")
        new, ok = QInputDialog.getText(
            self, "Редактирование текста", f"Блок {block_id}:", text=current
        )
        if not ok:
            return
        new = (new or "").strip()
        if new == current.strip():
            return
        cmd = self._ocr_push_snapshot("Правка текста блока")
        blk["text"] = new
        binding = self.model.find_binding(block_id)
        if binding is not None:
            binding["text"] = new
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()
        self._redraw_all()
        self.update_status(f"Текст блока: «{new[:40]}»")

    # -----------------------------------------------------------------
    # Изменение размера текст-блока (ручки) — только в состоянии 'ocr'
    # -----------------------------------------------------------------
    def _redraw_single_ocr_block(self, bid: str):
        """Перерисовать один текст-блок (рамка + подпись + подложка + линия)."""
        pair = self._ocr_block_items.pop(bid, None)
        if pair:
            for it in pair.values():
                if it is not None and it.scene() is not None:
                    self.scene.removeItem(it)
        blk = self.model.find_text_block(bid)
        if blk is not None and blk.get("merged_into") is None:
            visible = getattr(self, "display_regime", "base") == "ocr"
            self._draw_ocr_block(blk, visible)

    def _show_ocr_block_resize(self, bid: str):
        """Показать 4 ручки изменения размера для текст-блока bid.

        on_resize пишет новый bbox прямо в blk['bbox'] (только model.text_blocks),
        живо перерисовывая блок. Коммит под Undo — на release (_ocr_resize_commit).
        """
        from ui.editors.resize_overlay import ResizableNodeOverlay
        blk = self.model.find_text_block(bid)
        if blk is None:
            return
        bbox = blk.get("bbox")
        if not bbox or len(bbox) != 4:
            return
        self._hide_ocr_block_resize()
        self._ocr_resize_block_id = bid
        self._ocr_resize_cmd = self._ocr_push_snapshot("Размер блока")

        def _on_resize(new_bbox, _bid=bid):
            b = self.model.find_text_block(_bid)
            if b is not None:
                b["bbox"] = [float(v) for v in new_bbox]
                self._redraw_single_ocr_block(_bid)
                # Привязанный блок «приклеен» к стороне цели: производная
                # позиция могла сдвинуть bbox — вернуть ручки на фактическое
                # положение блока (размер сохранён, позиция у стенки).
                ov = self._ocr_resize_overlay
                if ov is not None and b["bbox"] != [float(v) for v in new_bbox]:
                    ov.set_bbox(b["bbox"])

        self._ocr_resize_overlay = ResizableNodeOverlay(
            scene=self.scene,
            bbox=[float(v) for v in bbox],
            min_size=max(1, int(_MIN_BLOCK_SIZE * self._ocr_vis_scale())),
            on_resize=_on_resize,
            on_commit=lambda: self._ocr_resize_commit(),
        )
        self._ocr_resize_overlay.show()
        self.update_status(
            "Тяните за углы — размер блока · Ctrl+ЛКМ по блоку — скрыть ручки · Esc — выход"
        )

    def _ocr_resize_commit(self):
        """Финализировать одно перетаскивание ручки блока (шаг undo)."""
        cmd = getattr(self, "_ocr_resize_cmd", None)
        if cmd is not None:
            self._ocr_commit(cmd)
            self._ocr_resize_cmd = None
        self.refresh_ocr_layer()
        # Начать новый снимок на случай продолжения перетаскивания той же рамки.
        if self._ocr_resize_overlay is not None and self._ocr_resize_block_id is not None:
            self._ocr_resize_cmd = self._ocr_push_snapshot("Размер блока")

    def _hide_ocr_block_resize(self):
        """Скрыть ручки изменения размера текст-блока (если есть)."""
        # Незакоммиченный (открытый) снимок просто отбрасываем — он не попал в
        # undo-стек, поэтому достаточно снять ссылку (изменений в модели нет).
        self._ocr_resize_cmd = None
        if self._ocr_resize_overlay is not None:
            try:
                self._ocr_resize_overlay.hide()
            except Exception:
                pass
            self._ocr_resize_overlay = None
        self._ocr_resize_block_id = None

    def _toggle_ocr_block_resize(self, bid: str):
        """Переключить ручки размера блока bid.

        Клик по тому же блоку — скрыть; по другому — переставить ручки на него.
        """
        if self._ocr_resize_overlay is not None and self._ocr_resize_block_id == bid:
            self._hide_ocr_block_resize()
        else:
            self._show_ocr_block_resize(bid)

    # -----------------------------------------------------------------
    # Подсветка цели при drag
    # -----------------------------------------------------------------
    def _ocr_clear_highlight(self):
        for item, pen in self._ocr_hl_restore:
            try:
                item.setPen(pen)
            except Exception:
                pass
        self._ocr_hl_restore = []

    def _ocr_highlight_target(self, x: float, y: float):
        self._ocr_clear_highlight()
        node_id = self.find_node_at(x, y)
        if node_id:
            # Подсвечиваем весь контур оборудования (рамка бокса ИЛИ контур
            # полигона) — ровно то, что привяжется; фолбэк — центроид-маркер.
            item = (self.bbox_items.get(node_id)
                    or self.polygon_items.get(node_id)
                    or self.node_items.get(node_id))
            if item is not None:
                self._ocr_hl_restore.append((item, item.pen()))
                item.setPen(QPen(_COLOR_DROP, 3 * self._ocr_vis_scale()))
            return
        edge_key, _pt = self.find_nearest_edge(x, y, threshold=20.0)
        if edge_key and edge_key in self.edge_items:
            item = self.edge_items[edge_key]
            self._ocr_hl_restore.append((item, item.pen()))
            item.setPen(QPen(_COLOR_DROP, 5 * self._ocr_vis_scale()))


# =====================================================================
# ModeHandler'ы
# =====================================================================
class AddOcrBlockHandler(ModeHandler):
    """Ctrl+ЛКМ протяжка по пустому месту — нарисовать рамку нового блока."""

    def on_enter(self, editor):
        editor._ocr_add_start = None
        if editor._ocr_add_preview is not None and editor._ocr_add_preview.scene():
            editor.scene.removeItem(editor._ocr_add_preview)
        editor._ocr_add_preview = None
        editor.update_status("Обведите область текста рамкой (Ctrl+ЛКМ протяжка)")

    def on_exit(self, editor):
        if editor._ocr_add_preview is not None and editor._ocr_add_preview.scene():
            editor.scene.removeItem(editor._ocr_add_preview)
        editor._ocr_add_preview = None
        editor._ocr_add_start = None

    def on_press(self, editor, x, y, event) -> bool:
        editor._ocr_add_start = (x, y)
        rect = QGraphicsRectItem(x, y, 1, 1)
        # _ocr_vis_scale — метод редактора, а не хендлера (self здесь — хендлер).
        rect.setPen(QPen(_COLOR_UNBOUND, 2 * editor._ocr_vis_scale(), Qt.PenStyle.DashLine))
        rect.setZValue(_BLOCK_Z + 5)
        editor.scene.addItem(rect)
        editor._ocr_add_preview = rect
        return True

    def on_move(self, editor, x, y, event) -> bool:
        if editor._ocr_add_start is None or editor._ocr_add_preview is None:
            return False
        x0, y0 = editor._ocr_add_start
        editor._ocr_add_preview.setRect(min(x0, x), min(y0, y), abs(x - x0), abs(y - y0))
        return True

    def on_release(self, editor, x, y, event) -> bool:
        if editor._ocr_add_start is None:
            return False
        x0, y0 = editor._ocr_add_start
        if editor._ocr_add_preview is not None and editor._ocr_add_preview.scene():
            editor.scene.removeItem(editor._ocr_add_preview)
        editor._ocr_add_preview = None
        editor._ocr_add_start = None
        editor.add_ocr_block_from_rect(x0, y0, x, y)
        return True


class OcrBindHandler(ModeHandler):
    """Резидентный режим состояния «ОКР привязка».

    Жесты Ctrl+ЛКМ по текст-блоку:
      • одиночный клик (без смещения) — переключить ручки изменения размера
        блока; при активном выделении клик по НЕвыделенному блоку добавляет
        его в выделение (вместо ручек);
      • протяжка — двигать блок; отпускание над узлом/ребром — привязка,
        иначе просто перемещение.
    Ctrl+ЛКМ клик по узлу с привязками (без блока под курсором) — повернуть
    его привязки на следующую сторону по часовой.
    Если под курсором ручка активного resize-оверлея — приоритет у оверлея
    (тянем угол, а не двигаем/переключаем блок).
    """

    # Порог различия клик/drag для блоков (px).
    OCR_CLICK_MOVE_THRESHOLD = 4.0

    def on_enter(self, editor):
        editor._ocr_drag_id = None
        editor.update_status(
            "Перетащите блок на узел/ребро — привязка. Ctrl+ПКМ по блоку — удалить/отвязать."
        )

    def on_exit(self, editor):
        editor._ocr_drag_id = None
        editor._ocr_rotate_nid = None
        editor._ocr_clear_highlight()

    def on_press(self, editor, x, y, event) -> bool:
        # 1. Ручка активного resize-оверлея имеет приоритет над всем.
        ov = getattr(editor, "_ocr_resize_overlay", None)
        if ov is not None and ov.visible:
            handle = ov.find_handle_at(x, y)
            if handle:
                ov.start_drag(handle)
                editor._ocr_drag_id = None
                editor._ocr_resize_dragging = True
                return True

        bid = editor._ocr_block_at(x, y)
        if bid is None:
            # Клик по пустому месту — скрыть ручки (если были).
            if getattr(editor, "_ocr_resize_overlay", None) is not None:
                editor._hide_ocr_block_resize()
            # Блока нет: узел с привязками под курсором — кандидат на
            # вращение привязок по часовой (клик/drag решается на release).
            nid = editor.find_node_at(x, y)
            if nid is not None and any(
                    b.get("node_id") == nid for b in editor.model.bindings):
                editor._ocr_rotate_nid = nid
                editor._ocr_rotate_xy = (x, y)
                return True
            return False
        blk = editor.model.find_text_block(bid)
        if not blk:
            return False
        # Если для этого блока уже показаны ручки размера — тело двигать нельзя
        # (размер меняем только углами). Клик по телу — скрыть ручки.
        editor._ocr_resize_lock = (
            getattr(editor, "_ocr_resize_overlay", None) is not None
            and editor._ocr_resize_block_id == bid
        )
        x1, y1, _x2, _y2 = blk["bbox"]
        editor._ocr_drag_id = bid
        editor._ocr_drag_dx = x - x1
        editor._ocr_drag_dy = y - y1
        editor._ocr_drag_origin = list(blk["bbox"])   # для возврата при привязке
        # Различаем клик и drag: снимок для перемещения берём лениво — на первом
        # реальном сдвиге (в on_move). Клик (без сдвига) переключает ручки.
        editor._ocr_press_xy = (x, y)
        editor._ocr_moved = False
        editor._ocr_drag_cmd = None
        editor._ocr_resize_dragging = False
        return True

    def on_move(self, editor, x, y, event) -> bool:
        # Перетаскивание ручки resize-оверлея.
        if getattr(editor, "_ocr_resize_dragging", False):
            ov = getattr(editor, "_ocr_resize_overlay", None)
            if ov is not None and ov.is_dragging:
                ov.drag_to(x, y)
                return True
            return False

        # Кандидат на вращение привязок: ждём release (порог проверяется там).
        if getattr(editor, "_ocr_rotate_nid", None) is not None:
            return True

        if editor._ocr_drag_id is None:
            return False
        # Блок с показанными ручками — тело НЕ двигаем (размер меняем углами).
        if getattr(editor, "_ocr_resize_lock", False):
            px, py = getattr(editor, "_ocr_press_xy", (x, y))
            if ((x - px) ** 2 + (y - py) ** 2) ** 0.5 >= self.OCR_CLICK_MOVE_THRESHOLD:
                editor._ocr_moved = True
            return True
        bid = editor._ocr_drag_id
        # Пока смещение меньше порога — считаем жест кликом (ручки не двигаем).
        if not getattr(editor, "_ocr_moved", False):
            px, py = getattr(editor, "_ocr_press_xy", (x, y))
            if ((x - px) ** 2 + (y - py) ** 2) ** 0.5 < self.OCR_CLICK_MOVE_THRESHOLD:
                return True
            editor._ocr_moved = True
            # Начало реального перемещения → снимок для undo.
            editor._ocr_drag_cmd = editor._ocr_push_snapshot(
                "Привязка / перемещение блока"
            )
        nx1 = x - editor._ocr_drag_dx
        ny1 = y - editor._ocr_drag_dy
        editor.move_ocr_block(bid, nx1, ny1)
        # перерисовать этот блок целиком (рамка + подпись + подложка + линия)
        pair = editor._ocr_block_items.pop(bid, None)
        if pair:
            for it in pair.values():
                if it is not None and it.scene() is not None:
                    editor.scene.removeItem(it)
        blk = editor.model.find_text_block(bid)
        if blk:
            editor._draw_ocr_block(blk, True)
        editor._ocr_highlight_target(x, y)
        return True

    def on_release(self, editor, x, y, event) -> bool:
        # Завершение перетаскивания ручки resize-оверлея.
        if getattr(editor, "_ocr_resize_dragging", False):
            editor._ocr_resize_dragging = False
            ov = getattr(editor, "_ocr_resize_overlay", None)
            if ov is not None and ov.is_dragging:
                ov.end_drag()   # вызовет _ocr_resize_commit (шаг undo)
            return True

        # Вращение привязок узла: Ctrl+ЛКМ КЛИК (без сдвига) по узлу с
        # привязками, когда под курсором не было блока.
        nid = getattr(editor, "_ocr_rotate_nid", None)
        if nid is not None:
            editor._ocr_rotate_nid = None
            px, py = getattr(editor, "_ocr_rotate_xy", (x, y))
            if ((x - px) ** 2 + (y - py) ** 2) ** 0.5 < self.OCR_CLICK_MOVE_THRESHOLD:
                editor._rotate_node_bindings(nid)
            return True

        # Блок с показанными ручками: тело не двигали. Клик — скрыть ручки; drag — no-op.
        if getattr(editor, "_ocr_resize_lock", False):
            editor._ocr_resize_lock = False
            _bid = editor._ocr_drag_id
            editor._ocr_drag_id = None
            _moved = getattr(editor, "_ocr_moved", False)
            editor._ocr_moved = False
            if not _moved and _bid is not None:
                editor._toggle_ocr_block_resize(_bid)
            return True

        bid = editor._ocr_drag_id
        if bid is None:
            return False
        editor._ocr_drag_id = None
        editor._ocr_clear_highlight()
        moved = getattr(editor, "_ocr_moved", False)
        editor._ocr_moved = False

        if not moved:
            # Клик без сдвига.
            editor._ocr_drag_cmd = None
            editor._ocr_drag_origin = None
            sel = getattr(editor, "_selected_ocr", None)
            if sel and bid not in sel:
                # Активное выделение: клик добавляет НЕвыделенный блок в
                # выделение (вместо переключения ручек resize).
                sel.add(bid)
                editor.refresh_ocr_layer()
                editor.update_status(
                    f"Выделено блоков: {len(sel)} (Delete — удалить)")
                return True
            # Иначе — прежнее поведение: переключить ручки размера блока.
            editor._toggle_ocr_block_resize(bid)
            return True

        cmd = editor._ocr_drag_cmd
        editor._ocr_drag_cmd = None
        origin = getattr(editor, "_ocr_drag_origin", None)
        editor._ocr_drag_origin = None
        editor.bind_block_at(bid, x, y, origin)
        if cmd is not None:
            editor._ocr_commit(cmd)
        return True
