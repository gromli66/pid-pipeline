"""
Основной класс для построения графа P&ID схемы.
"""

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
from PIL import Image

from .utils import load_binary_mask
from .nodes import extract_nodes, update_node_degrees, prepare_tracing_data, assign_node_classes, filter_isolated_connectors, load_coco_annotations
from .tracing import trace_edges, trace_edges_v3, compute_edge_statistics
from .bridge_preprocessing import preprocess_bridges
from .visualize import plot_graph_overlay, plot_statistics, plot_isolated_nodes_debug, plot_contact_points_debug
from .export_json import export_graph_to_json


class GraphBuilder:
    """
    Построитель графа P&ID схемы.
    
    Пример использования:
        builder = GraphBuilder(
            min_spur_length=5,
            max_path_length=10000,
            verbose=True
        )
        
        result = builder.build(
            equipment_mask_path="masks/equipment.png",
            connection_mask_path="masks/connections.png",
            bridge_mask_path="masks/bridges.png",
            skeleton_path="masks/skeleton.png"
        )
        
        builder.save(
            result=result,
            output_dir="output/scheme_001",
            graph_path="graphs/scheme_001.json"
        )
    """
    
    def __init__(
        self,
        min_spur_length: int = 5,
        max_path_length: int = 10000,
        node_dilation: int = 1,
        dpi: int = 150,
        show_labels: bool = False,
        save_stats: bool = False,
        debug_isolated: bool = False,
        debug_contacts: bool = False,
        json_format: str = "node-link",
        include_paths: bool = False,
        verbose: bool = False,
        debug: bool = False
    ):
        """
        Инициализация билдера.
        
        Args:
            min_spur_length: Минимальная длина концевого ребра
            max_path_length: Максимальная длина пути
            node_dilation: Дилатация узлов для поиска контактов
            dpi: Разрешение визуализации
            show_labels: Показывать ID узлов
            save_stats: Сохранять статистику
            debug_isolated: Отладка изолированных узлов
            debug_contacts: Отладка точек контакта
            json_format: Формат JSON экспорта
            include_paths: Включать пути в JSON
            verbose: Подробный вывод
            debug: Отладочный вывод (детали трассировки, классификации)
        """
        self.min_spur_length = min_spur_length
        self.max_path_length = max_path_length
        self.node_dilation = node_dilation
        self.dpi = dpi
        self.show_labels = show_labels
        self.save_stats = save_stats
        self.debug_isolated = debug_isolated
        self.debug_contacts = debug_contacts
        self.json_format = json_format
        self.include_paths = include_paths
        self.verbose = verbose
        self.debug = debug
    
    def build(
        self,
        equipment_mask_path: str,
        connection_mask_path: str,
        bridge_mask_path: str,
        skeleton_path: str,
        turn_mask_path: Optional[str] = None,
        original_image_path: Optional[str] = None,
        coco_path: Optional[str] = None,
        image_filename: Optional[str] = None
    ) -> Dict:
        """
        Построить граф из масок.
        
        Args:
            equipment_mask_path: Путь к маске оборудования
            connection_mask_path: Путь к маске connections
            bridge_mask_path: Путь к маске bridges
            skeleton_path: Путь к скелету
            turn_mask_path: Путь к маске turns (опционально)
            original_image_path: Путь к оригинальному изображению (опционально)
            coco_path: Путь к COCO JSON файлу с аннотациями
            image_filename: Имя файла изображения в COCO JSON (опционально,
                            если не указано — выводится из equipment_mask_path)
            
        Returns:
            Словарь с результатами построения графа
        """
        start_time = time.time()
        
        if self.verbose:
            print("=" * 60)
            print("P&ID Graph Builder - Построение графа")
            print("=" * 60)
        
        # ===== ЗАГРУЗКА ДАННЫХ =====
        if self.verbose:
            print("\n[1/6] Загрузка масок...")
        
        equipment_mask = load_binary_mask(equipment_mask_path)
        connection_mask = load_binary_mask(connection_mask_path)
        bridge_mask = load_binary_mask(bridge_mask_path)
        skeleton = load_binary_mask(skeleton_path)
        
        # Объединить connection + turn если есть
        if turn_mask_path and Path(turn_mask_path).exists():
            turn_mask = load_binary_mask(turn_mask_path)
            connection_mask = np.logical_or(connection_mask, turn_mask)
        
        if self.verbose:
            print(f"  Размер изображения: {skeleton.shape}")
            print(f"  Пикселей скелета: {np.sum(skeleton)}")
            print(f"  Пикселей оборудования: {np.sum(equipment_mask)}")
            print(f"  Пикселей connections: {np.sum(connection_mask)}")
            print(f"  Пикселей bridges: {np.sum(bridge_mask)}")
        
        # Загрузка оригинального изображения
        original_image = None
        if original_image_path and Path(original_image_path).exists():
            img = Image.open(original_image_path).convert('L')
            original_image = np.array(img)
            if self.verbose:
                print(f"  Оригинальное изображение загружено")
        
        # ===== ЭТАП 0: BRIDGE PREPROCESSING =====
        if self.verbose:
            print("\n[2/6] Bridge preprocessing...")
        
        bridge_results = preprocess_bridges(
            bridge_mask=bridge_mask,
            connection_mask=connection_mask,
            skeleton=skeleton,
            dilation=self.node_dilation,
            verbose=self.verbose
        )
        
        valid_bridges_mask = bridge_results['valid_bridges_mask']
        invalid_bridges_mask = bridge_results['invalid_bridges_mask']
        bridge_routing = bridge_results['bridge_routing']
        bridge_info = bridge_results['bridge_info']
        updated_connections_mask = bridge_results['updated_connections_mask']
        
        # Unified nodes mask
        unified_nodes_mask = np.logical_or(equipment_mask, updated_connections_mask)
        
        if self.verbose:
            print(f"\n  Итоговая маска узлов:")
            print(f"    Equipment: {np.sum(equipment_mask)}")
            print(f"    Connections (updated): {np.sum(updated_connections_mask)}")
            print(f"    Unified nodes: {np.sum(unified_nodes_mask)}")
            print(f"    Valid bridges: {np.sum(valid_bridges_mask)}")
            print(f"    Invalid bridges: {np.sum(invalid_bridges_mask)}")
        
        # ===== ЗАГРУЗКА COCO АННОТАЦИЙ =====
        annotations = []
        if coco_path:
            # Имя файла изображения для поиска в COCO
            if image_filename:
                # Явно указанное имя — используем напрямую
                annotations = load_coco_annotations(coco_path, image_filename, skeleton.shape)
            else:
                # Пробуем разные варианты имени
                img_filename = Path(equipment_mask_path).name
                # Если маска имеет суффикс типа _equipment.png, убираем его
                base_name = img_filename.replace('_equipment', '').replace('_mask', '')
                
                annotations = load_coco_annotations(coco_path, base_name, skeleton.shape)
                if not annotations:
                    # Пробуем оригинальное имя
                    annotations = load_coco_annotations(coco_path, img_filename, skeleton.shape)
            
            if self.verbose:
                num_with_seg = sum(1 for a in annotations if a.get('segmentation'))
                print(f"\n  Загружено COCO аннотаций: {len(annotations)}")
                print(f"    - с полигонами (segmentation): {num_with_seg}")
                print(f"    - только bbox: {len(annotations) - num_with_seg}")
        
        # ===== ЭТАП 1: ИЗВЛЕЧЕНИЕ КОМПОНЕНТ (для геометрии) =====
        if self.verbose:
            print("\n[3/6] Извлечение компонент маски...")
        
        # РАЗДЕЛЬНАЯ нумерация equipment и connectors
        # Это предотвращает слияние касающихся масок в одну компоненту
        from scipy import ndimage
        labeled_equipment, num_equipment = ndimage.label(equipment_mask)
        labeled_connectors, num_connectors = ndimage.label(updated_connections_mask)
        
        # Для обратной совместимости создаём unified labeled_nodes
        # (используется в визуализации и некоторых функциях)
        # Connectors получают сдвинутые label_id: num_equipment + 1, num_equipment + 2, ...
        labeled_nodes = labeled_equipment.copy()
        connector_offset = num_equipment
        labeled_nodes[labeled_connectors > 0] = labeled_connectors[labeled_connectors > 0] + connector_offset
        num_components = num_equipment + num_connectors
        
        if self.verbose:
            print(f"  Компонент equipment: {num_equipment}")
            print(f"  Компонент connectors: {num_connectors}")
            print(f"  Всего компонент: {num_components}")
            print(f"  Найдено компонент: {num_components}")
        
        # ===== ПОДГОТОВКА ДАННЫХ ДЛЯ ТРАССИРОВКИ =====
        if self.verbose:
            print("\n[4/6] Подготовка данных для трассировки...")
        
        skeleton_cleaned, contact_map, bridge_contact_map = prepare_tracing_data(
            skeleton=skeleton,
            labeled_equipment=labeled_equipment,
            labeled_connectors=labeled_connectors,
            connector_offset=connector_offset,
            valid_bridges_mask=valid_bridges_mask,
            bridge_routing=bridge_routing,
            dilation=self.node_dilation,
            debug=self.debug
        )
        
        # ===== ЭТАП 2: ТРАССИРОВКА РЁБЕР (v3 — динамические узлы) =====
        if self.verbose:
            print("\n[5/6] Трассировка рёбер (v3)...")
        
        edges, nodes = trace_edges_v3(
            skeleton=skeleton_cleaned,
            labeled_equipment=labeled_equipment,
            labeled_connectors=labeled_connectors,
            contact_map=contact_map,
            bridge_contact_map=bridge_contact_map,
            bridge_routing=bridge_routing,
            equipment_mask=equipment_mask,
            connection_mask=updated_connections_mask,
            annotations=annotations,
            max_path_length=self.max_path_length,
            connector_offset=connector_offset,
            debug=self.debug
        )
        
        # Вычислить статистики для рёбер
        for edge in edges:
            compute_edge_statistics(edge)
        
        # Обновить degree узлов
        update_node_degrees(nodes, edges, debug=self.debug)
        
        # ===== ЭТАП 3: ФИЛЬТРАЦИЯ =====
        if self.verbose:
            print("\n[6/7] Фильтрация коротких шпор...")
        
        edges_before = len(edges)
        edges = [e for e in edges if not (e['is_terminal'] and e['length'] < self.min_spur_length)]
        removed = edges_before - len(edges)
        
        if self.verbose:
            print(f"  Удалено коротких шпор: {removed}")
        
        # Обновить degree после фильтрации
        update_node_degrees(nodes, edges, debug=self.debug)
        
        # ===== ЭТАП 4: ФИЛЬТРАЦИЯ ИЗОЛИРОВАННЫХ CONNECTOR'ОВ =====
        if self.verbose:
            print("\n[7/7] Фильтрация изолированных connector'ов...")
        
        nodes, edges, filter_stats = filter_isolated_connectors(nodes, edges, debug=self.debug)
        
        if self.verbose:
            print(f"  Удалено connector'ов: {filter_stats['removed_connectors']}")
            print(f"  Удалено рёбер: {filter_stats['removed_edges']}")
        
        # Обновить degree после фильтрации connector'ов
        update_node_degrees(nodes, edges, debug=self.debug)
        
        elapsed = time.time() - start_time
        
        # ===== РЕЗУЛЬТАТ =====
        result = {
            'nodes': nodes,
            'edges': edges,
            'skeleton': skeleton,
            'skeleton_cleaned': skeleton_cleaned,
            'labeled_nodes': labeled_nodes,
            'equipment_mask': equipment_mask,
            'connection_mask': connection_mask,
            'valid_bridges_mask': valid_bridges_mask,
            'invalid_bridges_mask': invalid_bridges_mask,
            'bridge_routing': bridge_routing,
            'bridge_info': bridge_info,
            'original_image': original_image,
            'elapsed_time': elapsed,
            'metadata': {
                'source': 'P&ID Graph Builder',
                'version': '1.0',
                'image_size': list(skeleton.shape),
                'num_nodes': len(nodes),
                'num_edges': len(edges),
                'num_valid_bridges': len(bridge_routing),
                'num_invalid_bridges': len([n for n in nodes if n['type'] == 'invalid_bridge']),
                'num_isolated_nodes': sum(1 for n in nodes if n['degree'] == 0)
            }
        }
        
        if self.verbose:
            self._print_summary(result)
        
        return result
    
    def save(
        self,
        result: Dict,
        output_dir: str,
        graph_path: str,
        scheme_name: str = "scheme"
    ) -> None:
        """
        Сохранить результаты построения графа.
        
        Args:
            result: Результат build()
            output_dir: Директория для визуализаций
            graph_path: Путь для JSON графа
            scheme_name: Имя схемы для файлов
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        graph_path = Path(graph_path)
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        
        # ===== ВИЗУАЛИЗАЦИЯ =====
        viz_path = output_dir / f"{scheme_name}_graph.png"
        plot_graph_overlay(
            skeleton=result['skeleton_cleaned'],
            labeled_nodes=result['labeled_nodes'],
            nodes=result['nodes'],
            edges=result['edges'],
            output_path=str(viz_path),
            original_image=result['original_image'],
            show_node_labels=self.show_labels,
            dpi=self.dpi,
            valid_bridges_mask=result['valid_bridges_mask'],
            bridge_info=result['bridge_info']
        )
        
        # ===== СТАТИСТИКА =====
        if self.save_stats:
            stats_path = output_dir / f"{scheme_name}_stats.png"
            plot_statistics(result['nodes'], result['edges'], str(stats_path))
        
        # ===== ОТЛАДКА =====
        if self.debug_isolated:
            debug_path = output_dir / f"{scheme_name}_debug_isolated.png"
            plot_isolated_nodes_debug(
                skeleton=result['skeleton_cleaned'],
                labeled_nodes=result['labeled_nodes'],
                nodes=result['nodes'],
                edges=result['edges'],
                output_path=str(debug_path),
                dpi=self.dpi
            )
        
        if self.debug_contacts:
            debug_path = output_dir / f"{scheme_name}_debug_contacts.png"
            plot_contact_points_debug(
                skeleton=result['skeleton'],
                labeled_nodes=result['labeled_nodes'],
                nodes=result['nodes'],
                output_path=str(debug_path),
                dpi=self.dpi
            )
        
        # ===== JSON ЭКСПОРТ =====
        export_graph_to_json(
            nodes=result['nodes'],
            edges=result['edges'],
            output_path=str(graph_path),
            format=self.json_format,
            include_paths=self.include_paths,
            metadata=result['metadata']
        )
    
    def _print_summary(self, result: Dict) -> None:
        """Вывести сводку результатов."""
        nodes = result['nodes']
        edges = result['edges']
        metadata = result['metadata']
        
        print("\n" + "=" * 60)
        print("РЕЗУЛЬТАТЫ:")
        print("=" * 60)
        
        num_equipment = np.sum(result['equipment_mask'] > 0)
        num_connections = np.sum(result['connection_mask'] > 0)
        num_invalid_bridges = metadata['num_invalid_bridges']
        num_isolated = metadata['num_isolated_nodes']
        
        num_normal = len([e for e in edges if not e['is_terminal'] and e.get('color') is None])
        num_blue = len([e for e in edges if e.get('color') == 'BLUE'])
        num_yellow = len([e for e in edges if e.get('color') == 'YELLOW'])
        num_terminal = len([e for e in edges if e['is_terminal']])
        
        print(f"  Узлов: {len(nodes)}")
        print(f"    - Equipment pixels: {num_equipment}")
        print(f"    - Connection pixels: {num_connections}")
        print(f"    - Invalid bridges: {num_invalid_bridges}")
        print(f"  Валидных мостов: {metadata['num_valid_bridges']}")
        print(f"  Рёбер: {len(edges)}")
        print(f"    - Обычные (зелёные): {num_normal}")
        print(f"    - Через мосты pipe1 (синие): {num_blue}")
        print(f"    - Через мосты pipe2 (жёлтые): {num_yellow}")
        print(f"    - Terminal (серые): {num_terminal}")
        print(f"  Изолированных узлов: {num_isolated}")
        print(f"\n  Время выполнения: {result['elapsed_time']:.2f} сек")
        print("=" * 60)
