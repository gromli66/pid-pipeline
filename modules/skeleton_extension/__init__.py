"""
Skeleton Extension Module

Модуль для продления скелета и обновления масок после получения предсказаний от U2-Net.

Использование:
    # CLI - обработка директории
    python -m skeleton_extension process --config config/default.yaml

    # CLI - обработка одного файла
    python -m skeleton_extension process-single \\
        --original ./image.png \\
        --prediction ./prediction.png \\
        --nodes ./nodes.png \\
        --output ./result.png \\
        --skeleton-output ./skeleton.png

    # Python API
    from skeleton_extension import process_single_image, load_config

    config = load_config('config/default.yaml')
    process_single_image(original, prediction, nodes, output, skeleton_output, config)
"""

from .core import (
    find_skeleton_endpoints,
    remove_skeleton_under_nodes,
    remove_skeleton_under_nodes_simple,
    remove_skeleton_around_nodes,
    trace_from_endpoint,
    create_endpoint_protection_mask,
    create_simple_protection_mask,
    get_endpoint_direction,
    round_to_4_directions,
    get_line_points,
    check_line_to_endpoint,
    connect_with_directed_lines,
    create_final_protection_mask,
    bfs_connect_endpoints,
    remove_orphan_components,
    trim_orphan_components,
)

from .visualization import (
    visualize_bfs_paths,
    visualize_all_lines,
    visualize_protection_mask,
    visualize_skeleton_contacts,
)

from .processing import process_single_image

from .mask_generation import (
    skeleton_to_mask,
    prune_spurs,
)

import yaml
from pathlib import Path


# Значения по умолчанию
DEFAULT_CONFIG = {
    'node_boundary_expansion': 1,
    'trim_length': 10,
    'trim_protection': 15,
    'mask_width': 8,
    'extend_radius': 5,  # Simple mode: радиус поиска контура для продления
    'direction_trace_length': 5,
    'max_line_length': 600,
    'endpoint_search_radius': 5,
    'endpoint_line_white_tolerance': 2,
    'skeleton_search_radius': 5,
    'skeleton_line_white_tolerance': 2,
    'bfs_max_depth': 1000,
    'bfs_iterations': 1,
    'bfs_mask_tolerance': 5,
    'save_intermediate': True,
    'paths': {
        'original_dir': '',
        'prediction_dir': '',
        'nodes_dir': '',
        'output_dir': '',
        'skeleton_output_dir': '',
    }
}


def load_config(config_path=None):
    """Загрузить конфигурацию из YAML файла

    Приоритет поиска:
    1. Явно указанный путь (--config)
    2. config/default.yaml в папке модуля
    """
    config = DEFAULT_CONFIG.copy()

    # Определяем путь к конфигу
    if config_path and Path(config_path).exists():
        cfg_file = Path(config_path)
    else:
        # Конфиг в папке модуля
        module_dir = Path(__file__).parent
        cfg_file = module_dir / 'config' / 'default.yaml'

    if cfg_file.exists():
        with open(cfg_file, 'r', encoding='utf-8') as f:
            file_config = yaml.safe_load(f)
            if file_config:
                config.update(file_config)

    return config


__all__ = [
    # Core
    'find_skeleton_endpoints',
    'remove_skeleton_under_nodes',
    'remove_skeleton_under_nodes_simple',
    'remove_skeleton_around_nodes',
    'trace_from_endpoint',
    'create_endpoint_protection_mask',
    'create_simple_protection_mask',
    'get_endpoint_direction',
    'round_to_4_directions',
    'get_line_points',
    'check_line_to_endpoint',
    'connect_with_directed_lines',
    'create_final_protection_mask',
    'bfs_connect_endpoints',
    'remove_orphan_components',
    'trim_orphan_components',
    # Visualization
    'visualize_bfs_paths',
    'visualize_all_lines',
    'visualize_protection_mask',
    # Processing
    'process_single_image',
    # Mask generation
    'skeleton_to_mask',
    'prune_spurs',
    # Config
    'load_config',
    'DEFAULT_CONFIG',
]
