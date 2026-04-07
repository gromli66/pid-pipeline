"""
Train Pipeline для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Оркестрация полного пайплайна обучения:
1. Предобработка (grayscale, удаление классов)
2. Разбиение на test/trainval (ВАЖНО: test фиксируется навсегда)
3. Тайлинг trainval
4. Разбиение тайлов на train/val
5. Copy-Paste аугментация
6. Rotation аугментация
7. Обучение YOLO

ВАЖНО О TEST:
------------
Test выделяется ОДИН РАЗ при первом обучении и сохраняется.
Все последующие эксперименты (включая finetune) используют этот же test.
Это гарантирует честное сравнение моделей.

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.pipelines import TrainPipeline
    from pid_node_detection.config import load_config, load_classes
    
    config = load_config("my_config.yaml")
    classes = load_classes()
    
    pipeline = TrainPipeline(config, classes)
    results = pipeline.run()
"""

import shutil
from pathlib import Path
from typing import Dict, Optional, Any

from pid_node_detection.data.preprocessing import preprocess_pipeline
from pid_node_detection.data.splitting import split_data, stratified_split_tiles
from pid_node_detection.data.tiling import tile_dataset
from pid_node_detection.data.statistics import analyze_dataset, print_statistics, save_statistics
from pid_node_detection.augmentation.copy_paste import CopyPasteAugmentation
from pid_node_detection.augmentation.rotation import RotationAugmentation
from pid_node_detection.training.trainer import YOLOTrainer, create_data_yaml


class TrainPipeline:
    """
    Полный пайплайн обучения модели детекции узлов.
    
    Attributes:
        config: Конфигурация из load_config()
        classes: Конфигурация классов из load_classes()
        work_dir: Рабочая директория для промежуточных результатов
    """
    
    def __init__(self, config: Any, classes: Any):
        """
        Args:
            config: Config объект с параметрами
            classes: ClassesConfig объект с маппингом классов
        """
        self.config = config
        self.classes = classes
        self.work_dir = Path(config.paths.work_dir)
        self.dataset_dir = Path(config.paths.dataset_dir)
        self.experiments_dir = Path(config.paths.experiments_dir)
        self.statistics_dir = Path(config.paths.statistics_dir)
        
        # Создать директории
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.dataset_dir.mkdir(parents=True, exist_ok=True)
        self.experiments_dir.mkdir(parents=True, exist_ok=True)
        self.statistics_dir.mkdir(parents=True, exist_ok=True)
    
    def step_preprocess(self) -> Path:
        """
        Шаг 1: Предобработка (grayscale + удаление классов).
        
        Returns:
            Путь к директории с очищенными данными
        """
        print("\n" + "="*70)
        print("ШАГ 1: ПРЕДОБРАБОТКА")
        print("="*70)
        
        raw_images = Path(self.config.paths.raw_images)
        raw_labels = Path(self.config.paths.raw_labels)
        
        # Получить reindex_mapping из classes config
        reindex_mapping = getattr(self.classes, 'reindex_mapping', None)
        
        cleaned_dir = preprocess_pipeline(
            raw_images_dir=raw_images,
            raw_labels_dir=raw_labels,
            output_dir=self.work_dir,
            classes_to_remove=self.classes.classes_to_remove,
            reindex_mapping=reindex_mapping,
            grayscale=self.config.preprocessing.grayscale
        )
        
        # Статистика после очистки
        stats = analyze_dataset(
            labels_dir=cleaned_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "После предобработки")
        save_statistics(stats, self.statistics_dir / "01_after_preprocessing.csv")
        
        return cleaned_dir
    
    def step_split_test(self, cleaned_dir: Path) -> Dict[str, Path]:
        """
        Шаг 2: Выделение TEST набора (фиксируется навсегда).
        
        ВАЖНО: Test выделяется на уровне СХЕМ, не тайлов.
        Test сохраняется в отдельную директорию и не меняется.
        
        Args:
            cleaned_dir: Путь к очищенным данным
            
        Returns:
            {"test": Path, "trainval": Path}
        """
        print("\n" + "="*70)
        print("ШАГ 2: ВЫДЕЛЕНИЕ TEST НАБОРА")
        print("="*70)
        
        test_dir = self.dataset_dir / "test"
        
        # Проверить существует ли уже test
        if test_dir.exists() and (test_dir / "images").exists():
            test_count = len(list((test_dir / "images").glob("*.png")))
            if test_count > 0:
                print(f"\n*** TEST УЖЕ СУЩЕСТВУЕТ: {test_count} схем ***")
                print(f"*** Используем существующий test для честного сравнения ***")
                
                # Для trainval используем всё кроме test
                # Скопируем все данные и удалим те, что в test
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
                
                print(f"TrainVal: {len(list(trainval_images.glob('*.png')))} схем")
                
                return {"test": test_dir, "trainval": trainval_dir}
        
        # Test не существует - создаем новый
        print("\n*** СОЗДАНИЕ НОВОГО TEST НАБОРА ***")
        print("*** ВНИМАНИЕ: Test будет зафиксирован для всех будущих экспериментов ***")
        
        split_result = split_data(
            images_dir=cleaned_dir / "images",
            labels_dir=cleaned_dir / "labels",
            output_dir=self.work_dir / "split",
            test_ratio=self.config.splitting.test_ratio,
            rare_classes=self.classes.rare_classes,
            random_seed=self.config.splitting.random_seed
        )
        
        # Сохранить test в постоянное место
        if test_dir.exists():
            shutil.rmtree(test_dir)
        shutil.copytree(split_result["test"], test_dir)
        
        print(f"\n*** TEST СОХРАНЕН: {test_dir} ***")
        
        # Статистика test
        stats = analyze_dataset(
            labels_dir=test_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "TEST набор (ФИКСИРОВАННЫЙ)")
        save_statistics(stats, self.statistics_dir / "02_test_fixed.csv")
        
        return {"test": test_dir, "trainval": split_result["trainval"]}
    
    def step_tiling(self, trainval_dir: Path) -> Path:
        """
        Шаг 3: Тайлинг trainval данных.
        
        Args:
            trainval_dir: Путь к trainval данным
            
        Returns:
            Путь к директории с тайлами
        """
        print("\n" + "="*70)
        print("ШАГ 3: ТАЙЛИНГ")
        print("="*70)
        
        tiles_dir = self.work_dir / "tiles"
        
        # Пути к маскам (опционально)
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
            tile_size=self.config.tiling.tile_size,
            overlap=self.config.tiling.overlap,
            min_visible_ratio=self.config.tiling.min_visible_ratio,
            keep_empty_ratio=self.config.tiling.keep_empty_ratio,
            dilation_kernel=self.config.tiling.forbidden_mask_dilation
        )
        
        # Статистика после тайлинга
        stats = analyze_dataset(
            labels_dir=tiles_dir / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "После тайлинга")
        save_statistics(stats, self.statistics_dir / "03_after_tiling.csv")
        
        return tiles_dir
    
    def step_split_train_val(self, tiles_dir: Path) -> Dict[str, Path]:
        """
        Шаг 4: Разбиение тайлов на train и val.
        
        Args:
            tiles_dir: Путь к тайлам
            
        Returns:
            {"train": Path, "val": Path}
        """
        print("\n" + "="*70)
        print("ШАГ 4: РАЗБИЕНИЕ TRAIN/VAL")
        print("="*70)
        
        split_result = stratified_split_tiles(
            tiles_dir=tiles_dir,
            output_dir=self.work_dir / "train_val_split",
            val_ratio=self.config.splitting.val_ratio,
            random_seed=self.config.splitting.random_seed
        )
        
        return split_result
    
    def step_copy_paste(self, train_dir: Path) -> Path:
        """
        Шаг 5: Copy-Paste аугментация.
        
        Args:
            train_dir: Путь к train данным
            
        Returns:
            Путь к аугментированным данным
        """
        print("\n" + "="*70)
        print("ШАГ 5: COPY-PASTE АУГМЕНТАЦИЯ")
        print("="*70)
        
        if not self.config.augmentation.copy_paste.enabled:
            print("Copy-Paste отключен в конфиге")
            return train_dir
        
        cpa_output = self.work_dir / "train_cpa"
        
        # Маски forbidden
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
        
        # Статистика после CPA
        stats = analyze_dataset(
            labels_dir=cpa_output / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "После Copy-Paste")
        save_statistics(stats, self.statistics_dir / "05_after_cpa.csv")
        
        return cpa_output
    
    def step_rotation(self, train_dir: Path) -> Path:
        """
        Шаг 6: Rotation аугментация.
        
        Args:
            train_dir: Путь к train данным
            
        Returns:
            Путь к аугментированным данным
        """
        print("\n" + "="*70)
        print("ШАГ 6: ROTATION АУГМЕНТАЦИЯ")
        print("="*70)
        
        if not self.config.augmentation.rotation.enabled:
            print("Rotation отключен в конфиге")
            return train_dir
        
        rotation_output = self.work_dir / "train_final"
        
        rotation = RotationAugmentation(
            mode=self.config.augmentation.rotation.mode,
            angles=self.config.augmentation.rotation.angles
        )
        
        rotation.augment(
            images_dir=train_dir / "images",
            labels_dir=train_dir / "labels",
            output_dir=rotation_output
        )
        
        # Статистика после rotation
        stats = analyze_dataset(
            labels_dir=rotation_output / "labels",
            class_names=self.classes.class_names
        )
        print_statistics(stats, "После Rotation (финальный train)")
        save_statistics(stats, self.statistics_dir / "06_train_final.csv")
        
        return rotation_output
    
    def step_prepare_dataset(
        self,
        train_dir: Path,
        val_dir: Path
    ) -> Path:
        """
        Шаг 7: Подготовка финального датасета.
        
        Создает структуру для YOLO:
        - dataset/train/images, dataset/train/labels
        - dataset/val/images, dataset/val/labels
        - dataset/data.yaml
        
        Args:
            train_dir: Путь к финальным train данным
            val_dir: Путь к val данным
            
        Returns:
            Путь к dataset директории
        """
        print("\n" + "="*70)
        print("ШАГ 7: ПОДГОТОВКА ДАТАСЕТА ДЛЯ YOLO")
        print("="*70)
        
        dataset_train = self.dataset_dir / "train"
        dataset_val = self.dataset_dir / "val"
        
        # Очистить если существует
        if dataset_train.exists():
            shutil.rmtree(dataset_train)
        if dataset_val.exists():
            shutil.rmtree(dataset_val)
        
        # Копировать train
        shutil.copytree(train_dir / "images", dataset_train / "images")
        shutil.copytree(train_dir / "labels", dataset_train / "labels")
        
        # Копировать val
        shutil.copytree(val_dir / "images", dataset_val / "images")
        shutil.copytree(val_dir / "labels", dataset_val / "labels")
        
        # Создать data.yaml
        create_data_yaml(
            dataset_dir=self.dataset_dir,
            num_classes=self.classes.num_classes,
            class_names=self.classes.class_names
        )
        
        print(f"\nДатасет подготовлен:")
        print(f"  Train: {len(list((dataset_train / 'images').glob('*.png')))} файлов")
        print(f"  Val: {len(list((dataset_val / 'images').glob('*.png')))} файлов")
        print(f"  Test: {len(list((self.dataset_dir / 'test' / 'images').glob('*.png')))} схем (фиксированный)")
        
        return self.dataset_dir
    
    def step_train(self, experiment_name: Optional[str] = None) -> Dict:
        """
        Шаг 8: Обучение модели.
        
        Args:
            experiment_name: Имя эксперимента (по умолчанию 'baseline')
            
        Returns:
            Результаты обучения
        """
        print("\n" + "="*70)
        print("ШАГ 8: ОБУЧЕНИЕ МОДЕЛИ")
        print("="*70)
        
        if experiment_name is None:
            experiment_name = "baseline"
        
        trainer = YOLOTrainer(self.config)
        
        results = trainer.train(
            data_yaml=self.dataset_dir / "data.yaml",
            project=self.experiments_dir,
            name=experiment_name
        )
        
        return results
    
    def run(
        self,
        experiment_name: Optional[str] = None,
        skip_training: bool = False
    ) -> Dict:
        """
        Запуск полного пайплайна.
        
        Args:
            experiment_name: Имя эксперимента
            skip_training: Пропустить обучение (только подготовка данных)
            
        Returns:
            Словарь с путями и результатами
        """
        print("\n" + "#"*70)
        print("#" + " "*22 + "TRAIN PIPELINE" + " "*22 + "#")
        print("#"*70)
        
        results = {}
        
        # Шаг 1: Предобработка
        cleaned_dir = self.step_preprocess()
        results["cleaned_dir"] = cleaned_dir
        
        # Шаг 2: Выделение test
        split_result = self.step_split_test(cleaned_dir)
        results["test_dir"] = split_result["test"]
        results["trainval_dir"] = split_result["trainval"]
        
        # Шаг 3: Тайлинг
        tiles_dir = self.step_tiling(split_result["trainval"])
        results["tiles_dir"] = tiles_dir
        
        # Шаг 4: Split train/val
        train_val = self.step_split_train_val(tiles_dir)
        
        # Шаг 5: Copy-Paste
        train_cpa = self.step_copy_paste(train_val["train"])
        
        # Шаг 6: Rotation
        train_final = self.step_rotation(train_cpa)
        results["train_final"] = train_final
        results["val_dir"] = train_val["val"]
        
        # Шаг 7: Подготовка датасета
        dataset_dir = self.step_prepare_dataset(train_final, train_val["val"])
        results["dataset_dir"] = dataset_dir
        
        # Шаг 8: Обучение
        if not skip_training:
            training_results = self.step_train(experiment_name)
            results["training"] = training_results
        
        print("\n" + "#"*70)
        print("#" + " "*22 + "PIPELINE ЗАВЕРШЕН" + " "*18 + "#")
        print("#"*70)
        
        print(f"\nРезультаты:")
        print(f"  Dataset: {results['dataset_dir']}")
        print(f"  Test (фиксированный): {results['test_dir']}")
        
        if not skip_training:
            print(f"  Эксперимент: {self.experiments_dir / (experiment_name or 'baseline')}")
        
        return results
