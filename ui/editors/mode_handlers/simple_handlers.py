"""
Simple Handlers — обработчики режимов для SimpleGraphEditor.

IdleHandler, AddEdgeHandler, DeleteEdgeHandler, AddConnectorHandler,
DeleteNodeHandler, AddNodeFromListHandler, ResizeNodeHandler.
"""

from ui.editors.mode_handlers.base_handler import ModeHandler


class IdleHandler(ModeHandler):
    """Нейтральный режим — никаких действий по клику, только hover."""

    def on_press(self, ed, x, y, event):
        ed.clear_selection()
        return True

    def on_move(self, ed, x, y, event):
        hovered = ed.find_node_at(x, y)
        ed.update_hover(hovered)
        return True


class AddEdgeHandler(ModeHandler):
    """Режим добавления рёбер: клик на узел A, клик на узел B → add_edge."""

    def on_press(self, ed, x, y, event):
        clicked = ed.find_node_at(x, y)
        if clicked:
            if ed.selected_node is None:
                ed.select_node(clicked)
            elif clicked != ed.selected_node:
                ed.add_edge(ed.selected_node, clicked)
                ed.clear_selection()
            else:
                ed.clear_selection()
        else:
            ed.clear_selection()
        return True

    def on_move(self, ed, x, y, event):
        hovered = ed.find_node_at(x, y)
        ed.update_hover(hovered)
        if ed.selected_node:
            ed.update_preview_line(x, y)
        return True


class DeleteEdgeHandler(ModeHandler):
    """Режим удаления рёбер: клик на узел A, клик на узел B → remove_edge."""

    def on_press(self, ed, x, y, event):
        clicked = ed.find_node_at(x, y)
        if clicked:
            if ed.selected_node is None:
                ed.select_node(clicked)
            elif clicked != ed.selected_node:
                ed.remove_edge(ed.selected_node, clicked)
                ed.clear_selection()
            else:
                ed.clear_selection()
        else:
            ed.clear_selection()
        return True

    def on_move(self, ed, x, y, event):
        hovered = ed.find_node_at(x, y)
        ed.update_hover(hovered)
        if ed.selected_node:
            ed.update_preview_line(x, y)
        return True


class AddConnectorHandler(ModeHandler):
    """Режим добавления коннектора: на ребро → split, на пустоту → isolated."""

    def on_press(self, ed, x, y, event):
        if ed.hovered_edge:
            _, proj = ed.find_nearest_edge(x, y)
            if proj:
                ed.add_connector_on_edge(ed.hovered_edge, proj[0], proj[1])
        else:
            ed.add_connector_isolated(x, y)
        ed.clear_selection()
        return True

    def on_move(self, ed, x, y, event):
        ed.update_connector_preview(x, y)
        return True


class DeleteNodeHandler(ModeHandler):
    """Режим удаления узла: клик на узел → delete_node."""

    def on_press(self, ed, x, y, event):
        clicked = ed.find_node_at(x, y)
        if clicked:
            ed.delete_node(clicked)
        else:
            ed.update_status(f"Узел не найден рядом с ({x:.0f}, {y:.0f})")
        return True

    def on_move(self, ed, x, y, event):
        hovered = ed.find_node_at(x, y)
        ed.update_hover(hovered)
        return True


class AddNodeFromListHandler(ModeHandler):
    """Режим добавления equipment из списка: Ctrl+ЛКМ протяжка → рамка узла.

    Поведение как в сегментации (Вал. pipe): обводим рамку, на отпускании
    создаётся узел; слишком маленькая рамка — отмена. Класс остаётся выбранным,
    поэтому можно нарисовать несколько узлов подряд.
    """

    def on_press(self, ed, x, y, event):
        cls = getattr(ed, '_pending_node_class', None)
        if not cls:
            ed.update_status("Сначала выберите класс оборудования")
            return True
        ed.start_node_bbox(x, y)
        return True

    def on_move(self, ed, x, y, event):
        ed.update_node_bbox(x, y)
        return True

    def on_release(self, ed, x, y, event):
        ed.finish_node_bbox(x, y)
        return True

    def on_exit(self, ed):
        # Отменить незавершённое рисование рамки при смене режима
        if hasattr(ed, "_cancel_node_bbox"):
            ed._cancel_node_bbox()


class ResizeNodeHandler(ModeHandler):
    """Режим изменения размера equipment-узла.

    Активируется двойным Ctrl+Click на equipment (без кнопки в toolbar).
    Ctrl+Click на handle → drag для изменения размера.
    Ctrl+Click на другой equipment → переключить overlay на него.
    Ctrl+Click на текущий узел (не handle) → игнорировать.
    Ctrl+Click мимо / Escape → вернуться в предыдущий режим.
    """

    def on_press(self, ed, x, y, event):
        overlay = ed._resize_overlay

        # 1. Drag handle
        if overlay and overlay.visible:
            handle = overlay.find_handle_at(x, y)
            if handle:
                overlay.start_drag(handle)
                return True

        # 2. Клик на узел
        clicked = ed.find_node_at(x, y)
        if clicked:
            # Клик на тот же узел — игнорировать (не пересоздавать overlay)
            if clicked == ed._resizing_node:
                return True
            # Клик на другой equipment → переключить overlay
            node = ed.nodes.get(clicked)
            if node and node.get('type') == 'equipment' and node.get('bbox'):
                ed._stop_resize()
                ed._enter_resize_mode(clicked)
                return True

        # 3. Клик мимо → завершить resize, вернуться в предыдущий режим
        ed._stop_resize()
        return True

    def on_move(self, ed, x, y, event):
        overlay = ed._resize_overlay
        if overlay and overlay.is_dragging:
            overlay.drag_to(x, y)
            return True
        # Hover — подсветка узлов
        hovered = ed.find_node_at(x, y)
        ed.update_hover(hovered)
        return True

    def on_release(self, ed, x, y, event):
        overlay = ed._resize_overlay
        if overlay and overlay.is_dragging:
            overlay.end_drag()
            return True
        return False

    def on_exit(self, ed):
        """При выходе из режима — убрать overlay."""
        if ed._resize_overlay:
            ed._resize_overlay.hide()
            ed._resize_overlay = None
