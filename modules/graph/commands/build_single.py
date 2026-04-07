"""
Команда build-single - построение графа для одной схемы.
"""

import sys
from pathlib import Path

from ..core.config import load_config, Config
from ..core.builder import GraphBuilder


def run_build_single(args) -> None:
    """
    Выполнить команду build-single.
    
    Args:
        args: Аргументы командной строки
    """
    # Проверить существование файлов
    required_files = [
        ('equipment-mask', args.equipment_mask),
        ('connection-mask', args.connection_mask),
        ('bridge-mask', args.bridge_mask),
        ('skeleton', args.skeleton),
    ]
    
    missing = []
    for name, path in required_files:
        if not Path(path).exists():
            missing.append(f"{name}: {path}")
    
    if missing:
        print("ОШИБКА: Файлы не найдены:")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)
    
    # Загрузить конфиг если указан
    config = None
    if args.config:
        try:
            config = load_config(args.config)
        except FileNotFoundError:
            print(f"ПРЕДУПРЕЖДЕНИЕ: Конфиг не найден: {args.config}, используются значения по умолчанию")
    
    # Параметры по умолчанию или из конфига
    min_spur_length = 5
    max_path_length = 10000
    node_dilation = 1
    dpi = 150
    show_labels = False
    save_stats = False
    debug_isolated = False
    debug_contacts = False
    json_format = "node-link"
    include_paths = False
    
    if config:
        min_spur_length = config.graph.min_spur_length
        max_path_length = config.graph.max_path_length
        node_dilation = config.graph.node_dilation
        dpi = config.visualization.dpi
        show_labels = config.visualization.show_labels
        save_stats = config.visualization.save_stats
        debug_isolated = config.visualization.debug_isolated
        debug_contacts = config.visualization.debug_contacts
        json_format = config.export.json_format
        include_paths = config.export.include_paths
    
    # Определить имя схемы
    if args.name:
        scheme_name = args.name
    else:
        scheme_name = Path(args.skeleton).stem
    
    # Выходные пути
    output_dir = Path(args.output)
    viz_dir = output_dir / "visualizations" / scheme_name
    graphs_dir = output_dir / "graphs"
    graph_path = graphs_dir / f"{scheme_name}.json"
    
    print("=" * 60)
    print("P&ID Graph Builder - Построение графа")
    print("=" * 60)
    print(f"\nВходные файлы:")
    print(f"  Equipment mask:  {args.equipment_mask}")
    print(f"  Connection mask: {args.connection_mask}")
    print(f"  Bridge mask:     {args.bridge_mask}")
    print(f"  Skeleton:        {args.skeleton}")
    if args.original_image:
        print(f"  Original image:  {args.original_image}")
    if args.yolo_labels:
        print(f"  YOLO labels:     {args.yolo_labels}")
    print(f"\nВыходная директория: {output_dir}")
    print(f"Имя схемы: {scheme_name}")
    print("=" * 60)

    # Создать билдер
    builder = GraphBuilder(
        min_spur_length=min_spur_length,
        max_path_length=max_path_length,
        node_dilation=node_dilation,
        dpi=dpi,
        show_labels=show_labels,
        save_stats=save_stats,
        debug_isolated=debug_isolated,
        debug_contacts=debug_contacts,
        json_format=json_format,
        include_paths=include_paths,
        verbose=args.verbose,
        debug=getattr(args, 'debug', False)
    )

    # Построить граф
    try:
        result = builder.build(
            equipment_mask_path=args.equipment_mask,
            connection_mask_path=args.connection_mask,
            bridge_mask_path=args.bridge_mask,
            skeleton_path=args.skeleton,
            original_image_path=args.original_image,
            coco_path=args.coco
        )
        # Сохранить результаты
        builder.save(
            result=result,
            output_dir=str(viz_dir),
            graph_path=str(graph_path),
            scheme_name=scheme_name
        )

        print(f"\n✓ Граф успешно построен!")
        print(f"  Узлов: {len(result['nodes'])}")
        print(f"  Рёбер: {len(result['edges'])}")
        print(f"\nРезультаты:")
        print(f"  Визуализация: {viz_dir}/")
        print(f"  JSON граф:    {graph_path}")

    except Exception as e:
        print(f"\n✗ ОШИБКА: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)