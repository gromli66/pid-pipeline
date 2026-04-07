"""
Статистика и анализ датасета для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль объединяет функции анализа датасета:
1. Подсчет классов в датасете
2. Анализ размеров объектов
3. Определение редких классов
4. Сохранение статистики в CSV

КОГДА ИСПОЛЬЗОВАТЬ:
------------------
- После очистки данных (проверить что осталось)
- После разбиения (проверить баланс train/val/test)
- После тайлинга (проверить покрытие классов)
- После аугментаций (проверить баланс классов)

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.data.statistics import analyze_dataset, save_statistics
    
    # Анализ датасета
    stats = analyze_dataset(
        labels_dir=Path("./dataset/train/labels"),
        images_dir=Path("./dataset/train/images"),  # опционально для размеров
        class_names={0: "armatura_ruchn", ...}
    )
    
    # Сохранение в CSV
    save_statistics(stats, Path("./statistics/train_stats.csv"))
"""

import csv
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict, Counter
from PIL import Image


def count_classes(labels_dir: Path) -> Dict[int, int]:
    """
    Подсчитать количество объектов каждого класса.
    
    Args:
        labels_dir: Директория с YOLO аннотациями
        
    Returns:
        Словарь {class_id: count}
        
    Example:
        >>> counts = count_classes(Path("./labels"))
        >>> print(counts)  # {0: 1234, 1: 567, 2: 890, ...}
    """
    class_counts = Counter()
    
    for label_file in labels_dir.glob("*.txt"):
        with open(label_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 5:
                    try:
                        class_id = int(parts[0])
                        class_counts[class_id] += 1
                    except ValueError:
                        continue
    
    return dict(class_counts)


def analyze_sizes(
    labels_dir: Path,
    images_dir: Optional[Path] = None,
    default_size: Tuple[int, int] = (1280, 1280)
) -> Dict[int, List[float]]:
    """
    Анализ размеров объектов по классам.
    
    Вычисляет диагональ bbox в пикселях для каждого объекта.
    
    Args:
        labels_dir: Директория с аннотациями
        images_dir: Директория с изображениями (для получения реального размера)
        default_size: Размер по умолчанию если изображение не найдено
        
    Returns:
        Словарь {class_id: [sizes]} где size - диагональ bbox в px
    """
    class_sizes = defaultdict(list)
    
    for label_file in labels_dir.glob("*.txt"):
        # Получить размер изображения
        img_w, img_h = default_size
        
        if images_dir:
            for ext in [".png", ".jpg", ".jpeg"]:
                img_path = images_dir / f"{label_file.stem}{ext}"
                if img_path.exists():
                    try:
                        with Image.open(img_path) as img:
                            img_w, img_h = img.size
                    except Exception:
                        pass
                    break
        
        with open(label_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 5:
                    try:
                        class_id = int(parts[0])
                        width = float(parts[3]) * img_w
                        height = float(parts[4]) * img_h
                        
                        # Диагональ как мера размера
                        diagonal = (width ** 2 + height ** 2) ** 0.5
                        class_sizes[class_id].append(diagonal)
                    except (ValueError, IndexError):
                        continue
    
    return dict(class_sizes)


def get_unique_classes(labels_dir: Path) -> Set[int]:
    """
    Получить множество уникальных классов в датасете.
    
    Args:
        labels_dir: Директория с аннотациями
        
    Returns:
        Set ID классов
    """
    classes = set()
    
    for label_file in labels_dir.glob("*.txt"):
        with open(label_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if parts:
                    try:
                        classes.add(int(parts[0]))
                    except ValueError:
                        continue
    
    return classes


def analyze_dataset(
    labels_dir: Path,
    images_dir: Optional[Path] = None,
    class_names: Optional[Dict[int, str]] = None,
    rare_threshold: int = 50
) -> Dict:
    """
    Полный анализ датасета.
    
    Args:
        labels_dir: Директория с аннотациями
        images_dir: Директория с изображениями (для анализа размеров)
        class_names: Маппинг id -> имя класса
        rare_threshold: Порог для определения редкого класса
        
    Returns:
        Словарь со статистикой:
        - total_files: количество файлов
        - total_objects: общее количество объектов
        - unique_classes: количество уникальных классов
        - class_stats: [{class_id, class_name, count, size_min, size_max, is_rare}]
        - rare_classes: список редких классов
        
    Example:
        >>> stats = analyze_dataset(
        ...     labels_dir=Path("./labels"),
        ...     class_names={0: "armatura_ruchn", ...},
        ...     rare_threshold=50
        ... )
    """
    labels_dir = Path(labels_dir)
    
    if class_names is None:
        class_names = {}
    
    # Подсчет классов
    class_counts = count_classes(labels_dir)
    
    # Анализ размеров
    class_sizes = analyze_sizes(labels_dir, images_dir)
    
    # Формирование статистики
    total_files = len(list(labels_dir.glob("*.txt")))
    total_objects = sum(class_counts.values())
    unique_classes = get_unique_classes(labels_dir)
    
    class_stats = []
    rare_classes = []
    
    for class_id in sorted(class_counts.keys()):
        count = class_counts[class_id]
        sizes = class_sizes.get(class_id, [])
        
        size_min = min(sizes) if sizes else 0
        size_max = max(sizes) if sizes else 0
        size_avg = np.mean(sizes) if sizes else 0
        
        is_rare = count < rare_threshold
        if is_rare:
            rare_classes.append(class_id)
        
        class_stats.append({
            "class_id": class_id,
            "class_name": class_names.get(class_id, f"class_{class_id}"),
            "count": count,
            "percent": count / total_objects * 100 if total_objects > 0 else 0,
            "size_min": round(size_min, 1),
            "size_max": round(size_max, 1),
            "size_avg": round(size_avg, 1),
            "is_rare": is_rare
        })
    
    return {
        "total_files": total_files,
        "total_objects": total_objects,
        "unique_classes": len(unique_classes),
        "class_stats": class_stats,
        "rare_classes": rare_classes
    }


def save_statistics(
    stats: Dict,
    output_path: Path,
    include_header: bool = True
) -> None:
    """
    Сохранить статистику в CSV файл.
    
    Args:
        stats: Результат analyze_dataset()
        output_path: Путь для сохранения
        include_header: Добавить заголовок
        
    Example:
        >>> save_statistics(stats, Path("./stats/train.csv"))
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        
        if include_header:
            writer.writerow([
                "class_id", "class_name", "count", "percent",
                "size_min", "size_max", "size_avg", "is_rare"
            ])
        
        for cls_stat in stats["class_stats"]:
            writer.writerow([
                cls_stat["class_id"],
                cls_stat["class_name"],
                cls_stat["count"],
                f"{cls_stat['percent']:.2f}",
                cls_stat["size_min"],
                cls_stat["size_max"],
                cls_stat["size_avg"],
                "YES" if cls_stat["is_rare"] else "NO"
            ])
    
    print(f"Статистика сохранена: {output_path}")


def print_statistics(stats: Dict, title: str = "Dataset Statistics") -> None:
    """
    Вывести статистику в консоль.
    
    Args:
        stats: Результат analyze_dataset()
        title: Заголовок
    """
    print("\n" + "="*80)
    print(f"{title}")
    print("="*80)
    print(f"Файлов: {stats['total_files']}")
    print(f"Объектов: {stats['total_objects']}")
    print(f"Уникальных классов: {stats['unique_classes']}")
    print(f"Редких классов (<порога): {len(stats['rare_classes'])}")
    print()
    
    # Заголовок таблицы
    print(f"{'ID':>3} {'Класс':<28} {'Кол-во':>8} {'%':>7} {'Min':>6} {'Max':>6} {'Редкий':>7}")
    print("-" * 80)
    
    # Сортировка по количеству (убывание)
    sorted_stats = sorted(stats["class_stats"], key=lambda x: x["count"], reverse=True)
    
    for cls_stat in sorted_stats:
        rare_mark = "ДА" if cls_stat["is_rare"] else ""
        print(f"{cls_stat['class_id']:>3} {cls_stat['class_name']:<28} "
              f"{cls_stat['count']:>8} {cls_stat['percent']:>6.1f}% "
              f"{cls_stat['size_min']:>6.0f} {cls_stat['size_max']:>6.0f} {rare_mark:>7}")
    
    print()
    
    if stats["rare_classes"]:
        print(f"Редкие классы: {stats['rare_classes']}")


def compare_splits(
    splits: Dict[str, Path],
    class_names: Optional[Dict[int, str]] = None,
    output_dir: Optional[Path] = None
) -> Dict[str, Dict]:
    """
    Сравнительный анализ нескольких сплитов.
    
    Args:
        splits: Словарь {split_name: labels_dir}
        class_names: Маппинг классов
        output_dir: Директория для сохранения статистики
        
    Returns:
        Словарь {split_name: stats}
        
    Example:
        >>> splits = {
        ...     "train": Path("./dataset/train/labels"),
        ...     "val": Path("./dataset/val/labels"),
        ...     "test": Path("./dataset/test/labels")
        ... }
        >>> all_stats = compare_splits(splits, class_names)
    """
    all_stats = {}
    all_classes = set()
    
    print("\n" + "="*80)
    print("СРАВНИТЕЛЬНЫЙ АНАЛИЗ СПЛИТОВ")
    print("="*80)
    
    for split_name, labels_dir in splits.items():
        labels_dir = Path(labels_dir)
        
        if not labels_dir.exists():
            print(f"ВНИМАНИЕ: {split_name} не найден: {labels_dir}")
            continue
        
        stats = analyze_dataset(labels_dir, class_names=class_names)
        all_stats[split_name] = stats
        
        classes = get_unique_classes(labels_dir)
        all_classes.update(classes)
        
        print(f"\n{split_name.upper()}:")
        print(f"  Файлов: {stats['total_files']}")
        print(f"  Объектов: {stats['total_objects']}")
        print(f"  Классов: {stats['unique_classes']}")
        
        if output_dir:
            save_statistics(stats, output_dir / f"{split_name}_statistics.csv")
    
    # Проверка покрытия классов
    print("\n" + "-"*40)
    print("Покрытие классов:")
    
    for split_name, stats in all_stats.items():
        split_classes = {s["class_id"] for s in stats["class_stats"]}
        missing = all_classes - split_classes
        
        if missing:
            print(f"  {split_name}: отсутствуют классы {sorted(missing)}")
        else:
            print(f"  {split_name}: все {len(all_classes)} классов")
    
    return all_stats


def find_rare_classes(
    labels_dir: Path,
    threshold: int = 50
) -> List[int]:
    """
    Найти редкие классы (ниже порога).
    
    Args:
        labels_dir: Директория с аннотациями
        threshold: Порог количества объектов
        
    Returns:
        Список ID редких классов
    """
    counts = count_classes(labels_dir)
    return [cls_id for cls_id, count in counts.items() if count < threshold]


def calculate_class_weights(
    labels_dir: Path,
    method: str = "inverse"
) -> Dict[int, float]:
    """
    Рассчитать веса классов для балансировки.
    
    Args:
        labels_dir: Директория с аннотациями
        method: Метод расчета:
                - "inverse": обратно пропорционально частоте
                - "sqrt_inverse": квадратный корень от inverse
                
    Returns:
        Словарь {class_id: weight}
    """
    counts = count_classes(labels_dir)
    total = sum(counts.values())
    
    weights = {}
    
    if method == "inverse":
        for cls_id, count in counts.items():
            weights[cls_id] = total / (count * len(counts))
    elif method == "sqrt_inverse":
        for cls_id, count in counts.items():
            weights[cls_id] = np.sqrt(total / (count * len(counts)))
    else:
        raise ValueError(f"Неизвестный метод: {method}")
    
    # Нормализация
    max_weight = max(weights.values())
    weights = {k: v / max_weight for k, v in weights.items()}
    
    return weights
