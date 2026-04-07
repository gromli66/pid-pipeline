"""
Finetune Pipeline для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Адаптация модели к новому стилю схем.

АЛГОРИТМ:
---------
1. Берём ВСЕ старые схемы из raw → 2 test, остальные train/val
2. Берём ВСЕ новые схемы → 2 test, остальные train/val
3. Препроцессинг (бинаризация + удаление классов)
4. Тайлинг РАЗДЕЛЬНО для old и new
5. CPA РАЗДЕЛЬНО для old и new (только train)
6. Rotation РАЗДЕЛЬНО с разными режимами (только train):
   - old: "all" (4x) — компенсируем меньший размер схем
   - new: "random_1" (2x) — новые и так дают больше тайлов
   → целевой баланс ~35:65 (old:new)
7. Объединение old + new после rotation
8. Обучение (finetune или с нуля)
"""

import cv2
import shutil
import random
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Tuple

from pid_node_detection.data.preprocessing import (
    binarize_image, filter_labels, preprocess_images, remove_classes,
)
from pid_node_detection.data.tiling import tile_dataset
from pid_node_detection.data.statistics import analyze_dataset, print_statistics, save_statistics
from pid_node_detection.augmentation.copy_paste import CopyPasteAugmentation
from pid_node_detection.augmentation.rotation import RotationAugmentation
from pid_node_detection.training.trainer import YOLOTrainer, create_data_yaml


class FinetunePipeline:
    """Пайплайн адаптации модели к новому стилю схем."""

    def __init__(self, config: Any, classes: Any):
        self.config = config
        self.classes = classes
        self.work_dir = Path(config.paths.work_dir)
        self.dataset_dir = Path(config.paths.dataset_dir)
        self.experiments_dir = Path(config.paths.experiments_dir)
        self.statistics_dir = Path(config.paths.statistics_dir)

    # ─────────────────────────────────────────────────────────────────
    # Утилиты
    # ─────────────────────────────────────────────────────────────────

    def _get_image_files(self, images_dir: Path) -> List[Path]:
        extensions = self.config.preprocessing.image_extensions
        files = []
        for ext in extensions:
            files.extend(images_dir.glob(f"*{ext}"))
        return sorted(files)

    @staticmethod
    def _count_items(directory: Path, pattern: str = "*.png") -> int:
        if directory.exists():
            return len(list(directory.glob(pattern)))
        return 0

    @staticmethod
    def _copy_dir_contents(src_dir: Path, dst_dir: Path, pattern: str = "*"):
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in src_dir.glob(pattern):
            if f.is_file():
                shutil.copy(f, dst_dir / f.name)

    # ─────────────────────────────────────────────────────────────────
    # 1. Сбор и разбиение
    # ─────────────────────────────────────────────────────────────────

    def _collect_and_split(
        self, images_dir: Path, source_name: str,
        test_count: int, val_ratio: float, seed: int,
    ) -> Tuple[List[Path], List[Path], List[Path]]:
        """Собрать все схемы и разбить на test / train / val."""
        if not images_dir.exists():
            raise FileNotFoundError(f"Директория не найдена: {images_dir}")

        all_schemes = self._get_image_files(images_dir)
        print(f"\n{source_name}: всего {len(all_schemes)} схем")

        random.seed(seed)
        schemes = all_schemes.copy()
        random.shuffle(schemes)

        test_schemes = schemes[:test_count]
        remaining = schemes[test_count:]

        val_count = int(len(remaining) * val_ratio)
        val_schemes = remaining[:val_count]
        train_schemes = remaining[val_count:]

        print(f"  test={len(test_schemes)}, train={len(train_schemes)}, val={len(val_schemes)}")
        return train_schemes, val_schemes, test_schemes

    # ─────────────────────────────────────────────────────────────────
    # 2. Test
    # ─────────────────────────────────────────────────────────────────

    def _prepare_test(
        self, old_test: List[Path], new_test: List[Path],
        output_dir: Path, binarize_method: str,
    ) -> Path:
        """Бинаризация test-схем (полноразмерные, без тайлинга)."""
        print("\n" + "="*60)
        print("ПОДГОТОВКА TEST")
        print("="*60)

        test_images_dir = output_dir / "test" / "images"
        test_images_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        for img in old_test + new_test:
            try:
                binary = binarize_image(img, method=binarize_method)
                cv2.imwrite(str(test_images_dir / f"{img.stem}.png"), binary)
                count += 1
            except Exception as e:
                print(f"  Ошибка: {img.name} ({e})")

        print(f"Test: {count} схем (old={len(old_test)}, new={len(new_test)})")
        return output_dir / "test"

    # ─────────────────────────────────────────────────────────────────
    # 3. Препроцессинг
    # ─────────────────────────────────────────────────────────────────

    def _preprocess_schemes(
        self, schemes: List[Path], labels_dir: Path, output_dir: Path,
        prefix: str = "", binarize_method: str = "full",
    ) -> Path:
        """Бинаризация + удаление классов + reindex."""
        images_out = output_dir / "images"
        labels_out = output_dir / "labels"
        images_out.mkdir(parents=True, exist_ok=True)
        labels_out.mkdir(parents=True, exist_ok=True)

        reindex_mapping = getattr(self.classes, 'reindex_mapping', None) or {}

        processed = 0
        for img_path in schemes:
            try:
                binary = binarize_image(img_path, method=binarize_method)
            except ValueError as e:
                print(f"  Не удалось загрузить: {img_path} ({e})")
                continue

            out_name = f"{prefix}{img_path.stem}.png" if prefix else f"{img_path.stem}.png"
            cv2.imwrite(str(images_out / out_name), binary)

            label_path = labels_dir / f"{img_path.stem}.txt"
            if label_path.exists():
                out_label_name = f"{prefix}{img_path.stem}.txt" if prefix else f"{img_path.stem}.txt"
                filtered_lines, _, _ = filter_labels(
                    label_path, self.classes.classes_to_remove, reindex_mapping,
                )
                with open(labels_out / out_label_name, 'w') as f:
                    f.writelines(filtered_lines)
            processed += 1

        print(f"  Предобработано: {processed}/{len(schemes)}")
        return output_dir

    # ─────────────────────────────────────────────────────────────────
    # 4. Тайлинг
    # ─────────────────────────────────────────────────────────────────

    def _tile(self, preprocessed_dir: Path, output_dir: Path, stage_name: str) -> Path:
        """Нарезка на тайлы."""
        print(f"\n  Тайлинг {stage_name}...")
        tiles_dir = output_dir / "tiles"

        pipe_masks = None
        if hasattr(self.config.paths, 'pipe_masks'):
            pipe_masks_path = Path(self.config.paths.pipe_masks)
            if pipe_masks_path.exists():
                pipe_masks = pipe_masks_path

        tile_dataset(
            images_dir=preprocessed_dir / "images",
            labels_dir=preprocessed_dir / "labels",
            output_dir=tiles_dir,
            pipe_masks_dir=pipe_masks,
            annotation_masks_dir=None,
            tile_size=self.config.tiling.tile_size,
            overlap=self.config.tiling.overlap,
            min_visible_ratio=self.config.tiling.min_visible_ratio,
            keep_empty_ratio=self.config.tiling.keep_empty_ratio
        )

        count = self._count_items(tiles_dir / "images")
        print(f"  Тайлов: {count}")
        return tiles_dir

    # ─────────────────────────────────────────────────────────────────
    # 5. CPA
    # ─────────────────────────────────────────────────────────────────

    def _apply_cpa(self, tiles_dir: Path, output_dir: Path, stage_name: str) -> Path:
        """Copy-Paste Augmentation. Возвращает tiles_dir если выключено."""
        if not self.config.augmentation.copy_paste.enabled:
            return tiles_dir

        print(f"\n  CPA {stage_name}...")

        masks_dir = tiles_dir / "forbidden_masks"
        if not masks_dir.exists():
            masks_dir = None

        cpa = CopyPasteAugmentation(
            rare_classes=self.classes.rare_classes,
            critical_classes=self.classes.critical_classes,
            target_counts={
                "rare": self.classes.augmentation_targets.get("rare_classes", 150),
                "critical": self.classes.augmentation_targets.get("critical_classes", 200)
            }
        )

        cpa_output = output_dir / "cpa"
        cpa.augment(
            images_dir=tiles_dir / "images",
            labels_dir=tiles_dir / "labels",
            output_dir=cpa_output,
            masks_dir=masks_dir
        )

        count = self._count_items(cpa_output / "images")
        print(f"  После CPA: {count}")
        return cpa_output

    # ─────────────────────────────────────────────────────────────────
    # 6. Rotation
    # ─────────────────────────────────────────────────────────────────

    def _apply_rotation(
        self, data_dir: Path, output_dir: Path,
        stage_name: str, rotation_mode: str = "all",
    ) -> Path:
        """Rotation Augmentation. Возвращает data_dir если выключено."""
        if not self.config.augmentation.rotation.enabled:
            return data_dir

        print(f"\n  Rotation {stage_name} (mode={rotation_mode})...")

        rotation = RotationAugmentation(
            mode=rotation_mode,
            angles=self.config.augmentation.rotation.angles
        )

        rotation_output = output_dir / "rotated"
        rotation.augment(
            images_dir=data_dir / "images",
            labels_dir=data_dir / "labels",
            output_dir=rotation_output
        )

        count = self._count_items(rotation_output / "images")
        print(f"  После rotation: {count}")
        return rotation_output

    # ─────────────────────────────────────────────────────────────────
    # Баланс
    # ─────────────────────────────────────────────────────────────────

    def _log_balance(self, old_count: int, new_count: int, stage: str = "train") -> Dict:
        total = old_count + new_count
        if total == 0:
            print(f"\n  ⚠ {stage}: нет тайлов!")
            return {"old": 0, "new": 0, "total": 0, "old_pct": 0, "new_pct": 0}

        old_pct = old_count / total * 100
        new_pct = new_count / total * 100

        print(f"\n  === Баланс {stage} ===")
        print(f"  Old: {old_count:>6} тайлов ({old_pct:.1f}%)")
        print(f"  New: {new_count:>6} тайлов ({new_pct:.1f}%)")
        print(f"  Total: {total:>4} тайлов")

        if old_pct < 20 or old_pct > 60:
            print(f"  ⚠ Перекос! Целевой баланс ~35:65 (old:new)")

        return {
            "old": old_count, "new": new_count, "total": total,
            "old_pct": old_pct, "new_pct": new_pct,
        }

    # ─────────────────────────────────────────────────────────────────
    # Основной пайплайн
    # ─────────────────────────────────────────────────────────────────

    def run(
        self,
        weights: Optional[Union[str, Path]] = None,
        experiment_name: str = "finetune",
        new_images_dir: Optional[Union[str, Path]] = None,
        new_labels_dir: Optional[Union[str, Path]] = None,
        binarize_method: str = "full",
        old_rotation_mode: str = "all",
        new_rotation_mode: str = "random_1",
    ) -> Dict:
        """
        Запуск пайплайна.

        Args:
            weights: Путь к весам. None = обучение с нуля.
            experiment_name: Имя эксперимента
            new_images_dir: Путь к новым изображениям (override)
            new_labels_dir: Путь к новым лейблам (override)
            binarize_method: "full" / "otsu"
            old_rotation_mode: rotation для старых ("all"=4x)
            new_rotation_mode: rotation для новых ("random_1"=2x)
        """
        print("\n" + "#"*70)
        print("#" + " "*20 + "FINETUNE PIPELINE" + " "*21 + "#")
        print("#"*70)

        # --- Пути ---
        if weights is None:
            weights = getattr(self.config.finetune, 'weights', None)
        if weights is not None:
            weights = Path(weights)
            if not weights.exists():
                raise FileNotFoundError(f"Веса не найдены: {weights}")

        if new_images_dir is not None:
            self.config.finetune.new_images = str(new_images_dir)
        if new_labels_dir is not None:
            self.config.finetune.new_labels = str(new_labels_dir)

        old_images_dir = Path(self.config.finetune.old_images)
        old_labels_dir = Path(self.config.finetune.old_labels)
        new_images_dir = Path(self.config.finetune.new_images)
        new_labels_dir = Path(self.config.finetune.new_labels)

        val_ratio = self.config.finetune.val_ratio
        test_count = self.config.finetune.test_count_per_source
        seed = self.config.splitting.random_seed

        print(f"\nПараметры:")
        print(f"  Модель: {self.config.training.model}")
        print(f"  Веса: {weights or 'с нуля'}")
        print(f"  Old: {old_images_dir}")
        print(f"  New: {new_images_dir}")
        print(f"  Val ratio: {val_ratio}")
        print(f"  Test per source: {test_count}")
        print(f"  LR: {self.config.finetune.lr0}")
        print(f"  Бинаризация: {binarize_method}")
        print(f"  Rotation old: {old_rotation_mode}, new: {new_rotation_mode}")

        results = {}
        finetune_work = self.work_dir / "finetune"
        finetune_work.mkdir(parents=True, exist_ok=True)

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 1: Сбор данных и разбиение
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 1: СБОР ДАННЫХ И РАЗБИЕНИЕ")
        print("="*60)

        old_train, old_val, old_test = self._collect_and_split(
            old_images_dir, "Old", test_count, val_ratio, seed
        )
        new_train, new_val, new_test = self._collect_and_split(
            new_images_dir, "New", test_count, val_ratio, seed + 1
        )

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 2: Test
        # ═══════════════════════════════════════════════════════════════
        test_dir = self._prepare_test(old_test, new_test, finetune_work, binarize_method)
        results["test_dir"] = test_dir

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 3: Препроцессинг
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 3: ПРЕПРОЦЕССИНГ")
        print("="*60)

        print("\n--- Old train ---")
        old_train_prep = self._preprocess_schemes(
            old_train, old_labels_dir,
            finetune_work / "preprocessed" / "old_train",
            prefix="old_", binarize_method=binarize_method,
        )
        print("\n--- Old val ---")
        old_val_prep = self._preprocess_schemes(
            old_val, old_labels_dir,
            finetune_work / "preprocessed" / "old_val",
            prefix="old_", binarize_method=binarize_method,
        )
        print("\n--- New train ---")
        new_train_prep = self._preprocess_schemes(
            new_train, new_labels_dir,
            finetune_work / "preprocessed" / "new_train",
            prefix="new_", binarize_method=binarize_method,
        )
        print("\n--- New val ---")
        new_val_prep = self._preprocess_schemes(
            new_val, new_labels_dir,
            finetune_work / "preprocessed" / "new_val",
            prefix="new_", binarize_method=binarize_method,
        )

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 4: Тайлинг
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 4: ТАЙЛИНГ")
        print("="*60)

        old_train_tiles = self._tile(old_train_prep, finetune_work / "tiled" / "old_train", "old_train")
        new_train_tiles = self._tile(new_train_prep, finetune_work / "tiled" / "new_train", "new_train")
        old_val_tiles = self._tile(old_val_prep, finetune_work / "tiled" / "old_val", "old_val")
        new_val_tiles = self._tile(new_val_prep, finetune_work / "tiled" / "new_val", "new_val")

        self._log_balance(
            self._count_items(old_train_tiles / "images"),
            self._count_items(new_train_tiles / "images"),
            "train (до аугментаций)"
        )

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 5: CPA (только train)
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 5: COPY-PASTE AUGMENTATION (train)")
        print("="*60)

        old_train_after_cpa = self._apply_cpa(
            old_train_tiles, finetune_work / "augmented" / "old_train", "old_train"
        )
        new_train_after_cpa = self._apply_cpa(
            new_train_tiles, finetune_work / "augmented" / "new_train", "new_train"
        )

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 6: Rotation (раздельные режимы, только train)
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 6: ROTATION (train)")
        print("="*60)

        old_train_final = self._apply_rotation(
            old_train_after_cpa, finetune_work / "augmented" / "old_train",
            "old_train", rotation_mode=old_rotation_mode,
        )
        new_train_final = self._apply_rotation(
            new_train_after_cpa, finetune_work / "augmented" / "new_train",
            "new_train", rotation_mode=new_rotation_mode,
        )

        # Финальный баланс
        old_final_count = self._count_items(old_train_final / "images")
        new_final_count = self._count_items(new_train_final / "images")
        balance = self._log_balance(old_final_count, new_final_count, "train (финальный)")
        results["train_balance"] = balance

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 7: Сборка финального датасета
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 7: СБОРКА ФИНАЛЬНОГО ДАТАСЕТА")
        print("="*60)

        final_dataset = finetune_work / "dataset"
        final_train = final_dataset / "train"
        final_val = final_dataset / "val"

        for d in [final_train / "images", final_train / "labels",
                  final_val / "images", final_val / "labels"]:
            d.mkdir(parents=True, exist_ok=True)

        # Train: old + new
        for src in [old_train_final, new_train_final]:
            self._copy_dir_contents(src / "images", final_train / "images", "*.png")
            self._copy_dir_contents(src / "labels", final_train / "labels", "*.txt")

        # Val: old + new (без аугментаций)
        for src in [old_val_tiles, new_val_tiles]:
            self._copy_dir_contents(src / "images", final_val / "images", "*.png")
            self._copy_dir_contents(src / "labels", final_val / "labels", "*.txt")

        # Test
        shutil.copytree(test_dir, final_dataset / "test", dirs_exist_ok=True)

        train_tiles = self._count_items(final_train / "images")
        val_tiles = self._count_items(final_val / "images")
        test_images = self._count_items(final_dataset / "test" / "images")

        print(f"\nФинальный датасет:")
        print(f"  Train: {train_tiles} тайлов")
        print(f"  Val:   {val_tiles} тайлов")
        print(f"  Test:  {test_images} полноразмерных схем")

        create_data_yaml(
            dataset_dir=final_dataset,
            num_classes=self.classes.num_classes,
            class_names=self.classes.class_names
        )
        results["dataset_dir"] = final_dataset

        # Статистика
        stats = analyze_dataset(
            labels_dir=final_train / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "Train после аугментации")
        self.statistics_dir.mkdir(parents=True, exist_ok=True)
        save_statistics(stats, self.statistics_dir / "finetune_train_stats.csv")

        val_balance = self._log_balance(
            self._count_items(old_val_tiles / "images"),
            self._count_items(new_val_tiles / "images"), "val"
        )
        results["val_balance"] = val_balance

        # ═══════════════════════════════════════════════════════════════
        # ЭТАП 8: Обучение
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "="*60)
        print("ЭТАП 8: ОБУЧЕНИЕ")
        print("="*60)

        trainer = YOLOTrainer(self.config)

        if weights is not None:
            training_results = trainer.finetune(
                weights=weights,
                data_yaml=final_dataset / "data.yaml",
                project=self.experiments_dir,
                name=experiment_name,
                lr0=self.config.finetune.lr0,
                epochs=self.config.finetune.epochs,
                patience=self.config.finetune.patience
            )
        else:
            training_results = trainer.train(
                data_yaml=final_dataset / "data.yaml",
                project=self.experiments_dir,
                name=experiment_name,
            )

        results["training"] = training_results
        results["experiment_dir"] = self.experiments_dir / experiment_name

        # ═══════════════════════════════════════════════════════════════
        # Завершение
        # ═══════════════════════════════════════════════════════════════
        print("\n" + "#"*70)
        print("#" + " "*18 + "FINETUNE ЗАВЕРШЕН" + " "*19 + "#")
        print("#"*70)

        print(f"\nРезультаты:")
        print(f"  Эксперимент: {results['experiment_dir']}")
        print(f"  Датасет: {results['dataset_dir']}")
        print(f"  Test: {results['dataset_dir'] / 'test'}")
        print(f"  Лучшие веса: {results['experiment_dir'] / 'weights' / 'best.pt'}")
        print(f"  Баланс train: old {balance['old_pct']:.1f}% / new {balance['new_pct']:.1f}%")

        return results