"""
Команда build - построение графов для всех схем в директории.
"""

import sys
import time
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ProcessPoolExecutor, as_completed

from ..core.config import load_config, validate_paths, Config
from ..core.builder import GraphBuilder


def find_matching_files(
    base_dir: Path, 
    extensions: tuple = ('.png', '.jpg', '.jpeg', '.tif', '.tiff')
) -> List[str]:
    """Найти все файлы изображений и вернуть базовые имена."""
    files = []
    for ext in extensions:
        files.extend([f.stem for f in base_dir.glob(f'*{ext}')])
        files.extend([f.stem for f in base_dir.glob(f'*{ext.upper()}')])
    return sorted(list(set(files)))


def get_file_path(
    directory: Path, 
    base_name: str, 
    extensions: tuple = ('.png', '.jpg', '.jpeg', '.tif', '.tiff')
) -> Optional[Path]:
    """Найти файл с данным базовым именем в директории."""
    for ext in extensions:
        path = directory / f"{base_name}{ext}"
        if path.exists():
            return path
        path = directory / f"{base_name}{ext.upper()}"
        if path.exists():
            return path
    return None


def process_single_scheme(
    base_name: str,
    config: Config,
    verbose: bool = False
) -> dict:
    """
    Обработать одну схему.
    
    Returns:
        Словарь с результатами
    """
    result = {
        'name': base_name,
        'success': False,
        'error': None,
        'num_nodes': 0,
        'num_edges': 0,
        'time': 0
    }
    
    start_time = time.time()
    
    try:
        # Найти файлы
        equipment_path = get_file_path(Path(config.paths.equipment_masks), base_name)
        connection_path = get_file_path(Path(config.paths.connection_masks), base_name)
        bridge_path = get_file_path(Path(config.paths.bridge_masks), base_name)
        skeleton_path = get_file_path(Path(config.paths.skeletons), base_name)
        
        turn_path = None
        if config.paths.turn_masks:
            turn_path = get_file_path(Path(config.paths.turn_masks), base_name)
        
        original_path = None
        if config.paths.original_images:
            original_path = get_file_path(Path(config.paths.original_images), base_name)
        
        # Разметка классов
        labels_format = config.labels.format if hasattr(config, 'labels') else "yolo"
        yolo_labels_path = None
        coco_path = None
        
        if labels_format == "coco":
            # COCO: один JSON файл
            if hasattr(config.labels, 'coco_annotations') and config.labels.coco_annotations:
                coco_file = Path(config.labels.coco_annotations)
                if coco_file.exists():
                    coco_path = str(coco_file)
        else:
            # YOLO: директория с .txt файлами
            if hasattr(config.labels, 'yolo_labels_dir') and config.labels.yolo_labels_dir:
                yolo_dir = Path(config.labels.yolo_labels_dir)
                yolo_file = yolo_dir / f"{base_name}.txt"
                if yolo_file.exists():
                    yolo_labels_path = str(yolo_file)
        
        # Проверка обязательных файлов
        missing = []
        if not equipment_path:
            missing.append(f"equipment_mask")
        if not connection_path:
            missing.append(f"connection_mask")
        if not bridge_path:
            missing.append(f"bridge_mask")
        if not skeleton_path:
            missing.append(f"skeleton")
        
        if missing:
            result['error'] = f"Отсутствуют: {', '.join(missing)}"
            return result
        
        # Создать билдер
        builder = GraphBuilder(
            min_spur_length=config.graph.min_spur_length,
            max_path_length=config.graph.max_path_length,
            node_dilation=config.graph.node_dilation,
            dpi=config.visualization.dpi,
            show_labels=config.visualization.show_labels,
            save_stats=config.visualization.save_stats,
            debug_isolated=config.visualization.debug_isolated,
            debug_contacts=config.visualization.debug_contacts,
            json_format=config.export.json_format,
            include_paths=config.export.include_paths,
            verbose=verbose,
            debug=getattr(config.processing, 'debug', False)
        )
        
        # Построить граф
        graph_result = builder.build(
            equipment_mask_path=str(equipment_path),
            connection_mask_path=str(connection_path),
            bridge_mask_path=str(bridge_path),
            skeleton_path=str(skeleton_path),
            turn_mask_path=str(turn_path) if turn_path else None,
            original_image_path=str(original_path) if original_path else None,
            coco_path=coco_path
        )

        # Выходные пути
        output_dir = Path(config.paths.output_dir)
        viz_dir = output_dir / "visualizations"  # Без подпапок
        graphs_dir = output_dir / "graphs"
        graph_path = graphs_dir / f"{base_name}.json"

        # Сохранить результаты
        builder.save(
            result=graph_result,
            output_dir=str(viz_dir),
            graph_path=str(graph_path),
            scheme_name=base_name
        )

        result['success'] = True
        result['num_nodes'] = len(graph_result['nodes'])
        result['num_edges'] = len(graph_result['edges'])
        result['time'] = time.time() - start_time

    except Exception as e:
        import traceback
        result['error'] = f"{str(e)}\n{traceback.format_exc()}"
        result['time'] = time.time() - start_time

    return result


def run_build(args) -> None:
    """
    Выполнить команду build.

    Args:
        args: Аргументы командной строки
    """
    # Загрузить конфиг
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ОШИБКА: {e}")
        sys.exit(1)

    # Переопределить verbose из аргументов
    if args.verbose:
        config.processing.verbose = True

    # Переопределить debug из аргументов
    if hasattr(args, 'debug') and args.debug:
        config.processing.debug = True
    elif not hasattr(config.processing, 'debug'):
        config.processing.debug = False

    # Проверить пути
    try:
        validate_paths(config)
    except FileNotFoundError as e:
        print(f"ОШИБКА: {e}")
        sys.exit(1)

    # Найти файлы для обработки
    skeleton_dir = Path(config.paths.skeletons)
    base_names = find_matching_files(skeleton_dir)

    if not base_names:
        print(f"ОШИБКА: Не найдено файлов в {skeleton_dir}")
        sys.exit(1)

    # Ограничение количества
    if args.limit:
        base_names = base_names[:args.limit]

    # Создать выходные директории
    output_dir = Path(config.paths.output_dir)
    (output_dir / "visualizations").mkdir(parents=True, exist_ok=True)
    (output_dir / "graphs").mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("P&ID Graph Builder - BATCH обработка")
    print("=" * 70)
    print(f"\nВходные директории:")
    print(f"  Equipment masks:  {config.paths.equipment_masks}")
    print(f"  Connection masks: {config.paths.connection_masks}")
    print(f"  Turn masks:       {config.paths.turn_masks or 'не указано'}")
    print(f"  Bridge masks:     {config.paths.bridge_masks}")
    print(f"  Skeletons:        {config.paths.skeletons}")
    print(f"  Original images:  {config.paths.original_images or 'не указано'}")

    # Информация о разметке
    labels_format = config.labels.format if hasattr(config, 'labels') else "yolo"
    if labels_format == "coco":
        coco_path = config.labels.coco_annotations if hasattr(config.labels, 'coco_annotations') else None
        print(f"  Labels format:    COCO")
        print(f"  COCO annotations: {coco_path or 'не указано'}")
    else:
        yolo_dir = config.labels.yolo_labels_dir if hasattr(config.labels, 'yolo_labels_dir') else None
        print(f"  Labels format:    YOLO")
        print(f"  YOLO labels:      {yolo_dir or 'не указано'}")

    print(f"\nВыходная директория: {output_dir}")
    print(f"\nНайдено файлов для обработки: {len(base_names)}")
    print(f"Параллельных процессов: {config.processing.workers}")
    print("=" * 70)

    total_start_time = time.time()
    results = []

    if config.processing.workers == 1:
        # Последовательная обработка
        for i, base_name in enumerate(base_names, 1):
            print(f"\n[{i}/{len(base_names)}] Обработка: {base_name}")

            result = process_single_scheme(
                base_name=base_name,
                config=config,
                verbose=config.processing.verbose
            )

            results.append(result)

            if result['success']:
                print(f"  ✓ Узлов: {result['num_nodes']}, Рёбер: {result['num_edges']}, "
                      f"Время: {result['time']:.2f}с")
            else:
                print(f"  ✗ ОШИБКА: {result['error'][:100]}...")
    else:
        # Параллельная обработка
        with ProcessPoolExecutor(max_workers=config.processing.workers) as executor:
            futures = {
                executor.submit(
                    process_single_scheme,
                    base_name=base_name,
                    config=config,
                    verbose=False  # В параллельном режиме без verbose
                ): base_name
                for base_name in base_names
            }

            for i, future in enumerate(as_completed(futures), 1):
                base_name = futures[future]
                try:
                    result = future.result()
                    results.append(result)

                    status = "✓" if result['success'] else "✗"
                    print(f"[{i}/{len(base_names)}] {status} {base_name} "
                          f"({result['time']:.2f}с)")
                except Exception as e:
                    print(f"[{i}/{len(base_names)}] ✗ {base_name}: {e}")
                    results.append({
                        'name': base_name,
                        'success': False,
                        'error': str(e),
                        'num_nodes': 0,
                        'num_edges': 0,
                        'time': 0
                    })

    # ===== ИТОГОВАЯ СТАТИСТИКА =====
    total_time = time.time() - total_start_time
    successful = [r for r in results if r['success']]
    failed = [r for r in results if not r['success']]

    print("\n" + "=" * 70)
    print("ИТОГИ BATCH ОБРАБОТКИ")
    print("=" * 70)
    print(f"\nВсего файлов:      {len(results)}")
    print(f"Успешно:           {len(successful)} ({100 * len(successful) / len(results):.1f}%)")
    print(f"С ошибками:        {len(failed)}")
    print(f"\nОбщее время:       {total_time:.2f} сек")
    if results:
        print(f"Среднее время:     {total_time / len(results):.2f} сек/файл")

    if successful:
        total_nodes = sum(r['num_nodes'] for r in successful)
        total_edges = sum(r['num_edges'] for r in successful)
        print(f"\nВсего узлов:       {total_nodes}")
        print(f"Всего рёбер:       {total_edges}")
        print(f"Среднее узлов:     {total_nodes / len(successful):.1f}")
        print(f"Среднее рёбер:     {total_edges / len(successful):.1f}")

    if failed:
        print(f"\nФайлы с ошибками:")
        for r in failed[:10]:
            error_short = r['error'].split('\n')[0][:80] if r['error'] else 'Unknown'
            print(f"  - {r['name']}: {error_short}...")
        if len(failed) > 10:
            print(f"  ... и ещё {len(failed) - 10} файлов")

    print(f"\nРезультаты сохранены в: {output_dir}")
    print(f"  - visualizations/<scheme_name>/  (визуализации)")
    print(f"  - graphs/                        (JSON графы)")
    print("=" * 70)

    # Сохранить сводку
    summary_path = output_dir / "batch_summary.txt"
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write("P&ID Graph Builder - Batch Summary\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"Total files: {len(results)}\n")
        f.write(f"Successful: {len(successful)}\n")
        f.write(f"Failed: {len(failed)}\n")
        f.write(f"Total time: {total_time:.2f} sec\n\n")

        if failed:
            f.write("FAILED FILES:\n")
            for r in failed:
                f.write(f"\n{r['name']}:\n{r['error']}\n")

    print(f"\nСводка сохранена: {summary_path}")