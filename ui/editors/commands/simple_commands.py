"""
Simple Commands — 7 granular команд для SimpleGraphEditor.

Каждая команда хранит данные для undo/redo без snapshot.
"""

import copy

from ui.editors.undo_manager import Command
from ui.editors.graph_data import GraphDataModel


class AddEdgeCommand(Command):
    """Добавить ребро. execute=add+draw, undo=remove+undraw, redo=execute."""

    def __init__(self, model: GraphDataModel, editor, node_a: str, node_b: str, edge_data: dict):
        self._model = model
        self._editor = editor
        self._node_a = node_a
        self._node_b = node_b
        self._edge_data = edge_data
        self._key = None

    def execute(self):
        self._key = self._model.add_edge(self._node_a, self._node_b, self._edge_data)
        self._editor.create_edge_item(self._key, self._edge_data)
        self._editor.update_node_color(self._node_a)
        self._editor.update_node_color(self._node_b)

    def undo(self):
        self._model.remove_edge(self._key)
        self._editor.remove_edge_item(self._key)
        self._editor.update_node_color(self._node_a)
        self._editor.update_node_color(self._node_b)

    @property
    def description(self):
        return f"Добавить ребро {self._node_a}—{self._node_b}"


class RemoveEdgeCommand(Command):
    """Удалить ребро. execute=remove+undraw, undo=restore+draw."""

    def __init__(self, model: GraphDataModel, editor, node_a: str, node_b: str):
        self._model = model
        self._editor = editor
        self._node_a = node_a
        self._node_b = node_b
        self._key = model.edge_key(node_a, node_b)
        self._removed_data = None
        self._removed_bindings: list[dict] = []

    def execute(self):
        # remove_edge чистит привязки текст-блоков к ребру — сохранить для undo
        self._removed_bindings = [
            b for b in self._model.bindings
            if self._model.binding_edge_key(b) == self._key
        ]
        self._removed_data = self._model.remove_edge(self._key)
        self._editor.remove_edge_item(self._key)
        self._editor.update_node_color(self._node_a)
        self._editor.update_node_color(self._node_b)

    def undo(self):
        if self._removed_data:
            self._model.add_edge(self._node_a, self._node_b, self._removed_data)
            self._editor.create_edge_item(self._key, self._removed_data)
            self._editor.update_node_color(self._node_a)
            self._editor.update_node_color(self._node_b)
            for b in self._removed_bindings:
                self._model.set_binding(b)

    @property
    def description(self):
        return f"Удалить ребро {self._node_a}—{self._node_b}"


class AddConnectorOnEdgeCommand(Command):
    """Разбить ребро на два, вставив коннектор.

    Granular: сохраняет созданные ID для корректного redo.
    """

    def __init__(self, model: GraphDataModel, editor,
                 edge_key: tuple, pos_x: float, pos_y: float,
                 seg_idx: int, old_edge_data: dict):
        self._model = model
        self._editor = editor
        self._edge_key = edge_key
        self._pos_x = pos_x
        self._pos_y = pos_y
        self._seg_idx = seg_idx
        self._old_edge_data = old_edge_data

        # Заполняются при первом execute, переиспользуются при redo
        self._created_node_id: str | None = None
        self._created_node: dict | None = None
        self._edge1_data: dict | None = None
        self._edge2_data: dict | None = None
        self._edge1_key: tuple | None = None
        self._edge2_key: tuple | None = None
        self._removed_bindings: list[dict] = []

    def execute(self):
        actual_source = self._old_edge_data['source']
        actual_target = self._old_edge_data['target']
        old_waypoints = self._old_edge_data.get('waypoints', [])

        # Привязки текст-блоков к разбиваемому ребру слетают — сохранить для undo
        self._removed_bindings = [
            b for b in self._model.bindings
            if self._model.binding_edge_key(b) == self._edge_key
        ]

        # Удаляем старое ребро из модели
        self._model.remove_edge(self._edge_key)
        self._editor.remove_edge_item(self._edge_key)

        if self._created_node_id is None:
            # Первый execute — генерируем ID
            self._created_node = self._model.create_connector_node(self._pos_x, self._pos_y)
            self._created_node_id = self._created_node['id']

            # Split waypoints
            seg_idx = max(0, self._seg_idx)
            edge1_wp = old_waypoints[:seg_idx]
            edge2_wp = old_waypoints[seg_idx:]

            self._edge1_data = self._model.create_edge_data(
                actual_source, self._created_node_id,
                self._old_edge_data.get('source_point'),
                [self._pos_y, self._pos_x],
            )
            self._edge1_data['waypoints'] = edge1_wp

            self._edge2_data = self._model.create_edge_data(
                self._created_node_id, actual_target,
                [self._pos_y, self._pos_x],
                self._old_edge_data.get('target_point'),
            )
            self._edge2_data['waypoints'] = edge2_wp

        # Добавляем коннектор
        self._model.add_node(self._created_node)

        # Добавляем два новых ребра
        self._edge1_key = self._model.add_edge(
            self._edge1_data['source'], self._edge1_data['target'], self._edge1_data)
        self._edge2_key = self._model.add_edge(
            self._edge2_data['source'], self._edge2_data['target'], self._edge2_data)

        # Рисуем
        self._editor._draw_single_node(self._created_node_id)
        self._editor.create_edge_item(self._edge1_key, self._edge1_data)
        self._editor.create_edge_item(self._edge2_key, self._edge2_data)

    def undo(self):
        # Удаляем два новых ребра
        if self._edge1_key:
            self._model.remove_edge(self._edge1_key)
            self._editor.remove_edge_item(self._edge1_key)
        if self._edge2_key:
            self._model.remove_edge(self._edge2_key)
            self._editor.remove_edge_item(self._edge2_key)

        # Удаляем коннектор
        if self._created_node_id:
            self._model.remove_node(self._created_node_id)
            self._editor.remove_node_items(self._created_node_id)

        # Восстанавливаем исходное ребро
        self._model.add_edge(self._old_edge_data['source'],
                             self._old_edge_data['target'],
                             self._old_edge_data)
        self._editor.create_edge_item(self._edge_key, self._old_edge_data)
        for b in self._removed_bindings:
            self._model.set_binding(b)

    def redo(self):
        # Переиспользуем сохранённые ID и данные
        self.execute()

    @property
    def description(self):
        return f"Коннектор на ребре {self._edge_key}"


class AddConnectorIsolatedCommand(Command):
    """Добавить изолированный коннектор."""

    def __init__(self, model: GraphDataModel, editor, node: dict):
        self._model = model
        self._editor = editor
        self._node = node
        self._node_id = node['id']

    def execute(self):
        self._model.add_node(self._node)
        self._editor._draw_single_node(self._node_id)

    def undo(self):
        self._model.remove_node(self._node_id)
        self._editor.remove_node_items(self._node_id)

    @property
    def description(self):
        return f"Изолированный коннектор {self._node_id}"


class DeleteNodeCommand(Command):
    """Удалить узел + каскадные рёбра."""

    def __init__(self, model: GraphDataModel, editor, node_id: str):
        self._model = model
        self._editor = editor
        self._node_id = node_id
        self._node_backup: dict | None = None
        self._edges_backup: list[dict] = []
        self._edge_keys: list[tuple] = []
        self._neighbors: list[str] = []
        self._removed_bindings: list[dict] = []

    def execute(self):
        # Запомнить соседей для обновления цветов
        self._edge_keys = self._model.get_connected_edges(self._node_id)
        # remove_node чистит привязки текст-блоков к узлу и его рёбрам —
        # сохранить для undo
        edge_keys = set(self._edge_keys)
        self._removed_bindings = [
            b for b in self._model.bindings
            if b.get('node_id') == self._node_id
            or self._model.binding_edge_key(b) in edge_keys
        ]
        self._neighbors = []
        for key in self._edge_keys:
            other = key[0] if key[1] == self._node_id else key[1]
            self._neighbors.append(other)

        # Удалить визуальные элементы рёбер ДО удаления из модели
        for key in self._edge_keys:
            self._editor.remove_edge_item(key)

        # Удалить из модели (каскадно удалит рёбра)
        self._node_backup, self._edges_backup = self._model.remove_node(self._node_id)

        # Удалить визуальные элементы узла
        self._editor.remove_node_items(self._node_id)

        # Обновить цвета соседей
        for neighbor in self._neighbors:
            if neighbor in self._model.nodes:
                self._editor.update_node_color(neighbor)

    def undo(self):
        if not self._node_backup:
            return

        # Восстановить узел
        self._model.add_node(self._node_backup)
        self._editor._draw_single_node(self._node_id)

        # Восстановить рёбра
        for edge_data in self._edges_backup:
            key = self._model.add_edge(edge_data['source'], edge_data['target'], edge_data)
            self._editor.create_edge_item(key, edge_data)

        # Восстановить привязки текст-блоков
        for b in self._removed_bindings:
            self._model.set_binding(b)

        # Обновить цвета
        self._editor.update_node_color(self._node_id)
        for neighbor in self._neighbors:
            if neighbor in self._model.nodes:
                self._editor.update_node_color(neighbor)

    @property
    def description(self):
        return f"Удалить узел {self._node_id}"


class AddEquipmentNodeCommand(Command):
    """Добавить equipment-узел из списка классов."""

    def __init__(self, model: GraphDataModel, editor, node: dict):
        self._model = model
        self._editor = editor
        self._node = node
        self._node_id = node['id']

    def execute(self):
        self._model.add_node(self._node)
        self._editor._draw_single_node(self._node_id)

    def undo(self):
        self._model.remove_node(self._node_id)
        self._editor.remove_node_items(self._node_id)

    @property
    def description(self):
        return f"Добавить оборудование {self._node.get('class_name', self._node_id)}"


class ResizeNodeCommand(Command):
    """Изменение размера bbox узла.

    Э5: пины концов инцидентных рёбер (edge['pin_source'|'pin_target'] —
    локальные смещения от центроида) масштабируются вместе с рамкой
    (`port_model.rescale_edge_pins` в `_on_node_resized`) — undo обязан
    вернуть и их, иначе после отката пин остаётся отмасштабированным при
    старой рамке и вылетает за границу узла. Снимок пинов:
    {(edge_key, role): {'dx','dy'} | None}.

    1.7: тем же жестом пересаживаются КОНЦЫ инцидентных рёбер
    (`_reseat_after_resize`; в «Ручной правке» — движком, вместе с коленями).
    Пины этого не покрывают: пин — смещение от центроида, а на сервер уходят
    `source_point`/`target_point`/`waypoints`. Без их снимка Ctrl+Z возвращал
    рамку и оставлял маршруты в состоянии «после ресайза» (замер 1.7: ребро
    `node_2 -> node_3`, source_point [13, 1547] -> [27.26, 1577.79], не
    возвращался). Снимок маршрутов: {edge_key: {поле: значение}}, поля —
    `ROUTE_KEYS`; отсутствующее в снимке поле при откате УДАЛЯЕТСЯ (иначе
    `_src_side`/`_tgt_side`, заведённые ресайзом, переживают undo).
    """

    #: Поля ребра, которые переписывает пересадка концов после resize
    #: (замер 1.7: сравнение полных словарей рёбер до/после жеста).
    ROUTE_KEYS = ('source_point', 'target_point', 'waypoints',
                  '_src_side', '_tgt_side')

    def __init__(self, model: GraphDataModel, editor, node_id: str,
                 old_bbox: list, old_centroid: list, old_area: float,
                 new_bbox: list, new_centroid: list, new_area: float,
                 old_pins: dict | None = None, new_pins: dict | None = None,
                 old_routes: dict | None = None, new_routes: dict | None = None):
        self._model = model
        self._editor = editor
        self._node_id = node_id
        self._old_bbox = old_bbox
        self._old_centroid = old_centroid
        self._old_area = old_area
        self._new_bbox = new_bbox
        self._new_centroid = new_centroid
        self._new_area = new_area
        self._old_pins = old_pins
        self._new_pins = new_pins
        self._old_routes = old_routes
        self._new_routes = new_routes

    def _apply_routes(self, routes):
        """1.7: вернуть концы/колени инцидентных рёбер к снятому состоянию."""
        if not routes:
            return
        for key, fields in routes.items():
            edge_data = self._model.find_edge_data(key)
            if edge_data is None:
                continue
            for field in self.ROUTE_KEYS:
                if field in fields:
                    edge_data[field] = copy.deepcopy(fields[field])
                else:
                    edge_data.pop(field, None)

    def _apply_pins(self, pins):
        if not pins:
            return
        from ui.editors import port_model
        for (key, role), pin in pins.items():
            edge_data = self._model.find_edge_data(key)
            if edge_data is None:
                continue
            if pin is None:
                port_model.clear_edge_pin(edge_data, role)
            else:
                edge_data[port_model.PIN_KEYS[role]] = dict(pin)

    def execute(self):
        self._model.update_node(self._node_id, {
            'bbox': self._new_bbox,
            'centroid': self._new_centroid,
            'area': self._new_area,
        })
        self._apply_pins(self._new_pins)
        self._apply_routes(self._new_routes)
        self._editor._redraw_all()

    def undo(self):
        self._model.update_node(self._node_id, {
            'bbox': self._old_bbox,
            'centroid': self._old_centroid,
            'area': self._old_area,
        })
        self._apply_pins(self._old_pins)
        self._apply_routes(self._old_routes)
        self._editor._redraw_all()

    @property
    def description(self):
        return f"Resize {self._node_id}"
