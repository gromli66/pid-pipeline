"""
Graph Data Model — модель данных графа P&ID.

Единственный source of truth для узлов, рёбер, метаданных.
Без Qt-зависимостей.
"""

import json
import statistics
from copy import deepcopy
from pathlib import Path
from typing import Optional


class GraphDataModel:
    """Модель данных графа P&ID. Без Qt-зависимостей.

    Все мутации данных — только через этот класс.
    Синхронизация graph_data['nodes'] и graph_data['links'] —
    внутри CRUD-методов.
    """

    def __init__(self):
        self.graph_data: dict = {}
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str]] = set()
        self.edges_data: list[dict] = []
        self.coco_annotations: dict[int, dict] = {}
        self.manual_node_counter: int = 0
        self.manual_edge_counter: int = 0
        self._edge_data_index: dict[tuple[str, str], dict] = {}

    # =================================================================
    # I/O
    # =================================================================

    def load(self, graph_path: str, coco_path: str = "") -> bool:
        """Загрузка графа и опционально COCO аннотаций.

        Восстанавливает manual_node_counter / manual_edge_counter
        из ID вида 'node_manual_N' / 'edge_manual_N'.

        Returns:
            True при успехе, False при ошибке.
        """
        # 1. Graph JSON
        try:
            with open(graph_path, 'r', encoding='utf-8') as f:
                self.graph_data = json.load(f)
        except Exception:
            return False

        # 2. COCO annotations (optional)
        self.coco_annotations.clear()
        if coco_path and Path(coco_path).exists():
            try:
                with open(coco_path, 'r', encoding='utf-8') as f:
                    coco_data = json.load(f)
                for ann in coco_data.get('annotations', []):
                    ann_id = ann.get('id')
                    if ann_id is not None:
                        self.coco_annotations[ann_id] = ann
            except Exception:
                pass

        # 3. Parse nodes
        self.nodes.clear()
        self.manual_node_counter = 0
        for node in self.graph_data.get('nodes', []):
            node_id = node.get('id')
            if not node_id:
                continue
            self.nodes[node_id] = node
            if node_id.startswith('node_manual_'):
                try:
                    n = int(node_id.split('_')[-1])
                    self.manual_node_counter = max(self.manual_node_counter, n)
                except ValueError:
                    pass

        # 4. Parse edges (ключ 'links' в формате NetworkX)
        self.edges.clear()
        self.manual_edge_counter = 0
        raw_edges = self.graph_data.get('links', [])

        # Фильтруем рёбра с None source/target
        self.edges_data = [e for e in raw_edges if e.get('source') and e.get('target')]

        for edge in self.edges_data:
            a, b = edge['source'], edge['target']
            self.edges.add(self.edge_key(a, b))
            # Обратная совместимость: гарантируем наличие waypoints
            if 'waypoints' not in edge:
                edge['waypoints'] = []
            # Восстановить manual_edge_counter
            edge_id = edge.get('id', '')
            if edge_id.startswith('edge_manual_'):
                try:
                    n = int(edge_id.split('_')[-1])
                    self.manual_edge_counter = max(self.manual_edge_counter, n)
                except ValueError:
                    pass

        # 5. Build index
        self.rebuild_edge_data_index()

        return True

    def save(self, path: str) -> bool:
        """Сохранить граф в JSON.

        Синхронизирует graph_data['graph'] метаданные:
        - links, num_edges, num_nodes, num_isolated_nodes.
        """
        if not self.graph_data:
            return False

        # Sync
        self.graph_data['links'] = self.edges_data
        self.graph_data['nodes'] = list(self.nodes.values())

        # Метаданные
        graph_meta = self.graph_data.setdefault('graph', {})
        graph_meta['num_edges'] = len(self.edges)
        graph_meta['num_nodes'] = len(self.nodes)

        stats = self.compute_statistics()
        graph_meta['num_isolated_nodes'] = stats['isolated']

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.graph_data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

    # =================================================================
    # Snapshot / Restore
    # =================================================================

    def snapshot(self) -> tuple:
        """Глубокая копия (nodes, edges_data, edges, graph_meta) для undo."""
        return (
            deepcopy(self.nodes),
            deepcopy(self.edges_data),
            deepcopy(self.edges),
            deepcopy(self.graph_data.get('graph', {})),
        )

    def restore(self, snap: tuple):
        """Восстановить из snapshot + перестроить индексы + sync graph_data."""
        if len(snap) == 4:
            self.nodes, self.edges_data, self.edges, graph_meta = snap
            self.graph_data['graph'] = graph_meta
        else:
            # Backward compatibility with old 3-tuple snapshots
            self.nodes, self.edges_data, self.edges = snap
        self.graph_data['nodes'] = list(self.nodes.values())
        self.graph_data['links'] = self.edges_data
        self.rebuild_edge_data_index()

    # =================================================================
    # Ключи и индексы
    # =================================================================

    def edge_key(self, a: str, b: str) -> tuple[str, str]:
        """Создаёт отсортированный ключ для ребра."""
        return (min(a, b), max(a, b))

    def find_edge_data(self, key: tuple[str, str]) -> Optional[dict]:
        """O(1) поиск edge_data по ключу."""
        return self._edge_data_index.get(key)

    def rebuild_edge_data_index(self):
        """Пересоздать индекс edge_key → edge_data."""
        self._edge_data_index.clear()
        for edge in self.edges_data:
            key = self.edge_key(edge['source'], edge['target'])
            self._edge_data_index[key] = edge

    # =================================================================
    # CRUD — Nodes
    # =================================================================

    def add_node(self, node: dict) -> str:
        """Добавить узел. Обновляет nodes + graph_data['nodes'].

        Returns:
            node_id
        """
        node_id = node['id']
        self.nodes[node_id] = node
        nodes_list = self.graph_data.setdefault('nodes', [])
        # Avoid duplicate — node may already be in list after undo/redo
        if node not in nodes_list and not any(n.get('id') == node_id for n in nodes_list):
            nodes_list.append(node)
        return node_id

    def remove_node(self, node_id: str) -> tuple[Optional[dict], list[dict]]:
        """Удалить узел + все связанные рёбра.

        Returns:
            (node_backup, edges_backup) — для undo.
            node_backup = None если узел не найден.
        """
        if node_id not in self.nodes:
            return None, []

        # Найти и удалить связанные рёбра
        edges_backup = []
        connected_keys = [key for key in self.edges if node_id in key]
        for key in connected_keys:
            edge_data = self.find_edge_data(key)
            if edge_data:
                edges_backup.append(edge_data.copy())
            self.edges.discard(key)
            self._edge_data_index.pop(key, None)

        self.edges_data = [
            e for e in self.edges_data
            if self.edge_key(e['source'], e['target']) not in set(connected_keys)
        ]

        # Удалить узел
        node_backup = self.nodes.pop(node_id)
        self.graph_data['nodes'] = [
            n for n in self.graph_data.get('nodes', []) if n.get('id') != node_id
        ]

        return node_backup, edges_backup

    def update_node(self, node_id: str, updates: dict):
        """Частичное обновление узла."""
        if node_id in self.nodes:
            self.nodes[node_id].update(updates)

    # =================================================================
    # CRUD — Edges
    # =================================================================

    def add_edge(self, source: str, target: str, edge_data: dict) -> tuple[str, str]:
        """Добавить ребро.

        Обновляет edges + edges_data + index + graph_data['links'].

        Returns:
            edge_key
        """
        key = self.edge_key(source, target)
        self.edges.add(key)
        self.edges_data.append(edge_data)
        self._edge_data_index[key] = edge_data
        # graph_data['links'] — тот же список self.edges_data
        self.graph_data['links'] = self.edges_data
        return key

    def remove_edge(self, key: tuple[str, str]) -> Optional[dict]:
        """Удалить ребро.

        Returns:
            removed edge_data или None.
        """
        self.edges.discard(key)
        removed = self._edge_data_index.pop(key, None)
        if removed:
            self.edges_data = [
                e for e in self.edges_data
                if self.edge_key(e['source'], e['target']) != key
            ]
            self.graph_data['links'] = self.edges_data
        return removed

    def update_edge(self, key: tuple[str, str], updates: dict):
        """Частичное обновление ребра."""
        edge_data = self.find_edge_data(key)
        if edge_data:
            edge_data.update(updates)

    # =================================================================
    # Запросы
    # =================================================================

    def get_connected_edges(self, node_id: str) -> list[tuple[str, str]]:
        """Все рёбра, связанные с узлом."""
        return [key for key in self.edges if node_id in key]

    def edge_exists(self, a: str, b: str) -> bool:
        """Проверка существования ребра."""
        return self.edge_key(a, b) in self.edges

    def compute_statistics(self) -> dict:
        """Подсчёт статистики графа.

        Returns:
            {'total_nodes': N, 'total_edges': N, 'connected': N, 'isolated': N}
        """
        if not self.nodes:
            return {
                'total_nodes': 0, 'total_edges': 0,
                'connected': 0, 'isolated': 0,
            }

        adjacency: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        for a, b in self.edges:
            if a in adjacency:
                adjacency[a].add(b)
            if b in adjacency:
                adjacency[b].add(a)

        isolated = sum(1 for nid in self.nodes if len(adjacency.get(nid, set())) == 0)
        connected = len(self.nodes) - isolated

        return {
            'total_nodes': len(self.nodes),
            'total_edges': len(self.edges),
            'connected': connected,
            'isolated': isolated,
        }

    # =================================================================
    # Фабрики
    # =================================================================

    def create_connector_node(self, x: float, y: float) -> dict:
        """Создаёт connector-узел. Инкрементирует manual_node_counter.

        Args:
            x, y: координаты в пространстве изображения

        Returns:
            node dict (ещё не добавлен в модель — вызвать add_node()).
        """
        self.manual_node_counter += 1
        return {
            "id": f"node_manual_{self.manual_node_counter}",
            "type": "connector",
            "centroid": [y, x],  # [y, x] формат
            "area": 225,
            "bbox": None,
            "degree": 0,
            "class_id": -1,
            "class_name": "connector",
            "yolo_idx": None,
            "segmentation": None,
            "manual": True,
        }

    def create_equipment_node(self, x: float, y: float,
                              width: float, height: float,
                              class_id: int, class_name: str) -> dict:
        """Создаёт equipment-узел с bbox. Инкрементирует manual_node_counter.

        bbox центрируется вокруг (x, y).

        Returns:
            node dict (ещё не добавлен в модель).
        """
        self.manual_node_counter += 1
        x1, y1 = x - width / 2, y - height / 2
        return {
            "id": f"node_manual_{self.manual_node_counter}",
            "type": "equipment",
            "centroid": [y, x],
            "area": width * height,
            "bbox": [x1, y1, x1 + width, y1 + height],
            "degree": 0,
            "class_id": class_id,
            "class_name": class_name,
            "yolo_idx": None,
            "segmentation": None,
            "manual": True,
        }

    def create_edge_data(self, source: str, target: str,
                         source_point: Optional[list] = None,
                         target_point: Optional[list] = None) -> dict:
        """Создаёт edge_data dict. Инкрементирует manual_edge_counter.

        Returns:
            edge_data dict (ещё не добавлено — вызвать add_edge()).
        """
        self.manual_edge_counter += 1
        return {
            "id": f"edge_manual_{self.manual_edge_counter}",
            "source": source,
            "target": target,
            "source_point": source_point or [0, 0],
            "target_point": target_point or [0, 0],
            "waypoints": [],
            "length": 0,
            "is_terminal": False,
            "color": None,
            "straight_line_distance": 0,
            "manual": True,
        }
