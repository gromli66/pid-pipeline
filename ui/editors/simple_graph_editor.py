"""
Simple Graph Editor — CRUD операции над графом P&ID.

Режимы: add_edge, delete_edge, add_connector, delete_node, add_node_from_list.
Рёбра: point-to-point (без L-route).
"""

import math
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen, QBrush

from ui.editors.base_graph_editor import BaseGraphEditor
from ui.editors.mode_handlers.simple_handlers import (
    IdleHandler, AddEdgeHandler, DeleteEdgeHandler, AddConnectorHandler,
    DeleteNodeHandler, AddNodeFromListHandler, ResizeNodeHandler,
)
from ui.editors.commands.simple_commands import (
    AddEdgeCommand, RemoveEdgeCommand, AddConnectorOnEdgeCommand,
    AddConnectorIsolatedCommand, DeleteNodeCommand,
    AddEquipmentNodeCommand, ResizeNodeCommand,
)
from ui.editors.graph_geometry import (
    bboxes_overlap, dispatch_connect, node_shape_is_polygon,
)
from ui.editors.resize_overlay import ResizableNodeOverlay


class SimpleGraphEditor(BaseGraphEditor):
    """Простой редактор: CRUD операции над графом.

    Потомок: AdvancedGraphEditor.
    """

    def __init__(self):
        super().__init__()

        # Регистрация режимов
        self.register_mode("idle", IdleHandler())
        self.register_mode("add_edge", AddEdgeHandler())
        self.register_mode("delete_edge", DeleteEdgeHandler())
        self.register_mode("add_connector", AddConnectorHandler())
        self.register_mode("delete_node", DeleteNodeHandler())
        self.register_mode("add_node_from_list", AddNodeFromListHandler())
        self.register_mode("resize_node", ResizeNodeHandler())

        # Resize state
        self._resize_overlay = None  # ResizableNodeOverlay | None
        self._resizing_node: str | None = None
        self._mode_before_resize: str = "idle"

        # Pending equipment class for AddNodeFromList
        self._pending_node_class: dict | None = None

        # Рисование bbox узла (как в сегментации): протяжка Ctrl+ЛКМ
        self._node_bbox_start: tuple | None = None
        self._node_bbox_preview = None  # QGraphicsRectItem

        # Default mode
        self.set_mode("idle")

    # =================================================================
    # CRUD — Edges (point-to-point)
    # =================================================================

    def add_edge(self, node_a: str, node_b: str) -> bool:
        """Добавить ребро — ПРОСТОЕ point-to-point соединение.

        Advanced переопределяет для полного L-route.
        """
        if node_a == node_b:
            self.update_status("Нельзя соединить узел с самим собой")
            return False

        if self.model.edge_exists(node_a, node_b):
            self.update_status(f"Ребро уже существует: {node_a} — {node_b}")
            return False

        # Посадка «как в Контурах»: строгая ось важнее центра (решение
        # Максима 2026-08-25). Прежняя цепочка get_connection_point теряла
        # ось — конец садился на проекцию партнёра и кламп в угол.
        src_point, tgt_point = self._seat_pair_dispatch(node_a, node_b)

        edge_data = self.model.create_edge_data(
            node_a, node_b,
            source_point=src_point,
            target_point=tgt_point,
        )
        edge_data['straight_line_distance'] = math.sqrt(
            (tgt_point[1] - src_point[1]) ** 2
            + (tgt_point[0] - src_point[0]) ** 2
        )

        cmd = AddEdgeCommand(self.model, self, node_a, node_b, edge_data)
        self.undo_mgr.execute(cmd)
        self.update_statistics()
        self.update_status(f"Добавлено ребро: {node_a} — {node_b}")
        return True

    def _seat_pair_dispatch(self, node_a: str, node_b: str) -> tuple[list, list]:
        """Посадка пары «как в Контурах» для «Проверки схемы» (2026-08-25).

        Возвращает ([y, x], [y, x]) для node_a и node_b В ТОМ ПОРЯДКЕ, в
        котором они переданы (у ребра это source и target).

        Три вещи, которых нет внутри `dispatch_connect` и быть не должно:

        1. **Нормализация пары.** Ветвление диспетчера спрашивает про
           источник раньше, чем про цель, поэтому `dispatch(s,t)` и
           `dispatch(t,s)` расходятся примерно на 45 % рёбер. Здесь пара
           приводится к детерминированному ключу — тому же, что у
           `GraphDataModel.edge_key` (min, max), — и роли раскладываются
           обратно ПО ЭТОМУ КЛЮЧУ, а не по «кого ресайзили»: в
           `_reseat_after_resize` тянуть могут любой из двух.
        2. **Лифт letterbox (Г5в).** Конец из нутра рамки скинового узла
           выталкивается на неё вдоль луча посадки — то же, что делает
           `get_connection_point`. ⚠ Только концам из BBOX-веток: у узла с
           контуром посадка идёт на контур, и лифт вытолкнул бы конец
           с нарисованной формы на рамку. Ориентир (`toward`) — ВТОРОЙ конец
           пары, а не центроид партнёра: луч осевой, ось сохраняется.
           ⚠ Замер §P4.5: на полном корпусе лифт не сдвинул НИ ОДНОГО из
           17 430 концов bbox-веток (5 065 из них скиновые). Он и не должен: все
           bbox-ветки `connect_*` отдают точку УЖЕ на грани, а `seat_rect`
           у редактора — весь bbox. Дефект bc41b77 приходил от КАНОНА
           (letterbox внутри рамки), которого на этом пути больше нет.
           Оставлен страховкой на случай ветки, отдающей точку внутри
           рамки, — сторожа у него нет и быть не может, пока он ничего
           не меняет. Снимать — вместе с каноном, отдельным решением.
        3. **Вырожденная пара (Г5а).** У пересекающихся рамок
           `connect_bbox_bbox` отдаёт пару ЦЕНТРОИДОВ
           (`graph_geometry.py:225`, тип "overlapping"). В «Контурах» ветка
           недостижима, в «Проверке схемы» дала бы конец в середине узла —
           до 89 px мимо формы. Такая пара сажается каноном.

        Контракт ошибок: диспетчер битую геометрию не прячет, а у нового
        ребра старых точек нет — поэтому отказ ловится здесь и пара садится
        каноном, а не тихим [0, 0] из `create_edge_data`. ⚠ Одного `except`
        для этого мало: битые данные умеют не бросать вовсе, а тихо вернуть
        бессмысленную точку (контур из шести нулей проходит мерку
        `node_shape_is_polygon` и сажает конец в [0, 0]). Такие пары ловит
        `_pair_is_degenerate` ДО вызова диспетчера — вход, а не исход.
        """
        first, second = self.model.edge_key(node_a, node_b)
        n1, n2 = self.nodes[first], self.nodes[second]

        try:
            if self._pair_is_degenerate(first, second, n1, n2):
                seats = self._seat_pair_canonical(first, second)
            else:
                p1, p2 = dispatch_connect(
                    n1, n2, connector_radius=self.CONNECTOR_MARKER_RADIUS)
                # (x, y) диспетчера → [y, x] графа; лифт тоже в [y, x].
                s1, s2 = [p1[1], p1[0]], [p2[1], p2[0]]
                if not node_shape_is_polygon(n1):
                    s1 = self._lift_to_seat_rect(n1, s1, p2[0], p2[1])
                if not node_shape_is_polygon(n2):
                    s2 = self._lift_to_seat_rect(n2, s2, p1[0], p1[1])
                seats = (s1, s2)
        except (TypeError, KeyError, IndexError, ValueError,
                ZeroDivisionError) as exc:
            # Битая геометрия узла: нет центроида, короткий контур, вырожденная
            # рамка. Шире не ловим — «Контуры» могут себе позволить общий
            # `except` (у них есть старые точки), у нового ребра их нет.
            import logging
            logging.getLogger(__name__).debug(
                "Посадка пары %s-%s диспетчером не удалась (%s) — "
                "садим каноном", first, second, exc)
            seats = self._seat_pair_canonical(first, second)

        return seats if node_a == first else (seats[1], seats[0])

    def _pair_is_degenerate(self, id_a: str, id_b: str,
                            node_a: dict, node_b: dict) -> bool:
        """Пара, которой диспетчер не может дать осмысленный ответ.

        Два случая, оба ведут на канонную посадку:

        1. **Г5а** — обе рамки (ни коннектора, ни контура) и перекрытие по
           ОБЕИМ осям: условие вырожденной ветки `connect_bbox_bbox`
           повторено буквально, там она отдаёт пару ЦЕНТРОИДОВ.
        2. **Контур мимо своей рамки** — у узла есть `segmentation` по мерке
           диспетчера, но лежит он ВНЕ собственного bbox. Тогда полигонная
           ветка честно вернёт точку на этом контуре, исключения не будет, и
           `except` ниже не сработает: ребро получило бы тихий [0, 0] (мерка
           `node_shape_is_polygon` — шесть значений, а `[0,0,0,0,0,0]` их
           даёт). Замер: на корпусе таких узлов 0 из 47 с контуром, то есть
           вход закрыт бесплатно; без этой ветки докстрока `_seat_pair_dispatch`
           обещала бы то, чего код не делает.
        """
        for n in (node_a, node_b):
            if self._contour_misses_own_bbox(n):
                return True
        for n in (node_a, node_b):
            if n.get('type', 'connector') == 'connector' or node_shape_is_polygon(n):
                return False
        return bboxes_overlap(self._get_node_bbox(id_a),
                              self._get_node_bbox(id_b))

    @staticmethod
    def _contour_misses_own_bbox(node: dict) -> bool:
        """Контур узла не пересекается с его собственной рамкой?

        Допуск не нужен: речь не о шуме в пиксель (у корпусных контуров
        вершины выходят за рамку на доли пикселя — это законно), а о контуре
        в совершенно другом месте листа.
        """
        if not node_shape_is_polygon(node):
            return False
        bb = node.get('bbox')
        if not bb or len(bb) != 4:
            return False
        seg = node['segmentation']
        xs, ys = seg[0::2], seg[1::2]
        return not bboxes_overlap([min(xs), min(ys), max(xs), max(ys)], bb)

    def _seat_pair_canonical(self, id_a: str, id_b: str) -> tuple[list, list]:
        """Канонная посадка пары — ЦЕПОЧКОЙ, как прежний `add_edge`.

        Точка на втором узле считается от центроида первого, а точка на
        первом — от УЖЕ ПОСЧИТАННОЙ точки на втором. От порядка кликов
        результат не зависит: пара сюда приходит уже нормализованной
        (`_seat_pair_dispatch`), поэтому «первый» определён детерминированно.

        ⚠ Была симметричная редакция (оба конца от центроидов) — она читалась
        аккуратнее, но КЛАМПИЛА конец в угол чаще прежнего кода: на корпусе
        концов в углу 7 против 3 у цепочки (замер §P4-rev-fix.2, 33 вырожденные
        пары). Клампить в угол — ровно тот дефект, который пункт 4.4.1 и
        лечит, поэтому фолбэк держит качество прежнего пути, а не красоту.
        """
        ca = self.nodes[id_a]['centroid']
        bx, by = self.get_connection_point(id_b, ca[1], ca[0])
        ax, ay = self.get_connection_point(id_a, bx, by)
        return ([ay, ax], [by, bx])

    def remove_edge(self, node_a: str, node_b: str) -> bool:
        """Удалить ребро между узлами."""
        key = self.model.edge_key(node_a, node_b)
        if key not in self.edges:
            self.update_status(f"Ребро не существует: {node_a} — {node_b}")
            return False

        cmd = RemoveEdgeCommand(self.model, self, node_a, node_b)
        self.undo_mgr.execute(cmd)
        self.update_statistics()
        self.update_status(f"Удалено ребро: {node_a} — {node_b}")
        return True

    # =================================================================
    # CRUD — Connectors
    # =================================================================

    def add_connector_on_edge(self, edge_key: tuple, pos_x: float, pos_y: float) -> Optional[str]:
        """Добавить коннектор на ребро, разбив его на два.

        Returns:
            ID нового коннектора или None.
        """
        if edge_key not in self.edges:
            return None

        # Находим segment index
        _, _, seg_idx, _ = self.find_nearest_edge_segment(pos_x, pos_y)

        # Получаем edge_data
        old_edge_data = self.model.find_edge_data(edge_key)
        if not old_edge_data:
            return None
        old_edge_data_copy = old_edge_data.copy()
        old_edge_data_copy['waypoints'] = [wp.copy() for wp in old_edge_data.get('waypoints', [])]
        if old_edge_data.get('source_point'):
            old_edge_data_copy['source_point'] = old_edge_data['source_point'].copy()
        if old_edge_data.get('target_point'):
            old_edge_data_copy['target_point'] = old_edge_data['target_point'].copy()

        cmd = AddConnectorOnEdgeCommand(
            self.model, self, edge_key, pos_x, pos_y, seg_idx, old_edge_data_copy
        )
        self.undo_mgr.execute(cmd)
        self.update_statistics()

        node_id = cmd._created_node_id
        self.update_status(f"Создан коннектор {node_id} на ребре")
        return node_id

    def add_connector_isolated(self, pos_x: float, pos_y: float) -> str:
        """Добавить изолированный коннектор."""
        node = self.model.create_connector_node(pos_x, pos_y)
        cmd = AddConnectorIsolatedCommand(self.model, self, node)
        self.undo_mgr.execute(cmd)
        self.update_statistics()
        self.update_status(f"Создан изолированный коннектор {node['id']}")
        return node['id']

    # =================================================================
    # CRUD — Nodes
    # =================================================================

    def delete_node(self, node_id: str) -> bool:
        """Удалить узел и все его рёбра."""
        if node_id not in self.nodes:
            self.update_status(f"Узел не найден: {node_id}")
            return False

        cmd = DeleteNodeCommand(self.model, self, node_id)
        self.undo_mgr.execute(cmd)
        self.update_statistics()
        self.update_status(f"Удалён узел {node_id}")
        return True

    # =================================================================
    # Add equipment from list
    # =================================================================

    def set_pending_node_class(self, cls: dict):
        """Установить класс оборудования для следующего клика."""
        self._pending_node_class = cls

    def add_equipment_node(self, x: float, y: float,
                           class_id: int, class_name: str,
                           width: float = 40, height: float = 40,
                           enter_resize: bool = True) -> str:
        """Добавить equipment-узел.

        enter_resize=False — размер уже задан (узел нарисован рамкой), шаг с
        угловыми ручками пропускается.
        """
        node = self.model.create_equipment_node(x, y, width, height, class_id, class_name)
        cmd = AddEquipmentNodeCommand(self.model, self, node)
        self.undo_mgr.execute(cmd)
        self.update_statistics()
        self.update_status(f"Добавлено оборудование: {class_name}")
        if enter_resize:
            # Переключиться в resize для задания размера
            self._enter_resize_mode(node['id'])
        return node['id']

    # =================================================================
    # Добавление узла рамкой (как в сегментации Вал. pipe)
    # =================================================================

    NODE_BBOX_MIN_PX = 5  # как в PolylineMaskEditor: меньше — отмена

    def start_node_bbox(self, x: float, y: float):
        """Начать рисование рамки нового узла (Ctrl+ЛКМ нажат)."""
        if not self._pending_node_class:
            self.update_status("Сначала выберите класс оборудования")
            return
        self._node_bbox_start = (x, y)
        pen = QPen(QColor(0, 200, 0, 220))
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        brush = QBrush(QColor(0, 200, 0, 40))
        self._node_bbox_preview = self.scene.addRect(x, y, 0, 0, pen, brush)
        self._node_bbox_preview.setZValue(50)

    def update_node_bbox(self, x: float, y: float):
        """Обновить превью рамки во время протяжки."""
        if not self._node_bbox_preview or not self._node_bbox_start:
            return
        sx, sy = self._node_bbox_start
        self._node_bbox_preview.setRect(min(sx, x), min(sy, y), abs(x - sx), abs(y - sy))

    def finish_node_bbox(self, x: float, y: float):
        """Завершить рисование рамки → создать узел (или отменить, если мелко)."""
        if self._node_bbox_start is None:
            return
        cls = self._pending_node_class
        sx, sy = self._node_bbox_start
        self._cancel_node_bbox()  # убрать превью + сбросить старт

        if not cls:
            return

        w, h = abs(x - sx), abs(y - sy)
        if w < self.NODE_BBOX_MIN_PX or h < self.NODE_BBOX_MIN_PX:
            self.update_status("Слишком маленький bbox — отменено")
            return

        x1, y1 = min(sx, x), min(sy, y)
        cx, cy = x1 + w / 2.0, y1 + h / 2.0
        self.add_equipment_node(
            cx, cy, cls['id'], cls['name'],
            width=w, height=h, enter_resize=False,
        )
        # класс остаётся выбранным — можно рисовать ещё узлы того же класса

    def _cancel_node_bbox(self):
        """Убрать превью рамки и сбросить состояние рисования."""
        if self._node_bbox_preview is not None:
            self.scene.removeItem(self._node_bbox_preview)
            self._node_bbox_preview = None
        self._node_bbox_start = None

    # =================================================================
    # Resize — 4 corner handles для equipment-узлов
    # =================================================================

    def _node_drag_allowed(self) -> bool:
        """В «Проверке схемы» Ctrl+ЛКМ по узлу НИКОГДА не начинает тягу.

        Перемещения узлов на этой вкладке не существует как жеста: базовые
        `_start_ctrl_drag` / `_update_ctrl_drag` / `_end_ctrl_drag` —
        заглушки `pass` (`base_graph_editor.py:1760-1771`), реализация только
        в «Ручной правке». А базовый диспетчер, увидев узел под курсором и
        разрешённую тягу, уводит нажатие в ОТЛОЖЕННОЕ решение «клик или тяга»
        (`base_graph_editor.py:1716-1726`) и отдаёт инструменту синтетический
        клик `event=None` уже на отпускании. Такой клик не открывает тягу за
        ручку размера (правило 1.8) — то есть разрешённая, но не
        реализованная тяга ВОРОВАЛА нажатия у единственного жеста, которому
        они нужны.

        Отсюда доступность ручек 90.4 % вместо потолка (замер §P4.6). Первая
        редакция пункта 4.5 лечила следствие — уводила ручки на 22 px наружу,
        из-под порога клика; на зуме этот сдвиг разлетался, ручки повисали
        далеко от рамки (приёмка глазами Максима 2026-08-25). Решение Максима:
        чинить корень — вернуть ручки на углы и закрыть тягу здесь.
        ⚠ Это РАЗВОРОТ буквы пункта («диспетчер нажатия не переписывать»);
        сам диспетчер не тронут, переопределён его виртуальный крючок.
        """
        return False

    def _enter_resize_mode(self, node_id: str):
        """Переключиться в resize_node и показать overlay."""
        # Вход из самого resize_node (повторный двойной клик, добавление узла
        # поверх открытых ручек) не имеет права стать «предыдущим режимом»:
        # иначе выход возвращает в resize_node, и инструмент теряется навсегда.
        if self._current_mode != "resize_node":
            self._mode_before_resize = self._current_mode or "add_edge"
        self.set_mode("resize_node")
        self._start_resize(node_id)

    def _start_resize(self, node_id: str):
        """Показать 4 resize handles для узла."""
        if self._resize_overlay:
            self._resize_overlay.hide()
            self._resize_overlay = None

        node = self.nodes.get(node_id)
        if not node or not node.get('bbox'):
            return

        bbox = node['bbox']
        if not bbox or len(bbox) != 4:
            return

        self._resizing_node = node_id

        # Сохраняем начальное состояние для undo
        self._resize_start_bbox = bbox.copy()
        self._resize_start_centroid = node['centroid'].copy()
        self._resize_start_area = node.get('area', 0)
        # Э5: пины концов инцидентных рёбер масштабируются вместе с рамкой —
        # undo обязан вернуть и их (иначе пин вылетает за границу узла).
        self._resize_start_pins = self._snapshot_edge_pins(node_id)
        # 1.7: концы/колени инцидентных рёбер переписывает `_reseat_after_resize`
        # (в «Ручной правке» — движком) — undo обязан вернуть и их.
        self._resize_start_routes = self._snapshot_edge_routes(node_id)

        self._resize_overlay = ResizableNodeOverlay(
            scene=self.scene,
            bbox=bbox,
            min_size=15,
            on_resize=lambda new_bbox: self._on_node_resized(node_id, new_bbox),
            on_commit=lambda: self._commit_resize(node_id),
        )
        self._resize_overlay.show()
        self.update_status(f"Resize: {node_id} — тяните за углы, Escape для отмены")

    def _on_node_resized(self, node_id: str, new_bbox: list):
        """Callback от overlay — обновить визуалы + пересчитать рёбра."""
        node = self.nodes.get(node_id)
        if not node:
            return

        old_bbox = list(node.get('bbox') or [])
        # Обновляем данные
        node['bbox'] = new_bbox.copy()
        x1, y1, x2, y2 = new_bbox
        node['centroid'] = [(y1 + y2) / 2, (x1 + x2) / 2]
        node['area'] = (x2 - x1) * (y2 - y1)

        # Э5: пины концов инцидентных рёбер — локальные смещения от
        # центроида; при resize масштабируются вместе с рамкой.
        if len(old_bbox) == 4:
            from ui.editors import port_model
            port_model.rescale_edge_pins(node, self.edges_data,
                                         old_bbox, new_bbox)

        # Обновляем bbox rect если есть
        if node_id in self.bbox_items:
            self.bbox_items[node_id].setRect(x1, y1, x2 - x1, y2 - y1)

        # Обновляем маркер
        if node_id in self.node_items:
            cx, cy = node['centroid'][1], node['centroid'][0]
            r = self.EQUIPMENT_MARKER_RADIUS
            self.node_items[node_id].setRect(cx - r, cy - r, r * 2, r * 2)

        # Пересчитать связанные рёбра (advanced-редактор переопределяет:
        # движок, только ближний конец)
        self._reseat_after_resize(node_id)

    def _snapshot_edge_pins(self, node_id: str) -> dict:
        """Э5: снимок пинов концов инцидентных рёбер для undo resize —
        {(edge_key, role): {'dx','dy'} | None}."""
        from ui.editors import port_model
        snap = {}
        for e in self.edges_data:
            for role in ('source', 'target'):
                if e.get(role) != node_id:
                    continue
                key = self.model.edge_key(e['source'], e['target'])
                pin = port_model.edge_pin(e, role)
                snap[(key, role)] = dict(pin) if pin else None
        return snap

    def _snapshot_edge_routes(self, node_id: str) -> dict:
        """1.7: снимок маршрутов инцидентных рёбер для undo resize —
        {edge_key: {поле: значение}} по `ResizeNodeCommand.ROUTE_KEYS`.
        Отсутствующее поле в снимок не попадает — при откате оно удаляется."""
        import copy

        snap = {}
        for e in self.edges_data:
            if node_id not in (e.get('source'), e.get('target')):
                continue
            key = self.model.edge_key(e['source'], e['target'])
            snap[key] = {f: copy.deepcopy(e[f])
                         for f in ResizeNodeCommand.ROUTE_KEYS if f in e}
        return snap

    def _reseat_after_resize(self, node_id: str):
        """База: пересадка концов рёбер узла после resize (point-to-point).

        ВНИМАНИЕ: переписывает ОБА конца — легаси-контракт простого
        редактора; вход с ПИНОМ (Э5) держится в пине. «Ручная правка»
        (AdvancedGraphEditor) переопределяет: движок, только ближний конец
        (Э2d).

        С 2026-08-25 пара сажается диспетчером «Контуров»
        (`_seat_pair_dispatch`), а не парой get_connection_point по
        центроидам: ресайз выводит партнёра из створа, и канон клампил конец
        в угол вместо того, чтобы удержать ось."""
        from ui.editors import port_model
        for edge in self.edges_data:
            if edge['source'] == node_id or edge['target'] == node_id:
                other_id = edge['target'] if edge['source'] == node_id else edge['source']
                if self.nodes.get(other_id) is None:
                    continue
                seat_s, seat_t = self._seat_pair_dispatch(edge['source'],
                                                          edge['target'])
                ps = port_model.pinned_port(self.nodes.get(edge['source']), edge)
                pt = port_model.pinned_port(self.nodes.get(edge['target']), edge)
                edge['source_point'] = [ps[1], ps[0]] if ps else seat_s
                edge['target_point'] = [pt[1], pt[0]] if pt else seat_t
                key = self.model.edge_key(edge['source'], edge['target'])
                self._update_edge_path(key)

    def _commit_resize(self, node_id: str):
        """Финализировать resize — записать в undo."""
        if not self._resizing_node or not hasattr(self, '_resize_start_bbox'):
            return

        node = self.nodes.get(node_id)
        if not node:
            return

        new_bbox = node['bbox'].copy()
        new_centroid = node['centroid'].copy()
        new_area = node.get('area', 0)

        # Только если реально изменилось
        if new_bbox != self._resize_start_bbox:
            cmd = ResizeNodeCommand(
                self.model, self, node_id,
                old_bbox=self._resize_start_bbox,
                old_centroid=self._resize_start_centroid,
                old_area=self._resize_start_area,
                new_bbox=new_bbox,
                new_centroid=new_centroid,
                new_area=new_area,
                old_pins=getattr(self, '_resize_start_pins', None),
                new_pins=self._snapshot_edge_pins(node_id),
                old_routes=getattr(self, '_resize_start_routes', None),
                new_routes=self._snapshot_edge_routes(node_id),
            )
            self.undo_mgr.push_executed(cmd)
            self.update_status(f"Resize: {node_id} → {int(new_bbox[2]-new_bbox[0])}×{int(new_bbox[3]-new_bbox[1])}")

        # Обновить start для следующего drag (если пользователь продолжит)
        self._resize_start_bbox = new_bbox
        self._resize_start_centroid = new_centroid
        self._resize_start_area = new_area
        self._resize_start_pins = self._snapshot_edge_pins(node_id)
        self._resize_start_routes = self._snapshot_edge_routes(node_id)

    def _revert_uncommitted_resize(self):
        """1.8: снять брошенную тягу — вернуть узел и инцидентные рёбра к
        последнему снимку (вход в режим / последний `_commit_resize`).

        Esc посреди протяжки прячет ручки, но модель уже мутирована каждым
        кадром (`_on_node_resized`: bbox/centroid/area, пины, пересадка
        концов) — без отката статусная строка врёт про отмену, а порча
        остаётся без шага undo и с чистым дёрти-флагом. Откат — тем же
        снимком и тем же кодом, что у Ctrl+Z (`ResizeNodeCommand.undo`:
        пять полей `ROUTE_KEYS`, заведённые ресайзом поля удаляются);
        в стек undo НИЧЕГО не пишется — у отменённого жеста нет шага."""
        node_id = self._resizing_node
        start_bbox = getattr(self, '_resize_start_bbox', None)
        if not node_id or not start_bbox:
            return
        node = self.nodes.get(node_id)
        if not node:
            return  # узел снесён undo/redo — откатывать нечего
        cur_pins = self._snapshot_edge_pins(node_id)
        cur_routes = self._snapshot_edge_routes(node_id)
        if (list(node.get('bbox') or []) == list(start_bbox)
                and cur_pins == getattr(self, '_resize_start_pins', None)
                and cur_routes == getattr(self, '_resize_start_routes', None)):
            return  # тяги не было: последний commit уже синхронизировал снимок
        cmd = ResizeNodeCommand(
            self.model, self, node_id,
            old_bbox=list(start_bbox),
            old_centroid=list(self._resize_start_centroid),
            old_area=self._resize_start_area,
            new_bbox=list(node['bbox']),
            new_centroid=list(node['centroid']),
            new_area=node.get('area', 0),
            old_pins=getattr(self, '_resize_start_pins', None),
            new_pins=cur_pins,
            old_routes=getattr(self, '_resize_start_routes', None),
            new_routes=cur_routes,
        )
        cmd.undo()
        # Штатное событие, не сбой: оператор увидит, почему рамка «вернулась».
        import logging
        logging.getLogger(__name__).info(
            "resize: брошенная тяга снята — %s возвращён к последнему "
            "зафиксированному размеру", node_id)

    def _resize_resync_overlay(self):
        """1.8: после undo/redo пересобрать режим правки размера на том, что
        стало моделью — снимок и ручки.

        Ctrl+Z, нажатый ВНУТРИ режима, честно откатывает модель, но снимок
        `_resize_start_*` (три точки 1.7) остаётся от прежнего состояния,
        и `_revert_uncommitted_resize` принимает расхождение, созданное undo,
        за брошенную тягу: следующий же выход из режима возвращает то, что
        оператор отменил и видел откаченным, — при пустом стеке, то есть без
        шага отмены (дефект шва, ревизия связки 1.6+1.7+1.8). Пересъём и есть
        лекарство: состояние после undo/redo и ЕСТЬ последнее зафиксированное.
        Ручки переставляются той же правкой: команда зовёт `_redraw_all`
        (не только своя — `DragNodeCommand` тоже) и сносит их со сцены, а
        overlay остаётся с ПРЕЖНИМ bbox — тяга за такую ручку поехала бы
        от отменённого размера."""
        node_id = self._resizing_node
        if not node_id:
            return
        if node_id not in self.nodes:
            self._stop_resize()  # узел снесён undo/redo — закрыть режим
            return
        self._start_resize(node_id)

    def _stop_resize(self):
        """Убрать resize handles и вернуться в предыдущий режим."""
        # Сначала снять ручки, потом откат: `_redraw_all` внутри отката сносит
        # со сцены всё с Z > 0, и hide() после него удалял бы осиротевшие item.
        if self._resize_overlay:
            self._resize_overlay.hide()
            self._resize_overlay = None
        self._revert_uncommitted_resize()
        self._resizing_node = None
        self._resize_start_bbox = None
        self._resize_start_centroid = None
        self._resize_start_area = None
        self._resize_start_pins = None
        self._resize_start_routes = None
        # Вернуться в предыдущий режим
        if self._current_mode == "resize_node":
            self.set_mode(self._mode_before_resize)

    def _ctrl_right_click_delete(self, x: float, y: float):
        """Ctrl+ПКМ — удалить узел или ребро под курсором."""
        # Приоритет: узел > ребро
        node_id = self.find_node_at(x, y)
        if node_id:
            self.delete_node(node_id)
            return
        edge_key, _ = self.find_nearest_edge(x, y)
        if edge_key:
            self.remove_edge(edge_key[0], edge_key[1])
            return
        self.update_status("Нет узла или ребра под курсором")

    # =================================================================
    # Events
    # =================================================================

    def undo(self):
        super().undo()
        self._resize_resync_overlay()

    def redo(self):
        super().redo()
        self._resize_resync_overlay()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self._current_mode == "resize_node":
                self._stop_resize()
                return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Двойной Ctrl+Click на equipment → переключиться в resize_node."""
        if self.ctrl_pressed and event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            x, y = pos.x(), pos.y()
            clicked = self.find_node_at(x, y)
            if clicked:
                node = self.nodes.get(clicked)
                if node and node.get('type') == 'equipment' and node.get('bbox'):
                    # Форма узла = контур → рамка ему не хозяйка: ручки правят
                    # bbox и центроид, а segmentation остаётся на месте.
                    if self._node_has_polygon(node):
                        self.update_status(
                            f"{clicked}: форма задана контуром — размер рамкой не меняется")
                        return
                    self._enter_resize_mode(clicked)
                    return
        super().mouseDoubleClickEvent(event)
