"""
Ensemble Train Pipeline для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Обучение нескольких YOLO моделей на разных размерах тайлов
для последующего ансамблевого инференса.

ИДЕЯ:
-----
P&ID схемы содержат объекты разного масштаба. Одна модель на одном
tile_size — это компромисс. Ансамбль из моделей на разных tile_size
позволяет каждой модели специализироваться:

  tile_size=640:
    - Объекты занимают больше площади тайла
    - Модель лучше видит мелкие детали
    - Больше тайлов → больше обучающих примеров
    - Меньше контекста → может путать похожие объекты

  tile_size=1280 (baseline):
    - Сбалансированный вариант
    - Оптимально для большинства объектов на P&ID 300 DPI

  tile_size=2048:
    - Больше контекста (видит соседние объекты/трубы)
    - Лучше для крупных объектов (сосуды, колонны)
    - Меньше тайлов → меньше обучающих примеров
    - YOLO всё равно ресайзит до imgsz, но видит больше контекста

ПАЙПЛАЙН ДЛЯ КАЖДОГО tile_size:
1. Предобработка (общая, одна на всех)
2. Выделение test (общий, один на всех)
3. Тайлинг с конкретным tile_size
4. Split train/val
5. Copy-Paste аугментация
6. Rotation аугментация
7. Обучение YOLO (imgsz = min(tile_size, 1280))

ИСПОЛЬЗОВАНИЕ:
-------------
    # Через CLI:
    python -m pid_node_detection ensemble-train --config config.yaml \\
        --tile-sizes 640 1280 2048 \\
        --name ensemble_v1

    # Через Python:
    from pid_node_detection.pipelines.ensemble_pipeline import EnsembleTrainPipeline
    from pid_node_detection.config import load_config, load_classes
    
    config = load_config("config.yaml")
    classes = load_classes()
    
    pipeline = EnsembleTrainPipeline(config, classes)
    results = pipeline.run(
        tile_sizes=[640, 1280, 2048],
        experiment_name="ensemble_v1"
    )
"""

import shutil
import copy
from pathlib import Path
from typing import Dict, Optional, Any, List

from pid_node_detection.data.preprocessing import preprocess_pipeline
from pid_node_detection.data.splitting import split_data, stratified_split_tiles
from pid_node_detection.data.tiling import tile_dataset
from pid_node_detection.data.statistics import analyze_dataset, print_statistics, save_statistics
from pid_node_detection.augmentation.copy_paste import CopyPasteAugmentation
from pid_node_detection.augmentation.rotation import RotationAugmentation
from pid_node_detection.training.trainer import YOLOTrainer, create_data_yaml


# Рекомендации по параметрам для каждого tile_size
TILE_SIZE_PRESETS = {
    640: {
        "imgsz": 640,
        "overlap": 0.3,       # Больше overlap для мелких тайлов
        "min_visible_ratio": 0.5,  # Мягче, т.к. объекты чаще обрезаются
        "keep_empty_ratio": 0.05,  # Много тайлов, меньше пустых
        "batch_size": 16,     # Мелкие тайлы → больше batch
        "sahi_overlap": 0.3,
    },
    960: {
        "imgsz": 960,
        "overlap": 0.25,
        "min_visible_ratio": 0.55,
        "keep_empty_ratio": 0.08,
        "batch_size": 8,
        "sahi_overlap": 0.25,
    },
    1280: {
        "imgsz": 1280,
        "overlap": 0.25,
        "min_visible_ratio": 0.6,
        "keep_empty_ratio": 0.1,
        "batch_size": 4,
        "sahi_overlap": 0.25,
    },
    1920: {
        "imgsz": 1280,  # YOLO ресайзит до 1280
        "overlap": 0.2,
        "min_visible_ratio": 0.6,
        "keep_empty_ratio": 0.1,
        "batch_size": 4,
        "sahi_overlap": 0.2,
    },
    2048: {
        "imgsz": 1280,  # YOLO ресайзит до 1280
        "overlap": 0.2,
        "min_visible_ratio": 0.65,
        "keep_empty_ratio": 0.15,
        "batch_size": 2,       # Крупные тайлы → маленький batch
        "sahi_overlap": 0.2,
    },
}


def get_tile_preset(tile_size: int) -> Dict:
    """
    Получить пресет параметров для данного tile_size.
    
    Если точного совпадения нет, интерполирует из ближайших.
    """
    if tile_size in TILE_SIZE_PRESETS:
        return TILE_SIZE_PRESETS[tile_size].copy()
    
    # Найти ближайший
    sizes = sorted(TILE_SIZE_PRESETS.keys())
    
    if tile_size <= sizes[0]:
        return TILE_SIZE_PRESETS[sizes[0]].copy()
    if tile_size >= sizes[-1]:
        preset = TILE_SIZE_PRESETS[sizes[-1]].copy()
        preset["imgsz"] = min(tile_size, 1280)
        return preset
    
    # Интерполяция из ближайшего меньшего
    for i in range(len(sizes) - 1):
        if sizes[i] <= tile_size < sizes[i + 1]:
            preset = TILE_SIZE_PRESETS[sizes[i]].copy()
            preset["imgsz"] = min(tile_size, 1280)
            return preset
    
    # Fallback
    return TILE_SIZE_PRESETS[1280].copy()


class EnsembleTrainPipeline:
    """
    Пайплайн обучения ансамбля моделей на разных tile_size.
    
    Общие шаги (выполняются один раз):
    - Предобработка
    - Выделение test набора
    
    Шаги для каждого tile_size:
    - Тайлинг
    - Split train/val
    - CPA
    - Rotation
    - Обучение YOLO
    """
    
    def __init__(self, config: Any, classes: Any):
        """
        Args:
            config: Config из load_config()
            classes: ClassesConfig из load_classes()
        """
        self.config = config
        self.classes = classes
        self.work_dir = Path(config.paths.work_dir)
        self.dataset_dir = Path(config.paths.dataset_dir)
        self.experiments_dir = Path(config.paths.experiments_dir)
        self.statistics_dir = Path(config.paths.statistics_dir)
        
        # Создать директории
        for d in [self.work_dir, self.dataset_dir, 
                  self.experiments_dir, self.statistics_dir]:
            d.mkdir(parents=True, exist_ok=True)
    
    def _shared_preprocess(self) -> Path:
        """Общая предобработка (один раз для всех tile_size)."""
        print("\n" + "=" * 70)
        print("ОБЩИЙ ШАГ: ПРЕДОБРАБОТКА")
        print("=" * 70)
        
        cleaned_dir = self.work_dir / "cleaned"
        
        # Пропустить если уже готово
        if cleaned_dir.exists() and (cleaned_dir / "images").exists():
            img_count = len(list((cleaned_dir / "images").glob("*.png")))
            if img_count > 0:
                print(f"\n*** ПРЕДОБРАБОТКА УЖЕ ВЫПОЛНЕНА: {img_count} изображений ***")
                return cleaned_dir
        
        raw_images = Path(self.config.paths.raw_images)
        raw_labels = Path(self.config.paths.raw_labels)
        reindex_mapping = getattr(self.classes, 'reindex_mapping', None)
        
        cleaned_dir = preprocess_pipeline(
            raw_images_dir=raw_images,
            raw_labels_dir=raw_labels,
            output_dir=self.work_dir,
            classes_to_remove=self.classes.classes_to_remove,
            reindex_mapping=reindex_mapping,
            grayscale=self.config.preprocessing.grayscale
        )
        
        stats = analyze_dataset(
            labels_dir=cleaned_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "После предобработки")
        save_statistics(stats, self.statistics_dir / "ensemble_01_preprocessing.csv")
        
        return cleaned_dir
    
    def _shared_split_test(self, cleaned_dir: Path) -> Dict[str, Path]:
        """Общее выделение test набора."""
        print("\n" + "=" * 70)
        print("ОБЩИЙ ШАГ: ВЫДЕЛЕНИЕ TEST")
        print("=" * 70)
        
        test_dir = self.dataset_dir / "test"
        
        if test_dir.exists() and (test_dir / "images").exists():
            test_count = len(list((test_dir / "images").glob("*.png")))
            if test_count > 0:
                print(f"\n*** TEST УЖЕ СУЩЕСТВУЕТ: {test_count} схем ***")
                
                trainval_dir = self.work_dir / "trainval"
                trainval_images = trainval_dir / "images"
                trainval_labels = trainval_dir / "labels"
                trainval_images.mkdir(parents=True, exist_ok=True)
                trainval_labels.mkdir(parents=True, exist_ok=True)
                
                test_names = {f.stem for f in (test_dir / "images").glob("*.png")}
                
                for img in (cleaned_dir / "images").glob("*.png"):
                    if img.stem not in test_names:
                        shutil.copy(img, trainval_images / img.name)
                        label = cleaned_dir / "labels" / f"{img.stem}.txt"
                        if label.exists():
                            shutil.copy(label, trainval_labels / f"{img.stem}.txt")
                
                return {"test": test_dir, "trainval": trainval_dir}
        
        print("\n*** СОЗДАНИЕ НОВОГО TEST НАБОРА ***")
        
        split_result = split_data(
            images_dir=cleaned_dir / "images",
            labels_dir=cleaned_dir / "labels",
            output_dir=self.work_dir / "split",
            test_ratio=self.config.splitting.test_ratio,
            rare_classes=self.classes.rare_classes,
            random_seed=self.config.splitting.random_seed
        )
        
        if test_dir.exists():
            shutil.rmtree(test_dir)
        shutil.copytree(split_result["test"], test_dir)
        
        stats = analyze_dataset(
            labels_dir=test_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "TEST (ФИКСИРОВАННЫЙ)")
        save_statistics(stats, self.statistics_dir / "ensemble_02_test.csv")
        
        return {"test": test_dir, "trainval": split_result["trainval"]}
    
    def _train_single_tile_size(
        self,
        tile_size: int,
        trainval_dir: Path,
        experiment_name: str,
        preset: Dict,
        skip_training: bool = False
    ) -> Dict:
        """
        Полный пайплайн для одного tile_size.
        
        Args:
            tile_size: Размер тайла
            trainval_dir: Путь к trainval данным
            experiment_name: Имя эксперимента
            preset: Пресет параметров для этого tile_size
            skip_training: Пропустить обучение
            
        Returns:
            {dataset_dir, train_final, val_dir, training_results}
        """
        tag = f"tile{tile_size}"
        work_sub = self.work_dir / tag
        dataset_sub = self.dataset_dir / f"ensemble_{tag}"
        
        print(f"\n{'#' * 70}")
        print(f"# ОБУЧЕНИЕ: tile_size={tile_size}")
        print(f"# imgsz={preset['imgsz']}, batch={preset['batch_size']}")
        print(f"# overlap={preset['overlap']}, min_visible={preset['min_visible_ratio']}")
        print(f"{'#' * 70}")
        
        # --- Проверка: данные уже подготовлены? ---
        ds_train = dataset_sub / "train"
        ds_val = dataset_sub / "val"
        data_yaml = dataset_sub / "data.yaml"
        
        if ds_train.exists() and ds_val.exists() and data_yaml.exists():
            train_count = len(list((ds_train / "images").glob("*.png")))
            val_count = len(list((ds_val / "images").glob("*.png")))
            if train_count > 0 and val_count > 0:
                print(f"\n*** ДАННЫЕ УЖЕ ГОТОВЫ: train={train_count}, val={val_count} ***")
                
                # Проверить: модель уже обучена?
                exp_name = f"{experiment_name}_{tag}"
                best_weights = self.experiments_dir / exp_name / "weights" / "best.pt"
                
                if best_weights.exists():
                    print(f"*** МОДЕЛЬ УЖЕ ОБУЧЕНА: {best_weights} ***")
                    print(f"*** Пропускаем полностью ***")
                    return {
                        "dataset_dir": dataset_sub,
                        "train_final": ds_train,
                        "val_dir": ds_val,
                        "tile_size": tile_size,
                        "preset": preset,
                        "best_weights": best_weights,
                    }
                
                print(f"*** Пропускаем тайлинг/CPA/rotation, переходим к обучению ***")
                
                result = {
                    "dataset_dir": dataset_sub,
                    "train_final": ds_train,
                    "val_dir": ds_val,
                    "tile_size": tile_size,
                    "preset": preset,
                }
                
                if not skip_training:
                    print(f"\n{'=' * 60}")
                    print(f"ОБУЧЕНИЕ YOLO ({tag}, imgsz={preset['imgsz']})")
                    print(f"{'=' * 60}")
                    
                    exp_name = f"{experiment_name}_{tag}"
                    
                    trainer = YOLOTrainer(
                        config=None,
                        model=self.config.training.model,
                        epochs=self.config.training.epochs,
                        batch_size=preset["batch_size"],
                        img_size=preset["imgsz"],
                        patience=self.config.training.patience,
                        optimizer=self.config.training.optimizer,
                        lr0=self.config.training.lr0,
                        lrf=self.config.training.lrf,
                        weight_decay=self.config.training.weight_decay,
                        loss_weights={
                            "box": self.config.training.loss_weights.box,
                            "cls": self.config.training.loss_weights.cls,
                            "dfl": self.config.training.loss_weights.dfl,
                        },
                        device=self.config.training.device,
                        save_period=self.config.training.save_period,
                    )
                    
                    ba = getattr(self.config.training, 'builtin_augmentation', None)
                    if ba is not None and getattr(ba, 'enabled', False):
                        for key in trainer.augmentation_params:
                            if hasattr(ba, key):
                                trainer.augmentation_params[key] = float(getattr(ba, key))
                    
                    training_results = trainer.train(
                        data_yaml=data_yaml,
                        project=self.experiments_dir,
                        name=exp_name
                    )
                    
                    result["training_results"] = training_results
                    result["best_weights"] = training_results["best_weights"]
                
                return result
        
        # --- Тайлинг ---
        print(f"\n{'=' * 60}")
        print(f"ТАЙЛИНГ (tile_size={tile_size})")
        print(f"{'=' * 60}")
        
        tiles_dir = work_sub / "tiles"
        
        pipe_masks = None
        annotation_masks = None
        
        if hasattr(self.config.paths, 'pipe_masks'):
            pipe_masks_path = Path(self.config.paths.pipe_masks)
            if pipe_masks_path.exists():
                pipe_masks = pipe_masks_path
        
        if hasattr(self.config.paths, 'annotation_masks'):
            ann_masks_path = Path(self.config.paths.annotation_masks)
            if ann_masks_path.exists():
                annotation_masks = ann_masks_path
        
        tile_dataset(
            images_dir=trainval_dir / "images",
            labels_dir=trainval_dir / "labels",
            output_dir=tiles_dir,
            pipe_masks_dir=pipe_masks,
            annotation_masks_dir=annotation_masks,
            tile_size=tile_size,
            overlap=preset["overlap"],
            min_visible_ratio=preset["min_visible_ratio"],
            keep_empty_ratio=preset["keep_empty_ratio"],
            dilation_kernel=self.config.tiling.forbidden_mask_dilation
        )
        
        stats = analyze_dataset(
            labels_dir=tiles_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, f"После тайлинга ({tag})")
        save_statistics(stats, self.statistics_dir / f"ensemble_{tag}_03_tiling.csv")
        
        # --- Split train/val ---
        print(f"\n{'=' * 60}")
        print(f"SPLIT TRAIN/VAL ({tag})")
        print(f"{'=' * 60}")
        
        train_val = stratified_split_tiles(
            tiles_dir=tiles_dir,
            output_dir=work_sub / "train_val_split",
            val_ratio=self.config.splitting.val_ratio,
            random_seed=self.config.splitting.random_seed
        )
        
        # --- CPA ---
        train_dir = train_val["train"]
        
        if self.config.augmentation.copy_paste.enabled:
            print(f"\n{'=' * 60}")
            print(f"COPY-PASTE ({tag})")
            print(f"{'=' * 60}")
            
            cpa_output = work_sub / "train_cpa"
            masks_dir = train_dir / "forbidden_masks"
            if not masks_dir.exists():
                masks_dir = None
            
            cpa = CopyPasteAugmentation(
                rare_classes=self.classes.rare_classes,
                critical_classes=self.classes.critical_classes,
                target_counts={
                    "rare": self.classes.augmentation_targets.get("rare_classes", 150),
                    "critical": self.classes.augmentation_targets.get("critical_classes", 200)
                },
                max_attempts=self.config.augmentation.copy_paste.max_attempts,
                forbidden_threshold=self.config.augmentation.copy_paste.forbidden_threshold,
                min_object_size=self.config.augmentation.copy_paste.min_object_size
            )
            
            cpa.augment(
                images_dir=train_dir / "images",
                labels_dir=train_dir / "labels",
                output_dir=cpa_output,
                masks_dir=masks_dir,
                create_synthetic_tiles=self.config.augmentation.copy_paste.synthetic_tiles.enabled,
                max_synthetic_tiles=self.config.augmentation.copy_paste.synthetic_tiles.max_tiles
            )
            train_dir = cpa_output
        
        # --- Rotation ---
        if self.config.augmentation.rotation.enabled:
            print(f"\n{'=' * 60}")
            print(f"ROTATION ({tag})")
            print(f"{'=' * 60}")
            
            rotation_output = work_sub / "train_final"
            
            rotation = RotationAugmentation(
                mode=self.config.augmentation.rotation.mode,
                angles=self.config.augmentation.rotation.angles
            )
            
            rotation.augment(
                images_dir=train_dir / "images",
                labels_dir=train_dir / "labels",
                output_dir=rotation_output
            )
            train_dir = rotation_output
        
        stats = analyze_dataset(
            labels_dir=train_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, f"Финальный train ({tag})")
        save_statistics(stats, self.statistics_dir / f"ensemble_{tag}_06_train_final.csv")
        
        # --- Подготовка датасета ---
        print(f"\n{'=' * 60}")
        print(f"ПОДГОТОВКА ДАТАСЕТА ({tag})")
        print(f"{'=' * 60}")
        
        ds_train = dataset_sub / "train"
        ds_val = dataset_sub / "val"
        
        if ds_train.exists():
            shutil.rmtree(ds_train)
        if ds_val.exists():
            shutil.rmtree(ds_val)
        
        shutil.copytree(train_dir / "images", ds_train / "images")
        shutil.copytree(train_dir / "labels", ds_train / "labels")
        shutil.copytree(train_val["val"] / "images", ds_val / "images")
        shutil.copytree(train_val["val"] / "labels", ds_val / "labels")
        
        create_data_yaml(
            dataset_dir=dataset_sub,
            num_classes=self.classes.num_classes,
            class_names=self.classes.class_names
        )
        
        print(f"Train: {len(list((ds_train / 'images').glob('*.png')))} файлов")
        print(f"Val: {len(list((ds_val / 'images').glob('*.png')))} файлов")
        
        result = {
            "dataset_dir": dataset_sub,
            "train_final": train_dir,
            "val_dir": train_val["val"],
            "tile_size": tile_size,
            "preset": preset,
        }
        
        # --- Обучение ---
        if not skip_training:
            print(f"\n{'=' * 60}")
            print(f"ОБУЧЕНИЕ YOLO ({tag}, imgsz={preset['imgsz']})")
            print(f"{'=' * 60}")
            
            exp_name = f"{experiment_name}_{tag}"
            
            trainer = YOLOTrainer(
                config=None,  # НЕ передаём config, чтобы он не перезаписал наши параметры
                model=self.config.training.model,
                epochs=self.config.training.epochs,
                batch_size=preset["batch_size"],       # ← из пресета
                img_size=preset["imgsz"],               # ← из пресета
                patience=self.config.training.patience,
                optimizer=self.config.training.optimizer,
                lr0=self.config.training.lr0,
                lrf=self.config.training.lrf,
                weight_decay=self.config.training.weight_decay,
                loss_weights={
                    "box": self.config.training.loss_weights.box,
                    "cls": self.config.training.loss_weights.cls,
                    "dfl": self.config.training.loss_weights.dfl,
                },
                device=self.config.training.device,
                save_period=self.config.training.save_period,
            )
            
            # Применить builtin_augmentation из конфига
            ba = getattr(self.config.training, 'builtin_augmentation', None)
            if ba is not None and getattr(ba, 'enabled', False):
                for key in trainer.augmentation_params:
                    if hasattr(ba, key):
                        trainer.augmentation_params[key] = float(getattr(ba, key))
            
            training_results = trainer.train(
                data_yaml=dataset_sub / "data.yaml",
                project=self.experiments_dir,
                name=exp_name
            )
            
            result["training_results"] = training_results
            result["best_weights"] = training_results["best_weights"]
        
        return result
    
    def run(
        self,
        tile_sizes: List[int] = [640, 1280, 2048],
        experiment_name: str = "ensemble",
        model_weights: Optional[List[float]] = None,
        skip_training: bool = False,
        custom_presets: Optional[Dict[int, Dict]] = None
    ) -> Dict:
        """
        Запуск полного пайплайна ансамбля.
        
        Args:
            tile_sizes: Список размеров тайлов для обучения
            experiment_name: Имя эксперимента
            model_weights: Веса моделей в ансамбле [w1, w2, ...] 
                          (по умолчанию все 1.0)
            skip_training: Пропустить обучение
            custom_presets: Пользовательские пресеты {tile_size: {param: val}}
            
        Returns:
            {
                "models": [{tile_size, weights, weight, preset}, ...],
                "test_dir": Path,
                "ensemble_config": dict для EnsembleDetector
            }
        """
        print("\n" + "#" * 70)
        print("#" + " " * 15 + "ENSEMBLE TRAIN PIPELINE" + " " * 15 + "#")
        print("#" * 70)
        print(f"\nТайл-размеры: {tile_sizes}")
        print(f"Эксперимент: {experiment_name}")
        
        if model_weights is None:
            model_weights = [1.0] * len(tile_sizes)
        
        assert len(model_weights) == len(tile_sizes), \
            f"Количество весов ({len(model_weights)}) != количество tile_sizes ({len(tile_sizes)})"
        
        # === Общие шаги ===
        cleaned_dir = self._shared_preprocess()
        split_result = self._shared_split_test(cleaned_dir)
        trainval_dir = split_result["trainval"]
        test_dir = split_result["test"]
        
        # === Обучение для каждого tile_size ===
        models_info = []
        
        for tile_size, weight in zip(tile_sizes, model_weights):
            # Получить пресет
            preset = get_tile_preset(tile_size)
            
            # Перезаписать кастомными настройками
            if custom_presets and tile_size in custom_presets:
                preset.update(custom_presets[tile_size])
            
            result = self._train_single_tile_size(
                tile_size=tile_size,
                trainval_dir=trainval_dir,
                experiment_name=experiment_name,
                preset=preset,
                skip_training=skip_training
            )
            
            models_info.append({
                "tile_size": tile_size,
                "weight": weight,
                "preset": preset,
                "dataset_dir": result["dataset_dir"],
                "best_weights": result.get("best_weights"),
            })
        
        # === Сохранить конфигурацию ансамбля ===
        ensemble_config = {
            "models": [],
            "merge_strategy": "wbf",
            "iou_threshold": 0.5,
            "confidence_threshold": 0.5,
        }
        
        for info in models_info:
            if info["best_weights"]:
                ensemble_config["models"].append({
                    "weights": str(info["best_weights"]),
                    "tile_size": info["tile_size"],
                    "weight": info["weight"],
                    "sahi_overlap": info["preset"]["sahi_overlap"],
                })
        
        # Сохранить конфиг в YAML
        import yaml
        config_path = self.experiments_dir / f"{experiment_name}_ensemble.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(ensemble_config, f, sort_keys=False, allow_unicode=True)
        
        print("\n" + "#" * 70)
        print("#" + " " * 12 + "ENSEMBLE PIPELINE ЗАВЕРШЕН" + " " * 14 + "#")
        print("#" * 70)
        
        print(f"\nОбученные модели:")
        for info in models_info:
            status = "✓" if info["best_weights"] else "✗ (пропущено)"
            print(f"  tile_size={info['tile_size']}: {status}")
            if info["best_weights"]:
                print(f"    weights: {info['best_weights']}")
                print(f"    weight в ансамбле: {info['weight']}")
        
        print(f"\nКонфигурация ансамбля: {config_path}")
        print(f"Test набор: {test_dir}")
        
        print(f"\n--- Для инференса ---")
        print(f"python -m pid_node_detection ensemble-inference \\")
        print(f"  --ensemble-config {config_path} \\")
        print(f"  --input ./images --output ./predictions")
        
        return {
            "models": models_info,
            "test_dir": test_dir,
            "ensemble_config": ensemble_config,
            "ensemble_config_path": config_path,
        }


class EnsembleTestPipeline:
    """
    Тестирование ансамблевой модели на фиксированном test наборе.
    
    Загружает конфигурацию ансамбля, запускает все модели,
    объединяет результаты и вычисляет метрики.
    """
    
    def __init__(self, config: Any, classes: Any):
        self.config = config
        self.classes = classes
        self.dataset_dir = Path(config.paths.dataset_dir)
        self.statistics_dir = Path(config.paths.statistics_dir)
    
    def run(
        self,
        ensemble_config_path: Path,
        output_name: Optional[str] = None,
        merge_strategy: Optional[str] = None,
        confidence_threshold: Optional[float] = None,
        iou_threshold: Optional[float] = None,
        save_visualizations: bool = True
    ) -> Dict:
        """
        Тестирование ансамбля.
        
        Args:
            ensemble_config_path: Путь к YAML конфигу ансамбля
            output_name: Имя для результатов
            merge_strategy: Переопределить стратегию слияния
            confidence_threshold: Переопределить порог confidence
            iou_threshold: Переопределить порог IoU
            save_visualizations: Сохранять визуализации
            
        Returns:
            {metrics, output_dir}
        """
        import yaml
        import json
        
        # Загрузить конфиг ансамбля
        with open(ensemble_config_path, "r") as f:
            ens_cfg = yaml.safe_load(f)
        
        if merge_strategy:
            ens_cfg["merge_strategy"] = merge_strategy
        if confidence_threshold is not None:
            ens_cfg["confidence_threshold"] = confidence_threshold
        if iou_threshold is not None:
            ens_cfg["iou_threshold"] = iou_threshold
        
        if output_name is None:
            output_name = f"test_ensemble_{ens_cfg['merge_strategy']}"
        
        print("\n" + "#" * 70)
        print("#" + " " * 15 + "ENSEMBLE TEST PIPELINE" + " " * 16 + "#")
        print("#" * 70)
        
        # Создать ансамблевый детектор
        from pid_node_detection.inference.ensemble import EnsembleDetector
        
        detector = EnsembleDetector(
            models=ens_cfg["models"],
            merge_strategy=ens_cfg.get("merge_strategy", "wbf"),
            iou_threshold=ens_cfg.get("iou_threshold", 0.5),
            confidence_threshold=ens_cfg.get("confidence_threshold", 0.5),
            device=getattr(self.config.inference, 'device', 'cuda'),
            class_names=self.classes.class_names,
            reverse_reindex=self.classes.reverse_reindex_mapping
        )
        
        # Test набор
        test_dir = self.dataset_dir / "test"
        test_images = test_dir / "images"
        test_labels = test_dir / "labels"
        
        print(f"Test: {len(list(test_images.glob('*.png')))} схем")
        print(f"Модели: {len(ens_cfg['models'])}")
        for m in ens_cfg["models"]:
            print(f"  tile={m['tile_size']}, weight={m['weight']}")
        print(f"Стратегия: {ens_cfg.get('merge_strategy', 'wbf')}")
        
        # Выходная директория
        output_dir = self.statistics_dir / output_name
        output_dir.mkdir(parents=True, exist_ok=True)
        predictions_dir = output_dir / "predictions"
        predictions_dir.mkdir(exist_ok=True)
        
        # Загрузить GT
        ground_truth = {}
        for label_file in test_labels.glob("*.txt"):
            annotations = []
            with open(label_file, "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        annotations.append({
                            "class_id": int(parts[0]),
                            "bbox": [float(x) for x in parts[1:5]]
                        })
            ground_truth[label_file.stem] = annotations
        
        # Инференс
        print(f"\n{'=' * 60}")
        print("АНСАМБЛЕВЫЙ ИНФЕРЕНС")
        print(f"{'=' * 60}")
        
        all_predictions = {}
        
        for img_path in sorted(test_images.glob("*.png")):
            print(f"Обработка: {img_path.name}")
            
            predictions = detector.detect(
                image=img_path,
                apply_reverse_mapping=False
            )
            
            all_predictions[img_path.stem] = predictions
            
            # Сохранить
            pred_file = predictions_dir / f"{img_path.stem}.txt"
            with open(pred_file, "w") as f:
                for det in predictions:
                    f.write(f"{det['class_id']} {det['x_center']:.6f} "
                            f"{det['y_center']:.6f} {det['width']:.6f} "
                            f"{det['height']:.6f} {det['confidence']:.4f}\n")
        
        # Метрики
        print(f"\n{'=' * 60}")
        print("РАСЧЕТ МЕТРИК")
        print(f"{'=' * 60}")
        
        from pid_node_detection.evaluation.metrics import (
            MetricsCalculator, plot_confusion_matrix, save_metrics_csv
        )
        
        calculator = MetricsCalculator(
            num_classes=self.classes.num_classes,
            class_names=self.classes.class_names,
            iou_threshold=self.config.evaluation.iou_threshold
        )
        
        for img_name in all_predictions:
            preds = all_predictions[img_name]
            gt = ground_truth.get(img_name, [])
            
            pred_tuples = [
                (p["class_id"], p["x_center"], p["y_center"],
                 p["width"], p["height"], p.get("confidence", 1.0))
                for p in preds
            ]
            gt_tuples = [
                (g["class_id"], g["bbox"][0], g["bbox"][1],
                 g["bbox"][2], g["bbox"][3])
                for g in gt
            ]
            
            calculator.add_image(pred_tuples, gt_tuples)
        
        metrics = calculator.calculate()
        
        # Вывод
        print(f"\nОбщие метрики:")
        print(f"  Precision: {metrics['precision']:.4f}")
        print(f"  Recall:    {metrics['recall']:.4f}")
        print(f"  F1:        {metrics['f1']:.4f}")
        print(f"  mAP@0.5:   {metrics['map50']:.4f}")
        
        # Сохранить
        save_metrics_csv(metrics, output_dir / "per_class_metrics.csv")
        
        if self.config.evaluation.save_confusion_matrix:
            plot_confusion_matrix(
                metrics["confusion_matrix"],
                class_names=self.classes.class_names,
                output_path=output_dir / "confusion_matrix.png",
                normalize=True
            )
        
        import numpy as np_json
        
        def convert_numpy(obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, (np.integer, np.floating)):
                return obj.item()
            elif isinstance(obj, dict):
                return {k: convert_numpy(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_numpy(x) for x in obj]
            return obj
        
        with open(output_dir / "metrics.json", "w") as f:
            json.dump(convert_numpy(metrics), f, indent=2, ensure_ascii=False)
        
        # Сохранить конфиг ансамбля рядом с результатами
        with open(output_dir / "ensemble_config.yaml", "w") as f:
            yaml.dump(ens_cfg, f, sort_keys=False)
        
        print(f"\nРезультаты: {output_dir}")
        
        return {
            "metrics": metrics,
            "output_dir": output_dir,
        }
