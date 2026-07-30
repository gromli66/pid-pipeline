"""
Advanced Handlers — обработчики режимов для AdvancedGraphEditor.

AddEdgeWithWaypointsHandler, OptimizeEdgeHandler, DragNodeHandler,
MultiSelectHandler, EditWaypointHandler.
"""

from ui.editors.mode_handlers.base_handler import ModeHandler


class AddEdgeWithWaypointsHandler(ModeHandler):
    """Режим добавления рёбер с промежуточными waypoints.

    Клик на узел A → начало.
    Клик на пустое место → добавить waypoint, продолжить.
    Клик на узел B → завершить ребро A→B с накопленными waypoints.
    Клик на тот же узел A → отмена.
    Escape → отмена (через idle).
    """

    def on_press(self, ed, x, y, event):
        clicked = ed.find_node_at(x, y)

        if ed.selected_node is None:
            # Начало: выбрать стартовый узел
            if clicked:
                ed.select_node(clicked)
                ed._pending_waypoints = []
            return True

        if clicked:
            if clicked == ed.selected_node:
                # Клик на себя → отмена
                ed._pending_waypoints = []
                ed._clear_waypoint_preview()
                ed.clear_selection()
            else:
                # Клик на другой узел → завершить ребро
                waypoints = list(ed._pending_waypoints)
                ed._pending_waypoints = []
                ed._clear_waypoint_preview()
                ed.add_edge_with_waypoints(ed.selected_node, clicked, waypoints)
                ed.clear_selection()
        else:
            # Клик на пустое место → waypoint
            ed._pending_waypoints.append([y, x])  # формат [y, x]
            ed._update_waypoint_preview()
            ed.update_status(
                f"Waypoint {len(ed._pending_waypoints)} добавлен. "
                f"Кликните на узел для завершения."
            )

        return True

    def on_move(self, ed, x, y, event):
        hovered = ed.find_node_at(x, y)
        ed.update_hover(hovered)
        if ed.selected_node:
            ed._update_edge_build_preview(x, y)
        return True

    def on_exit(self, ed):
        ed._pending_waypoints = []
        ed._clear_waypoint_preview()


class OptimizeEdgeHandler(ModeHandler):
    """Режим оптимизации: клик на ребро → optimize_edge."""

    def on_press(self, ed, x, y, event):
        edge_key, _ = ed.find_nearest_edge(x, y)
        if edge_key:
            ed.optimize_edge(*edge_key)
        else:
            ed.update_status("Ребро не найдено. Кликните на оранжевое ребро.")
        return True

    def on_move(self, ed, x, y, event):
        ed.update_optimize_preview(x, y)
        return True


class DragNodeHandler(ModeHandler):
    """Режим перетаскивания узлов."""

    def on_press(self, ed, x, y, event):
        clicked = ed.find_node_at(x, y)
        if clicked:
            ed.start_drag_node(clicked)
        else:
            ed.update_status("Узел не найден. Кликните на узел для перетаскивания.")
        return True

    def on_move(self, ed, x, y, event):
        if ed.dragging_node:
            ed.drag_node_to(x, y)
        else:
            ed.update_drag_preview(x, y)
        return True

    def on_release(self, ed, x, y, event):
        if ed.dragging_node:
            ed.end_drag_node()
        return True


class MultiSelectHandler(ModeHandler):
    """Режим множественного выделения: клик toggle / rubber band."""

    def on_press(self, ed, x, y, event):
        clicked = ed.find_node_at(x, y)
        if clicked:
            ed.toggle_select_node(clicked)
        else:
            edge_key, _ = ed.find_nearest_edge(x, y)
            if edge_key:
                ed.toggle_select_edge(edge_key)
            else:
                # Начинаем rubber band selection
                ed._start_rubber_band(x, y)
        return True

    def on_move(self, ed, x, y, event):
        if ed._rb_active:
            ed._update_rubber_band(x, y)
        return True

    def on_release(self, ed, x, y, event):
        if ed._rb_active:
            ed._finish_rubber_band(x, y, event)
        return True


class EditEdgeColorHandler(ModeHandler):
    """Режим изменения ЦВЕТА ребра.

    Ctrl+ЛКМ по ребру → покрасить в текущий цвет палитры.
      • если ребро входит в обводку (shift+протяжка) — красятся все обведённые;
      • иначе — только это ребро.
    Ctrl+ПКМ по обведённому ребру → убрать его из обводки.
    """

    def on_enter(self, ed):
        # Чистый старт: режим работает только с рёбрами
        ed.clear_multi_select()

    def on_press(self, ed, x, y, event):
        edge_key, _ = ed.find_nearest_edge(x, y, threshold=20.0)
        if edge_key:
            ed.apply_edge_style_at(edge_key, kind="color")
        else:
            ed.update_status("Ctrl+ЛКМ по ребру — покрасить. Shift+протяжка — обвести рёбра.")
        return True

    def on_move(self, ed, x, y, event):
        ed.update_edge_style_preview(x, y)
        return True


class EditEdgeSizeHandler(ModeHandler):
    """Режим изменения РАЗМЕРА (толщины) ребра.

    Ctrl+ЛКМ по ребру → присвоить текущий размер (обведённым — всем).
    Ctrl+колесо → изменить текущий размер.
    Ctrl+ПКМ по обведённому ребру → убрать его из обводки.
    """

    def on_enter(self, ed):
        # Чистый старт: режим работает только с рёбрами
        ed.clear_multi_select()

    def on_press(self, ed, x, y, event):
        edge_key, _ = ed.find_nearest_edge(x, y, threshold=20.0)
        if edge_key:
            ed.apply_edge_style_at(edge_key, kind="size")
        else:
            ed.update_status("Ctrl+ЛКМ по ребру — задать размер. Ctrl+колесо — менять размер.")
        return True

    def on_move(self, ed, x, y, event):
        ed.update_edge_style_preview(x, y)
        return True


class EditWaypointHandler(ModeHandler):
    """Режим редактирования waypoints.

    Приоритет on_press:
    1. Waypoint drag
    2. Endpoint drag
    3. Node → cycle sides
    4. Edge segment → add waypoint
    """

    def on_enter(self, ed):
        """Показать маркеры waypoints и endpoints."""
        ed._show_waypoint_markers()
        ed._show_endpoint_markers()

    def on_exit(self, ed):
        """Скрыть маркеры."""
        ed._hide_waypoint_markers()
        ed._hide_endpoint_markers()

    def on_press(self, ed, x, y, event):
        # 1. Waypoint hit?
        wp_hit = ed.find_waypoint_at(x, y)
        if wp_hit:
            ed._start_waypoint_drag(wp_hit)
            return True

        # 2. Endpoint hit?
        ep_hit = ed._find_endpoint_at(x, y)
        if ep_hit:
            ed._start_endpoint_drag(ep_hit)
            return True

        # 3. Node → cycle sides
        clicked = ed.find_node_at(x, y)
        if clicked:
            ed._cycle_node_sides(clicked)
            return True

        # 4. Edge segment → add waypoint
        result = ed.find_nearest_edge_segment(x, y, 15.0)
        if result:
            edge_key, _, seg_idx, _ = result
            if edge_key:
                ed._add_waypoint_on_segment(edge_key, seg_idx, x, y)
        return True

    def on_move(self, ed, x, y, event):
        # Кнопка отпущена — жеста нет. У вьюпорта включён mouse tracking, и без
        # этой проверки потерянный release (alt-tab, модальное окно) оставлял
        # конец ребра приклеенным к курсору: маршрут объявлялся ручным, а
        # waypoints стирались — молча, без действия оператора.
        from PySide6.QtCore import Qt
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            if ed.dragging_waypoint:
                ed._end_waypoint_drag()
            elif ed._dragging_endpoint:
                ed._end_endpoint_drag()
            return True
        if ed.dragging_waypoint:
            ed._drag_waypoint_to(x, y)
        elif ed._dragging_endpoint:
            ed._drag_endpoint_to(x, y)
        return True

    def on_release(self, ed, x, y, event):
        if ed.dragging_waypoint:
            ed._end_waypoint_drag()
        elif ed._dragging_endpoint:
            ed._end_endpoint_drag()
        return True


class ResizeObjectsHandler(ModeHandler):
    """Режим массового изменения размеров объектов одного класса.

    Набор экземпляров правится жестами (перехватываются в редакторе):
      • Ctrl+ЛКМ по экземпляру класса — добавить в набор;
      • Ctrl+ПКМ по экземпляру — убрать из набора;
      • Shift+рамка — добавить все экземпляры класса из рамки.
    Сами контролы (класс, размеры, масштаб) — в левой панели вкладки.
    """

    def on_enter(self, ed):
        ed._enter_resize_objects()

    def on_exit(self, ed):
        ed._exit_resize_objects()

    def on_press(self, ed, x, y, event):
        # Ctrl+ЛКМ по экземпляру класса — добавить в набор.
        # В режиме resize_objects _node_drag_allowed()=False, поэтому клик по
        # узлу приходит сюда (а не в _on_ctrl_lmb_click). По пустому месту
        # _resize_handle_ctrl_click ничего не делает (узел не найден).
        ed._resize_handle_ctrl_click(x, y)
        return True

    def on_move(self, ed, x, y, event):
        return True

    def on_release(self, ed, x, y, event):
        return True
