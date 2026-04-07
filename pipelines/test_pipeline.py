"""
Test Pipeline для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Оценка модели на фиксированном test наборе:
1. Загрузка модели
2. Инференс на полноразмерных тестовых схемах через SAHI
3. Расчет метрик (Precision, Recall, F1, mAP)
4. Confusion matrix
5. Per-class анализ

ВАЖНО:
------
- Использует ФИКСИРОВАННЫЙ test набор (созданный при первом train)
- Тестовые схемы в полном разрешении (не тайлы!)
- SAHI используется для слайсинга при инференсе
- Результаты сохраняются для сравнения моделей

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.pipelines import TestPipeline
    from pid_node_detection.config import load_config, load_classes
    
    config = load_config("my_config.yaml")
    classes = load_classes()
    
    pipeline = TestPipeline(config, classes)
    results = pipeline.run(
        weights=Path("./experiments/baseline/weights/best.pt"),
        output_name="baseline_test"
    )
"""

import json
from pathlib import Path
from typing import Dict, Optional, Any, Union, List

from pid_node_detection.data.preprocessing import preprocess_images
from pid_node_detection.inference.detector import NodeDetector
from pid_node_detection.evaluation.metrics import (
    MetricsCalculator,
    plot_confusion_matrix,
    save_metrics_csv,
    print_metrics
)


class TestPipeline:
    """
    Пайплайн тестирования модели на фиксированном test наборе.
    
    Attributes:
        config: Конфигурация из load_config()
        classes: Конфигурация классов из load_classes()
    """
    
    def __init__(self, config: Any, classes: Any):
        """
        Args:
            config: Config объект с параметрами
            classes: ClassesConfig объект с маппингом классов
        """
        self.config = config
        self.classes = classes
        self.dataset_dir = Path(config.paths.dataset_dir)
        self.experiments_dir = Path(config.paths.experiments_dir)
        self.statistics_dir = Path(config.paths.statistics_dir)
    
    def _get_test_dir(self) -> Path:
        """
        Получить путь к фиксированному test набору.
        
        Returns:
            Путь к test директории
            
        Raises:
            FileNotFoundError: Если test не найден
        """
        test_dir = self.dataset_dir / "test"
        
        if not test_dir.exists():
            raise FileNotFoundError(
                f"Test набор не найден: {test_dir}\n"
                "Сначала выполните train pipeline."
            )
        
        return test_dir
    
    def _load_ground_truth(self, labels_dir: Path) -> Dict[str, List[Dict]]:
        """
        Загрузить ground truth аннотации.
        
        Args:
            labels_dir: Директория с аннотациями
            
        Returns:
            {filename: [{"class_id": int, "bbox": [x,y,w,h]}]}
        """
        ground_truth = {}
        
        for label_file in labels_dir.glob("*.txt"):
            annotations = []
            
            with open(label_file, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            annotations.append({
                                "class_id": int(parts[0]),
                                "bbox": [float(x) for x in parts[1:5]]
                            })
                        except ValueError:
                            continue
            
            ground_truth[label_file.stem] = annotations
        
        return ground_truth
    
    def _visualize_predictions(
        self,
        image_path: Path,
        predictions: List[Dict],
        output_path: Path,
        ground_truth: List[Dict] = None
    ):
        """
        Визуализация предсказаний на изображении.
        
        Args:
            image_path: Путь к изображению
            predictions: Список предсказаний
            output_path: Путь для сохранения
            ground_truth: Ground truth аннотации (опционально)
        """
        import cv2
        
        img = cv2.imread(str(image_path))
        if img is None:
            return
        
        h, w = img.shape[:2]
        
        # Рисуем ground truth зелёным
        if ground_truth:
            for gt in ground_truth:
                bbox = gt["bbox"]
                x_center, y_center, bw, bh = bbox
                x1 = int((x_center - bw/2) * w)
                y1 = int((y_center - bh/2) * h)
                x2 = int((x_center + bw/2) * w)
                y2 = int((y_center + bh/2) * h)
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        
        # Рисуем предсказания красным
        for pred in predictions:
            x_center = pred["x_center"]
            y_center = pred["y_center"]
            bw = pred["width"]
            bh = pred["height"]
            conf = pred.get("confidence", 0)
            class_id = pred["class_id"]
            
            x1 = int((x_center - bw/2) * w)
            y1 = int((y_center - bh/2) * h)
            x2 = int((x_center + bw/2) * w)
            y2 = int((y_center + bh/2) * h)
            
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
            
            # Подпись
            class_name = self.classes.class_names.get(class_id, str(class_id))
            label = f"{class_name}: {conf:.2f}"
            cv2.putText(img, label, (x1, y1-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        
        cv2.imwrite(str(output_path), img)
    
    def run(
        self,
        weights: Union[str, Path],
        output_name: Optional[str] = None,
        confidence_threshold: Optional[float] = None,
        save_predictions: bool = True,
        save_visualizations: bool = True
    ) -> Dict:
        """
        Запуск тестирования модели.
        
        Args:
            weights: Путь к весам модели
            output_name: Имя для результатов (по умолчанию из имени весов)
            confidence_threshold: Порог уверенности
            save_predictions: Сохранить предсказания в YOLO формате
            save_visualizations: Сохранить визуализации
            
        Returns:
            Словарь с метриками и путями к результатам
        """
        weights = Path(weights)
        
        if not weights.exists():
            raise FileNotFoundError(f"Веса не найдены: {weights}")
        
        if output_name is None:
            output_name = f"test_{weights.parent.parent.name}"
        
        print("\n" + "#"*70)
        print("#" + " "*22 + "TEST PIPELINE" + " "*23 + "#")
        print("#"*70)
        
        # Получить test набор
        test_dir = self._get_test_dir()
        test_images = test_dir / "images"
        test_labels = test_dir / "labels"
        
        test_count = len(list(test_images.glob("*.png")))
        print(f"\nTest набор: {test_count} схем")
        print(f"Веса: {weights}")
        
        # Подготовить директорию для результатов
        output_dir = self.statistics_dir / output_name
        output_dir.mkdir(parents=True, exist_ok=True)
        
        predictions_dir = output_dir / "predictions"
        visualizations_dir = output_dir / "visualizations"
        
        if save_predictions:
            predictions_dir.mkdir(exist_ok=True)
        if save_visualizations:
            visualizations_dir.mkdir(exist_ok=True)
        
        # Создать детектор
        inference_cfg = self.config.inference
        detector = NodeDetector(
            weights=weights,
            confidence=confidence_threshold,
            iou_threshold=getattr(inference_cfg, 'iou_threshold', 0.5),
            device=getattr(inference_cfg, 'device', 'cuda'),
            use_sahi=getattr(inference_cfg, 'use_sahi', True),
            adaptive_slicing=getattr(inference_cfg.adaptive_slicing, 'enabled', True) if hasattr(inference_cfg, 'adaptive_slicing') else True,
            class_names=self.classes.class_names,
            reverse_reindex=self.classes.reverse_reindex_mapping
        )
        
        # Загрузить ground truth
        ground_truth = self._load_ground_truth(test_labels)
        
        # Инференс на всех тестовых схемах
        print("\n" + "="*60)
        print("ИНФЕРЕНС НА ТЕСТОВЫХ СХЕМАХ")
        print("="*60)
        
        all_predictions = {}
        
        for img_path in sorted(test_images.glob("*.png")):
            print(f"\nОбработка: {img_path.name}")
            
            # Детекция
            # Не применяем reverse_mapping - test labels уже в формате модели
            predictions = detector.detect(image=img_path, apply_reverse_mapping=False)
            
            all_predictions[img_path.stem] = predictions
            
            # Сохранить предсказания в YOLO формате
            if save_predictions:
                pred_file = predictions_dir / f"{img_path.stem}.txt"
                with open(pred_file, "w", encoding="utf-8") as f:
                    for det in predictions:
                        class_id = det["class_id"]
                        # Применить обратную переиндексацию если нужно
                        if getattr(self.config.inference, 'apply_reverse_mapping', False):
                            class_id = self.classes.reverse_reindex_mapping.get(class_id, class_id)
                        f.write(f"{class_id} {det['x_center']:.6f} {det['y_center']:.6f} "
                                f"{det['width']:.6f} {det['height']:.6f} {det['confidence']:.4f}\n")
            
            # Визуализация
            if save_visualizations:
                vis_path = visualizations_dir / f"{img_path.stem}_pred.png"
                self._visualize_predictions(
                    image_path=img_path,
                    predictions=predictions,
                    output_path=vis_path,
                    ground_truth=ground_truth.get(img_path.stem, [])
                )
        
        # Расчет метрик с использованием MetricsCalculator
        print("\n" + "="*60)
        print("РАСЧЕТ МЕТРИК")
        print("="*60)
        
        calculator = MetricsCalculator(
            num_classes=self.classes.num_classes,
            class_names=self.classes.class_names,
            iou_threshold=self.config.evaluation.iou_threshold
        )
        
        # Добавить результаты по каждому изображению
        for img_name in all_predictions:
            preds = all_predictions[img_name]
            gt = ground_truth.get(img_name, [])
            
            # Конвертировать в формат для MetricsCalculator
            # predictions: [(class_id, x, y, w, h, conf), ...]
            # ground_truth: [(class_id, x, y, w, h), ...]
            pred_tuples = []
            for p in preds:
                pred_tuples.append((
                    p["class_id"],
                    p["x_center"], p["y_center"], p["width"], p["height"],
                    p.get("confidence", 1.0)
                ))
            
            gt_tuples = []
            for g in gt:
                gt_tuples.append((
                    g["class_id"],
                    g["bbox"][0], g["bbox"][1], g["bbox"][2], g["bbox"][3]
                ))
            
            calculator.add_image(pred_tuples, gt_tuples)
        
        # Вычислить метрики
        metrics = calculator.calculate()
        confusion_matrix = metrics["confusion_matrix"]
        
        # Вывод результатов
        print("\n" + "="*60)
        print("РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ")
        print("="*60)
        
        print(f"\nОбщие метрики:")
        print(f"  Precision: {metrics['precision']:.4f}")
        print(f"  Recall:    {metrics['recall']:.4f}")
        print(f"  F1:        {metrics['f1']:.4f}")
        print(f"  mAP@0.5:   {metrics['map50']:.4f}")
        
        # Per-class метрики
        print(f"\nPer-class метрики (топ 10 по F1):")
        per_class = metrics['per_class']
        sorted_classes = sorted(per_class.items(), key=lambda x: x[1]['f1'], reverse=True)
        
        print(f"{'Класс':<30} {'P':>8} {'R':>8} {'F1':>8} {'Support':>8}")
        print("-" * 64)
        
        for class_id, class_metrics in sorted_classes[:10]:
            class_name = self.classes.class_names.get(int(class_id), f"class_{class_id}")
            print(f"{class_name:<30} {class_metrics['precision']:>8.3f} "
                  f"{class_metrics['recall']:>8.3f} {class_metrics['f1']:>8.3f} "
                  f"{class_metrics['support']:>8}")
        
        # Проблемные классы (низкий F1)
        print(f"\nПроблемные классы (F1 < 0.5):")
        problem_classes = [(c, m) for c, m in sorted_classes 
                          if m['f1'] < 0.5 and m['support'] > 0]
        
        if problem_classes:
            for class_id, class_metrics in problem_classes:
                class_name = self.classes.class_names.get(int(class_id), f"class_{class_id}")
                print(f"  {class_name}: F1={class_metrics['f1']:.3f}, "
                      f"P={class_metrics['precision']:.3f}, R={class_metrics['recall']:.3f}")
        else:
            print("  Нет классов с F1 < 0.5")
        
        # Сохранить CSV с метриками
        save_metrics_csv(metrics, output_dir / "per_class_metrics.csv")
        
        # Сохранить confusion matrix
        if self.config.evaluation.save_confusion_matrix:
            plot_confusion_matrix(
                confusion_matrix,
                class_names=self.classes.class_names,
                output_path=output_dir / "confusion_matrix.png",
                normalize=True  # Нормализованная по строкам (проценты)
            )
        
        # Сохранить метрики в JSON
        metrics_json = output_dir / "metrics.json"
        with open(metrics_json, "w", encoding="utf-8") as f:
            # Конвертировать numpy типы
            import numpy as np
            def convert(obj):
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                elif isinstance(obj, (np.integer, np.floating)):
                    return obj.item()
                elif hasattr(obj, 'item') and not isinstance(obj, np.ndarray):
                    return obj.item()
                elif isinstance(obj, dict):
                    return {k: convert(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert(x) for x in obj]
                return obj
            
            json.dump(convert(metrics), f, indent=2, ensure_ascii=False)
        
        print("\n" + "#"*70)
        print("#" + " "*20 + "TEST ЗАВЕРШЕН" + " "*21 + "#")
        print("#"*70)
        
        print(f"\nРезультаты сохранены:")
        print(f"  Метрики JSON: {metrics_json}")
        print(f"  Метрики CSV: {output_dir / 'per_class_metrics.csv'}")
        if self.config.evaluation.save_confusion_matrix:
            print(f"  Confusion matrix: {output_dir / 'confusion_matrix.png'}")
        if save_predictions:
            print(f"  Предсказания: {predictions_dir}")
        if save_visualizations:
            print(f"  Визуализации: {visualizations_dir}")
        
        return {
            "metrics": metrics,
            "confusion_matrix": confusion_matrix,
            "output_dir": output_dir,
            "predictions_dir": predictions_dir if save_predictions else None,
            "visualizations_dir": visualizations_dir if save_visualizations else None
        }


def compare_experiments(
    experiment_dirs: List[Union[str, Path]],
    output_path: Optional[Union[str, Path]] = None
) -> Dict:
    """
    Сравнить результаты нескольких экспериментов.
    
    Args:
        experiment_dirs: Пути к директориям с результатами тестирования
        output_path: Путь для сохранения сравнения
        
    Returns:
        Таблица сравнения
    """
    import pandas as pd
    
    results = []
    
    for exp_dir in experiment_dirs:
        exp_dir = Path(exp_dir)
        metrics_file = exp_dir / "metrics.json"
        
        if not metrics_file.exists():
            print(f"Метрики не найдены: {metrics_file}")
            continue
        
        with open(metrics_file, "r", encoding="utf-8") as f:
            metrics = json.load(f)
        
        results.append({
            "experiment": exp_dir.name,
            "precision": metrics.get("precision", metrics.get("overall", {}).get("precision", 0)),
            "recall": metrics.get("recall", metrics.get("overall", {}).get("recall", 0)),
            "f1": metrics.get("f1", metrics.get("overall", {}).get("f1", 0)),
            "mAP": metrics.get("map50", metrics.get("overall", {}).get("mAP", 0))
        })
    
    df = pd.DataFrame(results)
    df = df.sort_values("f1", ascending=False)
    
    print("\n" + "="*70)
    print("СРАВНЕНИЕ ЭКСПЕРИМЕНТОВ")
    print("="*70)
    print(df.to_string(index=False))
    
    if output_path:
        df.to_csv(output_path, index=False)
        print(f"\nСохранено: {output_path}")
    
    return df.to_dict("records")
