"""
Stage 2: Finetune на чисто новых данных.

НАЗНАЧЕНИЕ:
----------
После обучения с нуля на old+new (Stage 1), 
подстраиваем модель чисто под новый стиль.

ЧТО ДЕЛАЕТ:
----------
1. Берёт ТОЛЬКО новые схемы из raw
2. Разбивает на train/val/test
3. Препроцессинг (full binarization)
4. Тайлинг
5. CPA (для редких классов)
6. Rotation (random_1 = 2x)
7. Finetune со Stage 1 весов (lr=0.0001, 20-30 эпох)

ЗАПУСК:
------
    cd C:\\project\\pid\\pid\\app
    python -m pid_node_detection.stage2_finetune
    
    # Или с параметрами:
    python -m pid_node_detection.stage2_finetune --weights path/to/best.pt --epochs 25
"""

import argparse
import cv2
import shutil
import random
from pathlib import Path
from typing import List, Tuple

from pid_node_detection.config import load_config, load_classes
from pid_node_detection.data.preprocessing import binarize_image, filter_labels
from pid_node_detection.data.tiling import tile_dataset
from pid_node_detection.data.statistics import analyze_dataset, print_statistics, save_statistics
from pid_node_detection.augmentation.copy_paste import CopyPasteAugmentation
from pid_node_detection.augmentation.rotation import RotationAugmentation
from pid_node_detection.training.trainer import YOLOTrainer, create_data_yaml


def get_image_files(images_dir: Path, extensions: list) -> List[Path]:
    files = []
    for ext in extensions:
        files.extend(images_dir.glob(f"*{ext}"))
    return sorted(files)


def count_items(directory: Path, pattern: str = "*.png") -> int:
    if directory.exists():
        return len(list(directory.glob(pattern)))
    return 0


def copy_dir_contents(src_dir: Path, dst_dir: Path, pattern: str = "*"):
    dst_dir.mkdir(parents=True, exist_ok=True)
    for f in src_dir.glob(pattern):
        if f.is_file():
            shutil.copy(f, dst_dir / f.name)


def main():
    parser = argparse.ArgumentParser(description="Stage 2: Finetune на новых данных")
    parser.add_argument("--weights", "-w", type=str, default=None,
                        help="Путь к весам Stage 1 (default: из конфига)")
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="Путь к конфигу")
    parser.add_argument("--name", "-n", type=str, default="stage2_finetune",
                        help="Имя эксперимента")
    parser.add_argument("--epochs", type=int, default=25,
                        help="Количество эпох (default: 25)")
    parser.add_argument("--lr", type=float, default=0.0001,
                        help="Learning rate (default: 0.0001)")
    parser.add_argument("--patience", type=int, default=10,
                        help="Early stopping patience (default: 10)")
    parser.add_argument("--batch", type=int, default=4,
                        help="Batch size (default: 4)")
    parser.add_argument("--val-ratio", type=float, default=0.15,
                        help="Доля val (default: 0.15)")
    parser.add_argument("--test-count", type=int, default=2,
                        help="Схем в test (default: 2)")
    args = parser.parse_args()

    # ─────────────────────────────────────────────────────────────
    config = load_config(args.config)
    classes = load_classes()

    # Пути
    new_images_dir = Path(config.finetune.new_images)
    new_labels_dir = Path(config.finetune.new_labels)
    work_dir = Path(config.paths.work_dir) / "stage2"
    experiments_dir = Path(config.paths.experiments_dir)
    statistics_dir = Path(config.paths.statistics_dir)

    # Веса
    if args.weights:
        weights = Path(args.weights)
    else:
        weights = Path(config.finetune.weights)

    if not weights.exists():
        raise FileNotFoundError(f"Веса не найдены: {weights}")

    extensions = config.preprocessing.image_extensions
    seed = config.splitting.random_seed

    print("\n" + "#"*70)
    print("#" + " "*15 + "STAGE 2: FINETUNE НА НОВЫХ" + " "*15 + "#")
    print("#"*70)
    print(f"\n  Веса Stage 1: {weights}")
    print(f"  Новые данные: {new_images_dir}")
    print(f"  LR: {args.lr}")
    print(f"  Epochs: {args.epochs}")
    print(f"  Patience: {args.patience}")
    print(f"  Batch: {args.batch}")

    work_dir.mkdir(parents=True, exist_ok=True)

    # ═══════════════════════════════════════════════════════════════
    # 1. Сбор и разбиение ТОЛЬКО новых данных
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 1: СБОР И РАЗБИЕНИЕ НОВЫХ ДАННЫХ")
    print("="*60)

    all_new = get_image_files(new_images_dir, extensions)
    print(f"Всего новых схем: {len(all_new)}")

    random.seed(seed)
    schemes = all_new.copy()
    random.shuffle(schemes)

    test_schemes = schemes[:args.test_count]
    remaining = schemes[args.test_count:]
    val_count = int(len(remaining) * args.val_ratio)
    val_schemes = remaining[:val_count]
    train_schemes = remaining[val_count:]

    print(f"  test={len(test_schemes)}, train={len(train_schemes)}, val={len(val_schemes)}")

    # ═══════════════════════════════════════════════════════════════
    # 2. Test (полноразмерные, без тайлинга)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 2: TEST")
    print("="*60)

    test_images_dir = work_dir / "test" / "images"
    test_images_dir.mkdir(parents=True, exist_ok=True)

    for img in test_schemes:
        binary = binarize_image(img, method="full")
        cv2.imwrite(str(test_images_dir / f"{img.stem}.png"), binary)
    print(f"Test: {len(test_schemes)} схем")

    # ═══════════════════════════════════════════════════════════════
    # 3. Препроцессинг
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 3: ПРЕПРОЦЕССИНГ")
    print("="*60)

    reindex_mapping = getattr(classes, 'reindex_mapping', None) or {}

    for split_name, split_schemes in [("train", train_schemes), ("val", val_schemes)]:
        print(f"\n--- {split_name} ---")
        out_images = work_dir / "preprocessed" / split_name / "images"
        out_labels = work_dir / "preprocessed" / split_name / "labels"
        out_images.mkdir(parents=True, exist_ok=True)
        out_labels.mkdir(parents=True, exist_ok=True)

        for img_path in split_schemes:
            try:
                binary = binarize_image(img_path, method="full")
            except ValueError as e:
                print(f"  Ошибка: {img_path} ({e})")
                continue

            cv2.imwrite(str(out_images / f"{img_path.stem}.png"), binary)

            label_path = new_labels_dir / f"{img_path.stem}.txt"
            if label_path.exists():
                filtered, _, _ = filter_labels(
                    label_path, classes.classes_to_remove, reindex_mapping
                )
                with open(out_labels / f"{img_path.stem}.txt", 'w') as f:
                    f.writelines(filtered)

        print(f"  Предобработано: {len(split_schemes)}")

    # ═══════════════════════════════════════════════════════════════
    # 4. Тайлинг
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 4: ТАЙЛИНГ")
    print("="*60)

    pipe_masks = None
    if hasattr(config.paths, 'pipe_masks'):
        p = Path(config.paths.pipe_masks)
        if p.exists():
            pipe_masks = p

    for split_name in ["train", "val"]:
        print(f"\n  Тайлинг {split_name}...")
        tile_dataset(
            images_dir=work_dir / "preprocessed" / split_name / "images",
            labels_dir=work_dir / "preprocessed" / split_name / "labels",
            output_dir=work_dir / "tiled" / split_name / "tiles",
            pipe_masks_dir=pipe_masks,
            annotation_masks_dir=None,
            tile_size=config.tiling.tile_size,
            overlap=config.tiling.overlap,
            min_visible_ratio=config.tiling.min_visible_ratio,
            keep_empty_ratio=config.tiling.keep_empty_ratio,
        )
        cnt = count_items(work_dir / "tiled" / split_name / "tiles" / "images")
        print(f"  Тайлов {split_name}: {cnt}")

    train_tiles_dir = work_dir / "tiled" / "train" / "tiles"

    # ═══════════════════════════════════════════════════════════════
    # 5. CPA (только train)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 5: CPA (train)")
    print("="*60)

    if config.augmentation.copy_paste.enabled:
        masks_dir = train_tiles_dir / "forbidden_masks"
        if not masks_dir.exists():
            masks_dir = None

        cpa = CopyPasteAugmentation(
            rare_classes=classes.rare_classes,
            critical_classes=classes.critical_classes,
            target_counts={
                "rare": classes.augmentation_targets.get("rare_classes", 150),
                "critical": classes.augmentation_targets.get("critical_classes", 200),
            }
        )
        cpa_output = work_dir / "augmented" / "cpa"
        cpa.augment(
            images_dir=train_tiles_dir / "images",
            labels_dir=train_tiles_dir / "labels",
            output_dir=cpa_output,
            masks_dir=masks_dir,
        )
        train_after_cpa = cpa_output
        print(f"  После CPA: {count_items(cpa_output / 'images')}")
    else:
        train_after_cpa = train_tiles_dir

    # ═══════════════════════════════════════════════════════════════
    # 6. Rotation (train only)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 6: ROTATION (train)")
    print("="*60)

    if config.augmentation.rotation.enabled:
        rotation = RotationAugmentation(
            mode="random_1",
            angles=config.augmentation.rotation.angles,
        )
        rot_output = work_dir / "augmented" / "rotated"
        rotation.augment(
            images_dir=train_after_cpa / "images",
            labels_dir=train_after_cpa / "labels",
            output_dir=rot_output,
        )
        train_final = rot_output
        print(f"  После rotation: {count_items(rot_output / 'images')}")
    else:
        train_final = train_after_cpa

    # ═══════════════════════════════════════════════════════════════
    # 7. Сборка датасета
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 7: СБОРКА ДАТАСЕТА")
    print("="*60)

    dataset = work_dir / "dataset"
    for d in [dataset / "train" / "images", dataset / "train" / "labels",
              dataset / "val" / "images", dataset / "val" / "labels"]:
        d.mkdir(parents=True, exist_ok=True)

    # Train
    copy_dir_contents(train_final / "images", dataset / "train" / "images", "*.png")
    copy_dir_contents(train_final / "labels", dataset / "train" / "labels", "*.txt")

    # Val (без аугментаций)
    val_tiles_dir = work_dir / "tiled" / "val" / "tiles"
    copy_dir_contents(val_tiles_dir / "images", dataset / "val" / "images", "*.png")
    copy_dir_contents(val_tiles_dir / "labels", dataset / "val" / "labels", "*.txt")

    # Test
    shutil.copytree(work_dir / "test", dataset / "test", dirs_exist_ok=True)

    train_cnt = count_items(dataset / "train" / "images")
    val_cnt = count_items(dataset / "val" / "images")
    test_cnt = count_items(dataset / "test" / "images")

    print(f"\nДатасет:")
    print(f"  Train: {train_cnt} тайлов")
    print(f"  Val:   {val_cnt} тайлов")
    print(f"  Test:  {test_cnt} полноразмерных схем")

    create_data_yaml(
        dataset_dir=dataset,
        num_classes=classes.num_classes,
        class_names=classes.class_names,
    )

    # Статистика
    stats = analyze_dataset(labels_dir=dataset / "train" / "labels", class_names=classes.class_names)
    print_statistics(stats, "Stage 2 Train")
    statistics_dir.mkdir(parents=True, exist_ok=True)
    save_statistics(stats, statistics_dir / "stage2_train_stats.csv")

    # ═══════════════════════════════════════════════════════════════
    # 8. Finetune
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*60)
    print("ЭТАП 8: FINETUNE")
    print("="*60)

    trainer = YOLOTrainer(config)
    results = trainer.finetune(
        weights=weights,
        data_yaml=dataset / "data.yaml",
        project=experiments_dir,
        name=args.name,
        lr0=args.lr,
        epochs=args.epochs,
        patience=args.patience,
    )

    print("\n" + "#"*70)
    print("#" + " "*15 + "STAGE 2 ЗАВЕРШЕН" + " "*16 + "#")
    print("#"*70)
    print(f"\n  Лучшие веса: {results['best_weights']}")
    print(f"  Эксперимент: {results['experiment_dir']}")


if __name__ == "__main__":
    main()
