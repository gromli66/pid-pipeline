"""
Метрики и оценка модели для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль вычисляет метрики качества детекции:
1. Precision, Recall, F1 (per-class и overall)
2. Confusion Matrix
3. mAP (mean Average Precision)

MATCHING АЛГОРИТМ:
-----------------
Для каждого ground truth объекта находим prediction с максимальным IoU:
- IoU >= threshold (default 0.5) и тот же класс = True Positive
- prediction без matching GT = False Positive  
- GT без matching prediction = False Negative

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.evaluation import evaluate_predictions
    
    metrics = evaluate_predictions(
        predictions_dir=Path("./predictions"),
        ground_truth_dir=Path("./test/labels"),
        iou_threshold=0.5
    )
    
    print(f"mAP@0.5: {metrics['map50']:.4f}")
    print(f"Precision: {metrics['precision']:.4f}")
    print(f"Recall: {metrics['recall']:.4f}")
"""

import csv
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict


def parse_yolo_file(
    file_path: Path,
    with_confidence: bool = False
) -> List[Tuple]:
    """
    Парсинг YOLO файла аннотаций/предсказаний.
    
    Args:
        file_path: Путь к файлу
        with_confidence: Содержит ли файл confidence (6-й столбец)
        
    Returns:
        Список кортежей:
        - без confidence: (class_id, x, y, w, h)
        - с confidence: (class_id, x, y, w, h, conf)
    """
    if not file_path.exists():
        return []
    
    objects = []
    
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            
            try:
                class_id = int(parts[0])
                x = float(parts[1])
                y = float(parts[2])
                w = float(parts[3])
                h = float(parts[4])
                
                if with_confidence and len(parts) >= 6:
                    conf = float(parts[5])
                    objects.append((class_id, x, y, w, h, conf))
                else:
                    objects.append((class_id, x, y, w, h))
            except ValueError:
                continue
    
    return objects


def calculate_iou(
    box1: Tuple[float, float, float, float],
    box2: Tuple[float, float, float, float]
) -> float:
    """
    Вычислить IoU между двумя YOLO bbox.
    
    Args:
        box1, box2: (x_center, y_center, width, height) - нормализованные координаты
        
    Returns:
        IoU (0-1)
    """
    # Конвертация в x1, y1, x2, y2
    b1_x1 = box1[0] - box1[2] / 2
    b1_y1 = box1[1] - box1[3] / 2
    b1_x2 = box1[0] + box1[2] / 2
    b1_y2 = box1[1] + box1[3] / 2
    
    b2_x1 = box2[0] - box2[2] / 2
    b2_y1 = box2[1] - box2[3] / 2
    b2_x2 = box2[0] + box2[2] / 2
    b2_y2 = box2[1] + box2[3] / 2
    
    # Intersection
    inter_x1 = max(b1_x1, b2_x1)
    inter_y1 = max(b1_y1, b2_y1)
    inter_x2 = min(b1_x2, b2_x2)
    inter_y2 = min(b1_y2, b2_y2)
    
    if inter_x1 >= inter_x2 or inter_y1 >= inter_y2:
        return 0.0
    
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    
    # Union
    b1_area = (b1_x2 - b1_x1) * (b1_y2 - b1_y1)
    b2_area = (b2_x2 - b2_x1) * (b2_y2 - b2_y1)
    
    union_area = b1_area + b2_area - inter_area
    
    return inter_area / (union_area + 1e-6)


def match_predictions(
    predictions: List[Tuple],
    ground_truth: List[Tuple],
    iou_threshold: float = 0.5
) -> Tuple[List[bool], List[bool], List[int]]:
    """
    Matching предсказаний с ground truth.
    
    Args:
        predictions: Список предсказаний (class_id, x, y, w, h, [conf])
        ground_truth: Список GT (class_id, x, y, w, h)
        iou_threshold: Порог IoU для положительного matching
        
    Returns:
        Tuple:
        - pred_matched: bool для каждого prediction (True = TP, False = FP)
        - gt_matched: bool для каждого GT (True = matched, False = FN)
        - pred_classes: class_id для каждого prediction
    """
    pred_matched = [False] * len(predictions)
    gt_matched = [False] * len(ground_truth)
    pred_classes = []
    
    # Сортировать predictions по confidence (если есть)
    if predictions and len(predictions[0]) >= 6:
        sorted_indices = sorted(
            range(len(predictions)),
            key=lambda i: predictions[i][5],
            reverse=True
        )
    else:
        sorted_indices = list(range(len(predictions)))
    
    for pred_idx in sorted_indices:
        pred = predictions[pred_idx]
        pred_class = pred[0]
        pred_box = pred[1:5]
        pred_classes.append(pred_class)
        
        best_iou = 0
        best_gt_idx = -1
        
        for gt_idx, gt in enumerate(ground_truth):
            if gt_matched[gt_idx]:
                continue
            
            gt_class = gt[0]
            gt_box = gt[1:5]
            
            # Класс должен совпадать
            if gt_class != pred_class:
                continue
            
            iou = calculate_iou(pred_box, gt_box)
            
            if iou > best_iou:
                best_iou = iou
                best_gt_idx = gt_idx
        
        if best_iou >= iou_threshold and best_gt_idx >= 0:
            pred_matched[pred_idx] = True
            gt_matched[best_gt_idx] = True
    
    return pred_matched, gt_matched, pred_classes


class MetricsCalculator:
    """
    Калькулятор метрик для детекции.
    
    Аккумулирует результаты по нескольким изображениям
    и вычисляет итоговые метрики.
    """
    
    def __init__(
        self,
        num_classes: int,
        class_names: Optional[Dict[int, str]] = None,
        iou_threshold: float = 0.5
    ):
        """
        Args:
            num_classes: Количество классов
            class_names: Маппинг id -> имя класса
            iou_threshold: Порог IoU для matching
        """
        self.num_classes = num_classes
        self.class_names = class_names or {}
        self.iou_threshold = iou_threshold
        
        # Аккумуляторы
        self.tp = defaultdict(int)  # True Positives по классам
        self.fp = defaultdict(int)  # False Positives по классам
        self.fn = defaultdict(int)  # False Negatives по классам
        
        # Confusion matrix: [gt_class][pred_class]
        self.confusion = defaultdict(lambda: defaultdict(int))
        
        # Для mAP
        self.all_predictions = []  # (confidence, is_tp, class_id)
        self.total_gt = defaultdict(int)
    
    def add_image(
        self,
        predictions: List[Tuple],
        ground_truth: List[Tuple]
    ) -> None:
        """
        Добавить результаты для одного изображения.
        
        Args:
            predictions: Предсказания (class_id, x, y, w, h, [conf])
            ground_truth: Ground truth (class_id, x, y, w, h)
        """
        pred_matched, gt_matched, pred_classes = match_predictions(
            predictions, ground_truth, self.iou_threshold
        )
        
        # Обновить счетчики
        for pred_idx, (is_tp, pred_class) in enumerate(zip(pred_matched, pred_classes)):
            if is_tp:
                self.tp[pred_class] += 1
            else:
                self.fp[pred_class] += 1
            
            # Для mAP
            conf = predictions[pred_idx][5] if len(predictions[pred_idx]) >= 6 else 1.0
            self.all_predictions.append((conf, is_tp, pred_class))
        
        for gt_idx, (is_matched, gt) in enumerate(zip(gt_matched, ground_truth)):
            gt_class = gt[0]
            self.total_gt[gt_class] += 1
            
            if not is_matched:
                self.fn[gt_class] += 1
        
        # Confusion matrix
        # Заполняем только для TP
        for pred_idx, pred in enumerate(predictions):
            pred_class = pred[0]
            pred_box = pred[1:5]
            
            # Найти лучший matching GT
            best_iou = 0
            best_gt_class = -1
            
            for gt in ground_truth:
                gt_class = gt[0]
                gt_box = gt[1:5]
                
                iou = calculate_iou(pred_box, gt_box)
                if iou > best_iou:
                    best_iou = iou
                    best_gt_class = gt_class
            
            if best_iou >= self.iou_threshold:
                self.confusion[best_gt_class][pred_class] += 1
    
    def calculate(self) -> Dict:
        """
        Вычислить все метрики.
        
        Returns:
            Словарь с метриками:
            - precision, recall, f1: overall
            - per_class: {class_id: {precision, recall, f1}}
            - map50: mAP@0.5
            - confusion_matrix: numpy array
        """
        # Per-class metrics
        per_class = {}
        
        all_tp = sum(self.tp.values())
        all_fp = sum(self.fp.values())
        all_fn = sum(self.fn.values())
        
        classes = set(self.tp.keys()) | set(self.fp.keys()) | set(self.fn.keys())
        
        for cls in classes:
            tp = self.tp[cls]
            fp = self.fp[cls]
            fn = self.fn[cls]
            
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            
            per_class[cls] = {
                "class_name": self.class_names.get(cls, f"class_{cls}"),
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "support": tp + fn
            }
        
        # Overall metrics
        precision = all_tp / (all_tp + all_fp) if (all_tp + all_fp) > 0 else 0
        recall = all_tp / (all_tp + all_fn) if (all_tp + all_fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        
        # mAP calculation (simplified - AP per class, then mean)
        ap_per_class = self._calculate_ap_per_class()
        map50 = np.mean(list(ap_per_class.values())) if ap_per_class else 0
        
        # Confusion matrix as numpy array
        all_classes = sorted(classes)
        cm_size = max(all_classes) + 1 if all_classes else 0
        confusion_matrix = np.zeros((cm_size, cm_size), dtype=np.int32)
        
        for gt_class, pred_dict in self.confusion.items():
            for pred_class, count in pred_dict.items():
                if gt_class < cm_size and pred_class < cm_size:
                    confusion_matrix[gt_class, pred_class] = count
        
        return {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "map50": map50,
            "per_class": per_class,
            "ap_per_class": ap_per_class,
            "confusion_matrix": confusion_matrix,
            "total_tp": all_tp,
            "total_fp": all_fp,
            "total_fn": all_fn
        }
    
    def _calculate_ap_per_class(self) -> Dict[int, float]:
        """Вычислить AP для каждого класса."""
        ap = {}
        
        # Группировать по классам
        by_class = defaultdict(list)
        for conf, is_tp, class_id in self.all_predictions:
            by_class[class_id].append((conf, is_tp))
        
        for cls, preds in by_class.items():
            if self.total_gt[cls] == 0:
                ap[cls] = 0
                continue
            
            # Сортировать по confidence
            preds.sort(key=lambda x: x[0], reverse=True)
            
            tp_cumsum = 0
            fp_cumsum = 0
            precisions = []
            recalls = []
            
            for conf, is_tp in preds:
                if is_tp:
                    tp_cumsum += 1
                else:
                    fp_cumsum += 1
                
                p = tp_cumsum / (tp_cumsum + fp_cumsum)
                r = tp_cumsum / self.total_gt[cls]
                
                precisions.append(p)
                recalls.append(r)
            
            # AP как площадь под PR-кривой (11-point interpolation)
            ap[cls] = self._calculate_ap(precisions, recalls)
        
        return ap
    
    def _calculate_ap(
        self,
        precisions: List[float],
        recalls: List[float]
    ) -> float:
        """11-point interpolated AP."""
        if not precisions:
            return 0
        
        ap = 0
        for t in np.arange(0, 1.1, 0.1):
            p_at_r = 0
            for p, r in zip(precisions, recalls):
                if r >= t:
                    p_at_r = max(p_at_r, p)
            ap += p_at_r
        
        return ap / 11


def evaluate_predictions(
    predictions_dir: Path,
    ground_truth_dir: Path,
    num_classes: int = 36,
    class_names: Optional[Dict[int, str]] = None,
    iou_threshold: float = 0.5,
    with_confidence: bool = False
) -> Dict:
    """
    Оценка предсказаний относительно ground truth.
    
    Args:
        predictions_dir: Директория с файлами предсказаний
        ground_truth_dir: Директория с файлами GT
        num_classes: Количество классов
        class_names: Маппинг id -> имя класса
        iou_threshold: Порог IoU для matching
        with_confidence: Файлы предсказаний содержат confidence
        
    Returns:
        Словарь с метриками
        
    Example:
        >>> metrics = evaluate_predictions(
        ...     predictions_dir=Path("./predictions"),
        ...     ground_truth_dir=Path("./test/labels"),
        ...     iou_threshold=0.5
        ... )
    """
    predictions_dir = Path(predictions_dir)
    ground_truth_dir = Path(ground_truth_dir)
    
    calculator = MetricsCalculator(
        num_classes=num_classes,
        class_names=class_names,
        iou_threshold=iou_threshold
    )
    
    # Найти все GT файлы
    gt_files = list(ground_truth_dir.glob("*.txt"))
    
    print(f"Оценка {len(gt_files)} изображений...")
    
    for gt_file in gt_files:
        pred_file = predictions_dir / gt_file.name
        
        gt = parse_yolo_file(gt_file)
        pred = parse_yolo_file(pred_file, with_confidence=with_confidence)
        
        calculator.add_image(pred, gt)
    
    metrics = calculator.calculate()
    
    return metrics


def calculate_metrics(
    predictions: List[Tuple],
    ground_truth: List[Tuple],
    iou_threshold: float = 0.5
) -> Dict:
    """
    Вычислить метрики для одного изображения.
    
    Args:
        predictions: Список предсказаний
        ground_truth: Список GT
        iou_threshold: Порог IoU
        
    Returns:
        {precision, recall, f1, tp, fp, fn}
    """
    pred_matched, gt_matched, _ = match_predictions(
        predictions, ground_truth, iou_threshold
    )
    
    tp = sum(pred_matched)
    fp = len(pred_matched) - tp
    fn = len(gt_matched) - sum(gt_matched)
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn
    }


def plot_confusion_matrix(
    confusion_matrix: np.ndarray,
    class_names: Optional[Dict[int, str]] = None,
    output_path: Optional[Path] = None,
    figsize: Tuple[int, int] = (12, 10),
    normalize: bool = False
) -> None:
    """
    Визуализация confusion matrix.
    
    Args:
        confusion_matrix: Numpy array размера (num_classes, num_classes)
        class_names: Маппинг id -> имя класса
        output_path: Путь для сохранения (если None - показать)
        figsize: Размер фигуры
        normalize: Нормализовать по строкам (GT)
    """
    try:
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError:
        print("matplotlib и seaborn не установлены. Визуализация недоступна.")
        return
    
    if normalize:
        row_sums = confusion_matrix.sum(axis=1, keepdims=True)
        cm = confusion_matrix.astype(float) / (row_sums + 1e-6)
    else:
        cm = confusion_matrix
    
    # Найти непустые классы
    non_empty_rows = np.any(confusion_matrix > 0, axis=1)
    non_empty_cols = np.any(confusion_matrix > 0, axis=0)
    non_empty = non_empty_rows | non_empty_cols
    
    indices = np.where(non_empty)[0]
    cm_filtered = cm[np.ix_(indices, indices)]
    
    # Labels
    if class_names:
        labels = [class_names.get(i, f"{i}") for i in indices]
    else:
        labels = [str(i) for i in indices]
    
    plt.figure(figsize=figsize)
    
    sns.heatmap(
        cm_filtered,
        annot=True,
        fmt=".2f" if normalize else "d",
        cmap="Blues",
        xticklabels=labels,
        yticklabels=labels
    )
    
    plt.xlabel("Predicted")
    plt.ylabel("Ground Truth")
    plt.title("Confusion Matrix")
    plt.tight_layout()
    
    if output_path:
        plt.savefig(output_path, dpi=150)
        print(f"Confusion matrix сохранена: {output_path}")
    else:
        plt.show()
    
    plt.close()


def save_metrics_csv(
    metrics: Dict,
    output_path: Path
) -> None:
    """
    Сохранить per-class метрики в CSV.
    
    Args:
        metrics: Результат calculate() или evaluate_predictions()
        output_path: Путь для сохранения
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "class_id", "class_name", "precision", "recall", "f1",
            "tp", "fp", "fn", "support", "ap"
        ])
        
        per_class = metrics.get("per_class", {})
        ap_per_class = metrics.get("ap_per_class", {})
        
        for cls in sorted(per_class.keys()):
            cls_metrics = per_class[cls]
            ap = ap_per_class.get(cls, 0)
            
            writer.writerow([
                cls,
                cls_metrics["class_name"],
                f"{cls_metrics['precision']:.4f}",
                f"{cls_metrics['recall']:.4f}",
                f"{cls_metrics['f1']:.4f}",
                cls_metrics["tp"],
                cls_metrics["fp"],
                cls_metrics["fn"],
                cls_metrics["support"],
                f"{ap:.4f}"
            ])
    
    print(f"Метрики сохранены: {output_path}")


def print_metrics(metrics: Dict) -> None:
    """Вывести метрики в консоль."""
    print("\n" + "="*60)
    print("МЕТРИКИ ОЦЕНКИ")
    print("="*60)
    print(f"Precision: {metrics['precision']:.4f}")
    print(f"Recall:    {metrics['recall']:.4f}")
    print(f"F1:        {metrics['f1']:.4f}")
    print(f"mAP@0.5:   {metrics['map50']:.4f}")
    print(f"\nTP: {metrics['total_tp']}, FP: {metrics['total_fp']}, FN: {metrics['total_fn']}")
    
    print("\n--- Per-class metrics ---")
    print(f"{'Class':<30} {'Prec':>8} {'Rec':>8} {'F1':>8} {'Support':>8}")
    print("-" * 70)
    
    per_class = metrics.get("per_class", {})
    for cls in sorted(per_class.keys()):
        m = per_class[cls]
        print(f"{m['class_name']:<30} {m['precision']:>8.4f} {m['recall']:>8.4f} "
              f"{m['f1']:>8.4f} {m['support']:>8}")
