"""
Экспорт графа P&ID в JSON формат для NetworkX.

ИЗМЕНЕНИЯ v2:
- Добавлены source_point и target_point в рёбра
- source_point = точка контакта на границе source узла [y, x]
- target_point = точка контакта на границе target узла [y, x]
"""
import json
import numpy as np
from typing import List, Dict, Optional


def export_graph_to_json(nodes: List[Dict],
                         edges: List[Dict],
                         output_path: str,
                         format: str = 'node-link',
                         include_paths: bool = False,
                         metadata: Optional[Dict] = None):
    """
    Экспортировать граф в JSON формат.

    Args:
        nodes: Список узлов
        edges: Список рёбер
        output_path: Путь для сохранения JSON
        format: Формат экспорта ('node-link', 'adjacency', 'cytoscape')
        include_paths: Включать ли полные пути рёбер
        metadata: Дополнительные метаданные графа
    """
    if format == 'node-link':
        data = export_node_link_format(nodes, edges, include_paths, metadata)
    elif format == 'adjacency':
        data = export_adjacency_format(nodes, edges, include_paths, metadata)
    elif format == 'cytoscape':
        data = export_cytoscape_format(nodes, edges, include_paths, metadata)
    else:
        raise ValueError(f"Неизвестный формат: {format}")

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, cls=NumpyEncoder)

    print(f"Граф экспортирован в JSON: {output_path}")
    print(f"  Формат: {format}")
    print(f"  Узлов: {len(nodes)}")
    print(f"  Рёбер: {len(edges)}")


def export_node_link_format(nodes: List[Dict],
                            edges: List[Dict],
                            include_paths: bool = False,
                            metadata: Optional[Dict] = None) -> Dict:
    """
    Экспорт в node-link формат (по умолчанию для NetworkX).
    
    ИЗМЕНЕНИЯ v2:
    - Рёбра содержат source_point и target_point
    """
    graph_data = {
        "directed": False,
        "multigraph": False,
        "graph": metadata or {},
        "nodes": [],
        "links": []
    }

    # Экспорт узлов
    for node in nodes:
        node_data = {
            "id": node['id'],
            "type": node['type'],
            "centroid": node['centroid'],
            "area": node['area'],
            "bbox": node['bbox'],
            "degree": node['degree'],
            "class_id": node.get('class_id'),
            "class_name": node.get('class_name')
        }
        # Добавить yolo_idx если есть
        if 'yolo_idx' in node:
            node_data['yolo_idx'] = node['yolo_idx']
        
        # Добавить segmentation (полигон) если есть
        if node.get('segmentation'):
            node_data['segmentation'] = node['segmentation']
            
        graph_data["nodes"].append(node_data)

    # Экспорт рёбер
    for edge in edges:
        # #43: terminal edges (to=None) пропускаем — downstream не знает что делать с null target
        if edge.get('to') is None:
            continue

        edge_data = {
            "id": edge['id'],
            "source": edge['from'],
            "target": edge['to'],
            "source_point": edge.get('source_point'),  # [y, x] - точка контакта источника
            "target_point": edge.get('target_point'),  # [y, x] - точка контакта цели
            "length": edge['length'],
            "is_terminal": edge['is_terminal'],
            "color": edge.get('color')
        }

        if include_paths:
            edge_data['path'] = edge['path']

        if 'straight_line_distance' in edge:
            edge_data['straight_line_distance'] = edge['straight_line_distance']

        graph_data["links"].append(edge_data)

    return graph_data


def export_adjacency_format(nodes: List[Dict],
                            edges: List[Dict],
                            include_paths: bool = False,
                            metadata: Optional[Dict] = None) -> List[Dict]:
    """
    Экспорт в adjacency формат.
    """
    node_dict = {node['id']: {
        "id": node['id'],
        "type": node['type'],
        "centroid": node['centroid'],
        "area": node['area'],
        "bbox": node['bbox'],
        "degree": node['degree'],
        "adjacency": []
    } for node in nodes}

    for edge in edges:
        source = edge['from']
        target = edge.get('to')

        if target:
            edge_data = {
                "id": edge['id'],
                "length": edge['length'],
                "color": edge.get('color'),
                "source_point": edge.get('source_point'),
                "target_point": edge.get('target_point')
            }

            if include_paths:
                edge_data['path'] = edge['path']

            if 'straight_line_distance' in edge:
                edge_data['straight_line_distance'] = edge['straight_line_distance']

            if source in node_dict:
                node_dict[source]["adjacency"].append({
                    "node": target,
                    **edge_data
                })

            if target in node_dict:
                node_dict[target]["adjacency"].append({
                    "node": source,
                    **edge_data
                })

    return list(node_dict.values())


def export_cytoscape_format(nodes: List[Dict],
                            edges: List[Dict],
                            include_paths: bool = False,
                            metadata: Optional[Dict] = None) -> Dict:
    """
    Экспорт в Cytoscape.js формат.
    """
    elements = {
        "nodes": [],
        "edges": []
    }

    for node in nodes:
        node_data = {
            "data": {
                "id": node['id'],
                "label": node['id'],
                "type": node['type'],
                "centroid": node['centroid'],
                "area": node['area'],
                "degree": node['degree']
            },
            "position": {
                "x": node['centroid'][1],
                "y": node['centroid'][0]
            }
        }
        elements["nodes"].append(node_data)

    for edge in edges:
        if edge.get('to'):
            edge_data = {
                "data": {
                    "id": edge['id'],
                    "source": edge['from'],
                    "target": edge['to'],
                    "length": edge['length'],
                    "color": edge.get('color'),
                    "source_point": edge.get('source_point'),
                    "target_point": edge.get('target_point'),
                    "is_bridge": edge.get('color') is not None
                }
            }

            if include_paths:
                edge_data['data']['path'] = edge['path']

            if 'straight_line_distance' in edge:
                edge_data['data']['straight_line_distance'] = edge['straight_line_distance']

            elements["edges"].append(edge_data)

    return elements


def load_graph_from_json(json_path: str, format: str = 'node-link'):
    """
    Загрузить граф из JSON файла в NetworkX.
    """
    import networkx as nx

    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if format == 'node-link':
        G = nx.node_link_graph(data)
    elif format == 'adjacency':
        G = nx.adjacency_graph(data)
    elif format == 'cytoscape':
        G = nx.Graph()
        for node in data['nodes']:
            G.add_node(node['data']['id'], **node['data'])
        for edge in data['edges']:
            G.add_edge(edge['data']['source'], edge['data']['target'], **edge['data'])
    else:
        raise ValueError(f"Неизвестный формат: {format}")

    return G


class NumpyEncoder(json.JSONEncoder):
    """JSON Encoder для numpy типов."""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NumpyEncoder, self).default(obj)


def create_networkx_graph(nodes: List[Dict], edges: List[Dict]):
    """
    Создать NetworkX граф напрямую из узлов и рёбер.
    """
    import networkx as nx

    G = nx.Graph()

    for node in nodes:
        G.add_node(
            node['id'],
            type=node['type'],
            centroid=node['centroid'],
            area=node['area'],
            bbox=node['bbox'],
            degree=node['degree']
        )

    for edge in edges:
        if not edge['is_terminal']:
            G.add_edge(
                edge['from'],
                edge['to'],
                id=edge['id'],
                length=edge['length'],
                color=edge.get('color'),
                source_point=edge.get('source_point'),
                target_point=edge.get('target_point'),
                path=edge['path'],
                straight_line_distance=edge.get('straight_line_distance')
            )

    return G
