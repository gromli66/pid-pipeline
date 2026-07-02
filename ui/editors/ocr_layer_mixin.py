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
_BORDER_W = 2.0


class OcrLayerMixin:
    """Слой OCR текст-блоков и привязок. Подмешивается в AdvancedGraphEditor.

    Не переопределяет события мыши базового редактора — вся интерактивность
    идёт через ModeHandler'ы (AddOcrBlockHandler, OcrBindHandler).
    """

    # -----------------------------------------------------------------
    # Инициализация / состояние
    # -----------------------------------------------------------------
    def _init_ocr_layer(self):
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
        x1, y1, x2, y2 = [float(v) for v in bbox]
        w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)

        binding = self.model.find_binding(blk.get("id"))
        bound = binding is not None
        text = (blk.get("text") or "").strip()

        # Линия привязки блок → цель — золотой пунктир (как в «бусине»).
        line = None
        if bound:
            tgt = self._binding_target_center(binding)
            if tgt is not None:
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                line = QGraphicsLineItem(cx, cy, tgt[0], tgt[1])
                pen = QPen(_COLOR_BOUND, 1.5)
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
        rect.setPen(QPen(border, _BORDER_W))
        rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect.setZValue(_BLOCK_Z)
        rect.setVisible(visible)
        self.scene.addItem(rect)

        # Подпись — НАД боксом с тёмной подложкой (унифицировано с «бусиной»).
        label = None
        bg = None
        if text:
            font = QFont("DejaVu Sans", _LABEL_PT)
            fm = QFontMetricsF(font)
            th = fm.height()
            tw = fm.horizontalAdvance(text)
            lx, ly = x1, y1 - th - 2
            if ly < 0:
                ly = y2 + 2
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
                f"Выделено блоков: {len(self._selected_ocr)} (Ctrl+ПКМ — удалить)"
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
        """Удалить все выделенные (Shift) блоки — Ctrl+ПКМ по выделенному."""
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
        # центроид-маркеры
        for nid, marker in self.node_items.items():
            is_equip = (nid in self.bbox_items) or (nid in poly)
            if not is_equip:
                c = _NODE_CONNECTOR
            else:
                c = _COLOR_BOUND if nid in bnodes else _NODE_CENTROID
            try:
                marker.setBrush(QBrush(c))
                marker.setPen(QPen(c.darker(130), 1.5))
            except Exception:
                pass
        # bbox оборудования — только рамка
        for nid, rect_item in self.bbox_items.items():
            c = _COLOR_BOUND if nid in bnodes else _NODE_EQUIP_GREY
            try:
                rect_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                rect_item.setPen(QPen(c, 2.0))
            except Exception:
                pass
        for nid, poly_item in poly.items():
            c = _COLOR_BOUND if nid in bnodes else _NODE_EQUIP_GREY
            try:
                poly_item.setPen(QPen(c, 2.0))
            except Exception:
                pass
        # рёбра — золото если привязано
        for key, item in self.edge_items.items():
            ek = f"{key[0]}|{key[1]}"
            if ek in bedges:
                try:
                    item.setPen(QPen(_COLOR_BOUND, 5))
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
        if (bx2 - bx1) < _MIN_BLOCK_SIZE or (by2 - by1) < _MIN_BLOCK_SIZE:
            self.update_status("Слишком маленькая рамка — блок не создан")
            return
        cmd = self._ocr_push_snapshot("Добавить блок")
        blk = self.model.create_text_block([bx1, by1, bx2, by2], text="", source="manual")
        self.model.add_text_block(blk)
        self._ocr_commit(cmd)
        self.refresh_ocr_layer()
        self.update_status("Блок добавлен — нажмите «Распознать добавленные»")

    def move_ocr_block(self, block_id: str, new_x1: float, new_y1: float):
        """Переместить блок (верхний левый угол в new_x1,new_y1)."""
        blk = self.model.find_text_block(block_id)
        if not blk:
            return
        x1, y1, x2, y2 = blk["bbox"]
        w, h = x2 - x1, y2 - y1
        blk["bbox"] = [new_x1, new_y1, new_x1 + w, new_y1 + h]

    def get_pending_ocr_boxes(self):
        """(ids, boxes) для пустых (не распознанных) активных блоков."""
        ids, boxes = [], []
        for blk in self.model.text_blocks:
            if blk.get("merged_into") is not None:
                continue
            if (blk.get("text") or "").strip():
                continue
            bbox = blk.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            ids.append(blk.get("id"))
            boxes.append([int(v) for v in bbox])
        return ids, boxes

    def apply_ocr_results(self, block_ids: list, results: list) -> int:
        """Проставить распознанный текст в блоки по id. Returns кол-во непустых."""
        cmd = self._ocr_push_snapshot("Распознать блоки")
        n = 0
        for bid, res in zip(block_ids, results):
            blk = self.model.find_text_block(bid)
            if not blk:
                continue
            txt = (res.get("text") or "").strip() if isinstance(res, dict) else ""
            blk["text"] = txt
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

        Привязка НЕ перемещает блок: при попадании на узел/ребро бокс
        возвращается на исходное место (origin) — как `_restore_ocr_pos` в
        «бусине». Если цели нет — блок остаётся на новом месте (перемещение).
        Снимок для undo делает вызывающий (жест целиком) — здесь не снимаем.
        """
        blk = self.model.find_text_block(block_id)
        if not blk:
            return False
        text = (blk.get("text") or "").strip()

        node_id = self.find_node_at(x, y)
        if node_id:
            if origin is not None:
                blk["bbox"] = list(origin)      # привязка ≠ перемещение
            self.model.set_binding({
                "block_id": block_id, "node_id": node_id,
                "kind": "node", "text": text,
            })
            self.refresh_ocr_layer()
            self._redraw_all()
            self.update_status(f"Блок привязан к узлу {node_id}")
            return True

        edge_key, _pt = self.find_nearest_edge(x, y, threshold=20.0)
        if edge_key:
            if origin is not None:
                blk["bbox"] = list(origin)
            self.model.set_binding({
                "block_id": block_id,
                "edge_key": f"{edge_key[0]}|{edge_key[1]}",
                "kind": "edge", "text": text,
            })
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
            item = self.bbox_items.get(node_id) or self.node_items.get(node_id)
            if item is not None:
                self._ocr_hl_restore.append((item, item.pen()))
                item.setPen(QPen(_COLOR_DROP, 3))
            return
        edge_key, _pt = self.find_nearest_edge(x, y, threshold=20.0)
        if edge_key and edge_key in self.edge_items:
            item = self.edge_items[edge_key]
            self._ocr_hl_restore.append((item, item.pen()))
            item.setPen(QPen(_COLOR_DROP, 5))


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
        rect.setPen(QPen(_COLOR_UNBOUND, 2, Qt.PenStyle.DashLine))
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

    Ctrl+ЛКМ по блоку и протяжка — двигать блок; отпускание над узлом/ребром —
    привязка, иначе просто перемещение.
    """

    def on_enter(self, editor):
        editor._ocr_drag_id = None
        editor.update_status(
            "Перетащите блок на узел/ребро — привязка. Ctrl+ПКМ по блоку — удалить/отвязать."
        )

    def on_exit(self, editor):
        editor._ocr_drag_id = None
        editor._ocr_clear_highlight()

    def on_press(self, editor, x, y, event) -> bool:
        bid = editor._ocr_block_at(x, y)
        if bid is None:
            return False
        blk = editor.model.find_text_block(bid)
        if not blk:
            return False
        x1, y1, _x2, _y2 = blk["bbox"]
        editor._ocr_drag_id = bid
        editor._ocr_drag_dx = x - x1
        editor._ocr_drag_dy = y - y1
        editor._ocr_drag_origin = list(blk["bbox"])   # для возврата при привязке
        editor._ocr_drag_cmd = editor._ocr_push_snapshot("Привязка / перемещение блока")
        return True

    def on_move(self, editor, x, y, event) -> bool:
        if editor._ocr_drag_id is None:
            return False
        bid = editor._ocr_drag_id
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
        bid = editor._ocr_drag_id
        if bid is None:
            return False
        editor._ocr_drag_id = None
        editor._ocr_clear_highlight()
        cmd = editor._ocr_drag_cmd
        editor._ocr_drag_cmd = None
        origin = getattr(editor, "_ocr_drag_origin", None)
        editor._ocr_drag_origin = None
        editor.bind_block_at(bid, x, y, origin)
        if cmd is not None:
            editor._ocr_commit(cmd)
        return True
