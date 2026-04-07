"""
CLI для P&ID Node Detection.

КОМАНДЫ:
-------
    train     - Полный пайплайн обучения
    finetune  - Дообучение на новых данных
    test      - Оценка модели на test наборе
    inference - Детекция на новых изображениях
    stats     - Статистика датасета

ИСПОЛЬЗОВАНИЕ:
-------------
    # Обучение
    python -m pid_node_detection train --config config.yaml
    
    # Тестирование
    python -m pid_node_detection test --config config.yaml --weights best.pt
    
    # Инференс
    python -m pid_node_detection inference --weights best.pt --input ./images --output ./predictions
    
    # Дообучение
    python -m pid_node_detection finetune --config config.yaml --weights best.pt \
        --new-images ./new_data/images --new-labels ./new_data/labels
    
    # Статистика
    python -m pid_node_detection stats --labels ./dataset/train/labels
"""

import argparse
import sys
from pathlib import Path
from typing import Optional


def create_parser() -> argparse.ArgumentParser:
    """Создать парсер аргументов."""
    parser = argparse.ArgumentParser(
        prog="pid_node_detection",
        description="P&ID Node Detection - детекция узлов на P&ID схемах",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s train --config config.yaml
  %(prog)s test --weights best.pt --config config.yaml
  %(prog)s inference --weights best.pt --input ./images --output ./predictions
  %(prog)s finetune --weights best.pt --new-images ./new --new-labels ./new_labels
  %(prog)s stats --labels ./dataset/train/labels
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Команда")
    
    # === TRAIN ===
    train_parser = subparsers.add_parser("train", help="Полный пайплайн обучения")
    train_parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Путь к конфигурационному файлу (по умолчанию default.yaml)"
    )
    train_parser.add_argument(
        "--name", "-n",
        type=str,
        default="baseline",
        help="Имя эксперимента (по умолчанию 'baseline')"
    )
    train_parser.add_argument(
        "--skip-training",
        action="store_true",
        help="Пропустить обучение (только подготовка данных)"
    )
    
    # === FINETUNE ===
    finetune_parser = subparsers.add_parser("finetune", help="Дообучение на новых данных")
    finetune_parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Путь к конфигурационному файлу"
    )
    finetune_parser.add_argument(
        "--weights", "-w",
        type=str,
        default=None,
        help="Путь к весам модели (по умолчанию из конфига finetune.weights)"
    )
    finetune_parser.add_argument(
        "--new-images",
        type=str,
        default=None,
        help="Директория с новыми изображениями (по умолчанию из конфига finetune.new_images)"
    )
    finetune_parser.add_argument(
        "--new-labels",
        type=str,
        default=None,
        help="Директория с новыми аннотациями (по умолчанию из конфига finetune.new_labels)"
    )
    finetune_parser.add_argument(
        "--name", "-n",
        type=str,
        default="finetune",
        help="Имя эксперимента"
    )
    
    # === TEST ===
    test_parser = subparsers.add_parser("test", help="Оценка модели на test наборе")
    test_parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Путь к конфигурационному файлу"
    )
    test_parser.add_argument(
        "--weights", "-w",
        type=str,
        required=True,
        help="Путь к весам модели"
    )
    test_parser.add_argument(
        "--name", "-n",
        type=str,
        default=None,
        help="Имя для результатов (по умолчанию из имени весов)"
    )
    test_parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Порог уверенности (по умолчанию из конфига)"
    )
    test_parser.add_argument(
        "--no-visualize",
        action="store_true",
        help="Не сохранять визуализации"
    )
    
    # === INFERENCE ===
    inference_parser = subparsers.add_parser("inference", help="Детекция на новых изображениях")
    inference_parser.add_argument(
        "--weights", "-w",
        type=str,
        required=True,
        help="Путь к весам модели"
    )
    inference_parser.add_argument(
        "--input", "-i",
        type=str,
        required=True,
        help="Путь к изображению или директории"
    )
    inference_parser.add_argument(
        "--output", "-o",
        type=str,
        required=True,
        help="Директория для результатов"
    )
    inference_parser.add_argument(
        "--confidence",
        type=float,
        default=0.8,
        help="Порог уверенности (по умолчанию 0.8)"
    )
    inference_parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        help="Устройство (cuda/cpu)"
    )
    inference_parser.add_argument(
        "--no-grayscale",
        action="store_true",
        default=False,
        help="НЕ конвертировать в grayscale перед инференсом (по умолчанию конвертация включена)"
    )
    inference_parser.add_argument(
        "--visualize",
        action="store_true",
        help="Сохранить визуализации детекций"
    )
    
    # === STATS ===
    stats_parser = subparsers.add_parser("stats", help="Статистика датасета")
    stats_parser.add_argument(
        "--labels", "-l",
        type=str,
        required=True,
        help="Директория с аннотациями"
    )
    stats_parser.add_argument(
        "--images",
        type=str,
        default=None,
        help="Директория с изображениями (для анализа размеров)"
    )
    stats_parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Путь для сохранения CSV"
    )
    stats_parser.add_argument(
        "--rare-threshold",
        type=int,
        default=50,
        help="Порог для определения редкого класса"
    )
    
    # === ENSEMBLE TRAIN ===
    ens_train_parser = subparsers.add_parser(
        "ensemble-train",
        help="Обучение ансамбля моделей на разных tile_size"
    )
    ens_train_parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Путь к конфигурационному файлу"
    )
    ens_train_parser.add_argument(
        "--tile-sizes",
        type=int,
        nargs="+",
        default=[640, 1280, 2048],
        help="Размеры тайлов (по умолчанию 640 1280 2048)"
    )
    ens_train_parser.add_argument(
        "--weights-ensemble",
        type=float,
        nargs="+",
        default=None,
        help="Веса моделей в ансамбле (по умолчанию все 1.0)"
    )
    ens_train_parser.add_argument(
        "--name", "-n",
        type=str,
        default="ensemble",
        help="Имя эксперимента"
    )
    ens_train_parser.add_argument(
        "--skip-training",
        action="store_true",
        help="Пропустить обучение (только подготовка данных)"
    )
    
    # === ENSEMBLE TEST ===
    ens_test_parser = subparsers.add_parser(
        "ensemble-test",
        help="Тестирование ансамбля на test наборе"
    )
    ens_test_parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Путь к конфигурационному файлу проекта"
    )
    ens_test_parser.add_argument(
        "--ensemble-config",
        type=str,
        required=True,
        help="Путь к YAML конфигу ансамбля (из ensemble-train)"
    )
    ens_test_parser.add_argument(
        "--merge-strategy",
        type=str,
        choices=["wbf", "nms", "soft_nms"],
        default=None,
        help="Стратегия слияния (по умолчанию из конфига)"
    )
    ens_test_parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Порог уверенности"
    )
    ens_test_parser.add_argument(
        "--name", "-n",
        type=str,
        default=None,
        help="Имя для результатов"
    )
    
    # === ENSEMBLE INFERENCE ===
    ens_inf_parser = subparsers.add_parser(
        "ensemble-inference",
        help="Ансамблевый инференс на новых изображениях"
    )
    ens_inf_parser.add_argument(
        "--ensemble-config",
        type=str,
        required=True,
        help="Путь к YAML конфигу ансамбля"
    )
    ens_inf_parser.add_argument(
        "--input", "-i",
        type=str,
        required=True,
        help="Путь к изображению или директории"
    )
    ens_inf_parser.add_argument(
        "--output", "-o",
        type=str,
        required=True,
        help="Директория для результатов"
    )
    ens_inf_parser.add_argument(
        "--merge-strategy",
        type=str,
        choices=["wbf", "nms", "soft_nms"],
        default=None,
        help="Стратегия слияния"
    )
    ens_inf_parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Порог уверенности"
    )
    ens_inf_parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        help="Устройство"
    )
    ens_inf_parser.add_argument(
        "--no-grayscale",
        action="store_true",
        help="НЕ конвертировать в grayscale"
    )
    
    return parser


def cmd_train(args) -> int:
    """Команда train."""
    from pid_node_detection.config import load_config, load_classes
    from pid_node_detection.pipelines import TrainPipeline
    
    config = load_config(args.config)
    classes = load_classes()
    
    pipeline = TrainPipeline(config, classes)
    pipeline.run(
        experiment_name=args.name,
        skip_training=args.skip_training
    )
    
    return 0


def cmd_finetune(args) -> int:
    """Команда finetune."""
    from pid_node_detection.config import load_config, load_classes
    from pid_node_detection.pipelines import FinetunePipeline
    
    config = load_config(args.config)
    classes = load_classes()
    
    pipeline = FinetunePipeline(config, classes)
    pipeline.run(
        weights=Path(args.weights) if args.weights else None,
        experiment_name=args.name,
        new_images_dir=Path(args.new_images) if args.new_images else None,
        new_labels_dir=Path(args.new_labels) if args.new_labels else None
    )
    
    return 0


def cmd_test(args) -> int:
    """Команда test."""
    from pid_node_detection.config import load_config, load_classes
    from pid_node_detection.pipelines import TestPipeline
    
    config = load_config(args.config)
    classes = load_classes()
    
    pipeline = TestPipeline(config, classes)
    pipeline.run(
        weights=Path(args.weights),
        output_name=args.name,
        confidence_threshold=args.confidence,
        save_visualizations=not args.no_visualize
    )
    
    return 0


def cmd_inference(args) -> int:
    """Команда inference."""
    from pid_node_detection.inference import NodeDetector
    from pid_node_detection.config import load_classes
    
    input_path = Path(args.input)
    output_path = Path(args.output)
    weights = Path(args.weights)
    
    if not weights.exists():
        print(f"Ошибка: веса не найдены: {weights}")
        return 1
    
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Загрузить информацию о классах
    classes = load_classes()
    
    # Создать детектор
    detector = NodeDetector(
        weights=weights,
        confidence=args.confidence,
        device=args.device,
        class_names=classes.class_names,
        reverse_reindex=classes.reverse_reindex_mapping
    )
    
    # Определить список файлов
    if input_path.is_file():
        image_files = [input_path]
    else:
        image_files = list(input_path.glob("*.png")) + list(input_path.glob("*.jpg"))
    
    if not image_files:
        print(f"Не найдено изображений в {input_path}")
        return 1
    
    print(f"\nОбработка {len(image_files)} изображений...")
    
    # Препроцессинг делегирован детектору (binarize_for_yolo method="full")
    # Это гарантирует идентичную предобработку при train и inference:
    #   NLM → CLAHE → Otsu → Dilate → 3ch BGR
    apply_grayscale = not args.no_grayscale

    # Инференс
    predictions_dir = output_path / "labels"
    predictions_dir.mkdir(exist_ok=True)

    if input_path.is_file():
        # Один файл — используем detect() напрямую
        detections = detector.detect(
            input_path,
            apply_grayscale=apply_grayscale,
            apply_reverse_mapping=True
        )
        detector._save_yolo(detections, predictions_dir / f"{input_path.stem}.txt",
                           save_confidence=True)
        stats = {"processed": 1, "total_detections": len(detections)}
    else:
        # Директория — batch
        stats = detector.detect_batch(
            input_dir=input_path,
            output_dir=predictions_dir,
            apply_grayscale=apply_grayscale,
            apply_reverse_mapping=True,
            save_confidence=True
        )

    print(f"\nОбработано: {stats['processed']} изображений")
    print(f"Всего детекций: {stats['total_detections']}")
    print(f"\nРезультаты сохранены: {output_path}")

    return 0


def cmd_stats(args) -> int:
    """Команда stats."""
    from pid_node_detection.data.statistics import analyze_dataset, print_statistics, save_statistics
    from pid_node_detection.config import load_classes

    labels_dir = Path(args.labels)
    images_dir = Path(args.images) if args.images else None

    if not labels_dir.exists():
        print(f"Ошибка: директория не найдена: {labels_dir}")
        return 1

    # Загрузить имена классов
    try:
        classes = load_classes()
        class_names = classes.class_names
    except Exception:
        class_names = None

    stats = analyze_dataset(
        labels_dir=labels_dir,
        images_dir=images_dir,
        class_names=class_names,
        rare_threshold=args.rare_threshold
    )

    print_statistics(stats, f"Статистика: {labels_dir}")

    if args.output:
        save_statistics(stats, Path(args.output))

    return 0


def cmd_ensemble_train(args) -> int:
    """Команда ensemble-train."""
    from pid_node_detection.config import load_config, load_classes
    from pid_node_detection.pipelines import EnsembleTrainPipeline
    
    config = load_config(args.config)
    classes = load_classes()
    
    pipeline = EnsembleTrainPipeline(config, classes)
    pipeline.run(
        tile_sizes=args.tile_sizes,
        experiment_name=args.name,
        model_weights=args.weights_ensemble,
        skip_training=args.skip_training
    )
    
    return 0


def cmd_ensemble_test(args) -> int:
    """Команда ensemble-test."""
    from pid_node_detection.config import load_config, load_classes
    from pid_node_detection.pipelines import EnsembleTestPipeline
    
    config = load_config(args.config)
    classes = load_classes()
    
    pipeline = EnsembleTestPipeline(config, classes)
    pipeline.run(
        ensemble_config_path=Path(args.ensemble_config),
        output_name=args.name,
        merge_strategy=args.merge_strategy,
        confidence_threshold=args.confidence
    )
    
    return 0


def cmd_ensemble_inference(args) -> int:
    """Команда ensemble-inference."""
    import yaml
    from pid_node_detection.inference.ensemble import EnsembleDetector
    from pid_node_detection.config import load_classes
    
    # Загрузить конфиг ансамбля
    with open(args.ensemble_config, "r") as f:
        ens_cfg = yaml.safe_load(f)
    
    if args.merge_strategy:
        ens_cfg["merge_strategy"] = args.merge_strategy
    if args.confidence is not None:
        ens_cfg["confidence_threshold"] = args.confidence
    
    classes = load_classes()
    
    detector = EnsembleDetector(
        models=ens_cfg["models"],
        merge_strategy=ens_cfg.get("merge_strategy", "wbf"),
        iou_threshold=ens_cfg.get("iou_threshold", 0.5),
        confidence_threshold=ens_cfg.get("confidence_threshold", 0.5),
        device=args.device,
        class_names=classes.class_names,
        reverse_reindex=classes.reverse_reindex_mapping
    )
    
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)
    
    apply_grayscale = not args.no_grayscale
    
    if input_path.is_file():
        detections = detector.detect(
            input_path,
            apply_grayscale=apply_grayscale,
            apply_reverse_mapping=True
        )
        
        out_file = output_path / f"{input_path.stem}.txt"
        with open(out_file, "w") as f:
            for det in detections:
                f.write(f"{det['class_id']} {det['x_center']:.6f} "
                        f"{det['y_center']:.6f} {det['width']:.6f} "
                        f"{det['height']:.6f} {det['confidence']:.4f}\n")
        
        print(f"Детекций: {len(detections)}")
    else:
        stats = detector.detect_batch(
            input_dir=input_path,
            output_dir=output_path,
            apply_grayscale=apply_grayscale,
            apply_reverse_mapping=True
        )
        print(f"Обработано: {stats['processed']}, детекций: {stats['total_detections']}")
    
    print(f"Результаты: {output_path}")
    return 0


def main() -> int:
    """Главная функция CLI."""
    parser = create_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "train": cmd_train,
        "finetune": cmd_finetune,
        "test": cmd_test,
        "inference": cmd_inference,
        "stats": cmd_stats,
        "ensemble-train": cmd_ensemble_train,
        "ensemble-test": cmd_ensemble_test,
        "ensemble-inference": cmd_ensemble_inference,
    }

    if args.command not in commands:
        print(f"Неизвестная команда: {args.command}")
        return 1

    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем")
        return 130
    except Exception as e:
        print(f"\nОшибка: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())