"""
Разбиение данных для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль выполняет стратифицированное разбиение данных на train/val/test:
1. Первый этап: Разделение схем на Test и TrainVal (ДО тайлинга)
2. Второй этап: Разделение тайлов на Train и Val (ПОСЛЕ тайлинга)

ПОЧЕМУ ДВУХЭТАПНОЕ РАЗБИЕНИЕ:
----------------------------
1. Test выделяется на уровне СХЕМ:
   - Предотвращает утечку данных (тайлы одной схемы не попадут в train и test)
   - Test содержит полноразмерные оригинальные изображения
   - На тесте используется SAHI для слайсинга (как в продакшене)

2. Train/Val выделяется на уровне ТАЙЛОВ:
   - Позволяет использовать больше данных для обучения
   - Val используется для early stopping и подбора гиперпараметров
   - Тайлы одной схемы могут быть в train и val (это нормально)

СТРАТИФИКАЦИЯ:
-------------
Гарантирует наличие редких классов во всех сплитах:
- Для редких классов (<=3 схемы): ВСЕ схемы в TrainVal
- Для редких классов (4-5 схем): минимум 1 в Test, остальные в TrainVal
- Для обычных классов: 20% Test / 80% TrainVal

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.data.splitting import split_data
    from pid_node_detection.config import load_config, load_classes
    
    # Первый этап: разделение схем
    split_data(
        images_dir=Path("./cleaned/images"),
        labels_dir=Path("./cleaned/labels"),
        output_dir=Path("./split"),
        test_ratio=0.2,
        rare_classes=[7, 9, 10, 12, ...]
    )
    
    # Результат:
    # ./split/test/images, ./split/test/labels - оригинальные схемы для теста
    # ./split/trainval/images, ./split/trainval/labels - схемы для train+val
"""

import random
import shutil
from pathlib import Path
from typing import List, Dict, Set, Tuple, Optional
from collections import defaultdict
from tqdm import tqdm


def get_scheme_classes(label_path: Path) -> Set[int]:
    """
    Получить множество классов в схеме.
    
    Читает файл аннотаций и возвращает set уникальных class_id.
    
    Args:
        label_path: Путь к файлу аннотаций (.txt)
        
    Returns:
        Множество ID классов в схеме
        
    Example:
        >>> classes = get_scheme_classes(Path("labels/scheme1.txt"))
        >>> print(classes)  # {0, 3, 5, 11, 17}
    """
    classes = set()
    
    if not label_path.exists():
        return classes
    
    with open(label_path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if parts:
                try:
                    classes.add(int(parts[0]))
                except ValueError:
                    continue
    
    return classes


def build_class_scheme_mapping(
    labels_dir: Path
) -> Tuple[Dict[str, Set[int]], Dict[int, List[str]]]:
    """
    Построить маппинги схема→классы и класс→схемы.
    
    Args:
        labels_dir: Директория с файлами аннотаций
        
    Returns:
        Tuple из:
        - scheme_to_classes: {scheme_name: {class_ids}}
        - class_to_schemes: {class_id: [scheme_names]}
        
    Example:
        >>> s2c, c2s = build_class_scheme_mapping(Path("./labels"))
        >>> print(c2s[7])  # Схемы с классом 7
    """
    scheme_to_classes = {}
    class_to_schemes = defaultdict(list)
    
    for label_path in labels_dir.glob("*.txt"):
        scheme_name = label_path.stem
        classes = get_scheme_classes(label_path)
        
        scheme_to_classes[scheme_name] = classes
        
        for cls in classes:
            class_to_schemes[cls].append(scheme_name)
    
    return scheme_to_classes, dict(class_to_schemes)


def stratified_split_schemes(
    scheme_to_classes: Dict[str, Set[int]],
    class_to_schemes: Dict[int, List[str]],
    test_ratio: float,
    rare_classes: List[int],
    random_seed: int = 42
) -> Tuple[List[str], List[str]]:
    """
    Стратифицированное разбиение схем на Test и TrainVal.
    
    Алгоритм:
    1. Для каждого редкого класса:
       - Если <=3 схемы: ВСЕ в TrainVal (не рискуем потерять)
       - Иначе: минимум 1 в Test, остальные в TrainVal
    2. Оставшиеся схемы распределяются для достижения target test_ratio
    
    Args:
        scheme_to_classes: Маппинг схема → классы
        class_to_schemes: Маппинг класс → схемы
        test_ratio: Доля данных для теста (0-1)
        rare_classes: Список редких классов
        random_seed: Seed для воспроизводимости
        
    Returns:
        Tuple (test_schemes, trainval_schemes) - списки имен схем
        
    Example:
        >>> test, trainval = stratified_split_schemes(
        ...     scheme_to_classes, class_to_schemes,
        ...     test_ratio=0.2,
        ...     rare_classes=[7, 9, 10]
        ... )
    """
    random.seed(random_seed)
    
    all_schemes = list(scheme_to_classes.keys())
    total = len(all_schemes)
    
    # Проверка на пустые данные
    if total == 0:
        print("\n⚠️  ВНИМАНИЕ: Нет данных для разбиения!")
        print("Проверьте путь к изображениям и аннотациям в конфиге.")
        return [], []
    
    # Результат: scheme_name -> "test" | "trainval"
    scheme_assignment = {}
    
    print(f"\nСтратифицированное разбиение схем:")
    print(f"Всего схем: {total}")
    print(f"Target test ratio: {test_ratio}")
    print(f"Редких классов: {len(rare_classes)}")
    print()
    
    # Шаг 1: Обработка редких классов
    for cls in rare_classes:
        schemes = class_to_schemes.get(cls, [])
        n_schemes = len(schemes)
        
        if n_schemes == 0:
            continue
        
        schemes_copy = schemes.copy()
        random.shuffle(schemes_copy)
        
        if n_schemes <= 3:
            # Все в trainval - слишком мало для разделения
            for scheme in schemes_copy:
                if scheme not in scheme_assignment:
                    scheme_assignment[scheme] = "trainval"
            print(f"  Класс {cls:2d}: {n_schemes} схем -> ВСЕ в TrainVal")
        else:
            # Минимум 1 в test
            test_assigned = False
            for scheme in schemes_copy:
                if scheme not in scheme_assignment:
                    if not test_assigned:
                        scheme_assignment[scheme] = "test"
                        test_assigned = True
                    else:
                        scheme_assignment[scheme] = "trainval"
            
            test_count = sum(1 for s in schemes_copy 
                           if scheme_assignment.get(s) == "test")
            trainval_count = n_schemes - test_count
            print(f"  Класс {cls:2d}: {n_schemes} схем -> {trainval_count} TrainVal, {test_count} Test")
    
    # Шаг 2: Распределение оставшихся схем
    unassigned = [s for s in all_schemes if s not in scheme_assignment]
    print(f"\nОставшихся схем для распределения: {len(unassigned)}")
    
    random.shuffle(unassigned)
    
    # Считаем сколько нужно в test
    target_test = int(total * test_ratio)
    current_test = sum(1 for s in scheme_assignment.values() if s == "test")
    needed_test = max(0, target_test - current_test)
    
    print(f"Target test: {target_test}, уже в test: {current_test}, нужно добавить: {needed_test}")
    
    for i, scheme in enumerate(unassigned):
        if i < needed_test:
            scheme_assignment[scheme] = "test"
        else:
            scheme_assignment[scheme] = "trainval"
    
    # Формируем результат
    test_schemes = [s for s, split in scheme_assignment.items() if split == "test"]
    trainval_schemes = [s for s, split in scheme_assignment.items() if split == "trainval"]
    
    # Проверка на пересечения
    assert len(set(test_schemes) & set(trainval_schemes)) == 0, "Обнаружено пересечение!"
    
    # Статистика по классам
    trainval_classes = set()
    test_classes = set()
    
    for scheme in trainval_schemes:
        trainval_classes.update(scheme_to_classes[scheme])
    for scheme in test_schemes:
        test_classes.update(scheme_to_classes[scheme])
    
    print(f"\nИтого:")
    print(f"  Test: {len(test_schemes)} схем ({len(test_schemes)/total*100:.1f}%)")
    print(f"  TrainVal: {len(trainval_schemes)} схем ({len(trainval_schemes)/total*100:.1f}%)")
    print(f"  Классов в TrainVal: {len(trainval_classes)}")
    print(f"  Классов в Test: {len(test_classes)}")
    
    # Проверка что все редкие классы в trainval
    missing_rare = set(rare_classes) - trainval_classes
    if missing_rare:
        print(f"  ВНИМАНИЕ: Редкие классы отсутствуют в TrainVal: {missing_rare}")
    else:
        print(f"  Все редкие классы присутствуют в TrainVal")
    
    return test_schemes, trainval_schemes


def copy_split_data(
    schemes: List[str],
    images_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    split_name: str
) -> int:
    """
    Копирование файлов для одного сплита.
    
    Args:
        schemes: Список имен схем для копирования
        images_dir: Исходная директория с изображениями
        labels_dir: Исходная директория с аннотациями
        output_dir: Базовая выходная директория
        split_name: Название сплита ("test", "trainval", "train", "val")
        
    Returns:
        Количество скопированных файлов
    """
    split_dir = output_dir / split_name
    images_out = split_dir / "images"
    labels_out = split_dir / "labels"
    
    images_out.mkdir(parents=True, exist_ok=True)
    labels_out.mkdir(parents=True, exist_ok=True)
    
    copied = 0
    
    for scheme_name in tqdm(schemes, desc=f"Copying {split_name}", unit="file"):
        # Копировать изображение
        src_img = None
        for ext in [".png", ".jpg", ".jpeg"]:
            candidate = images_dir / f"{scheme_name}{ext}"
            if candidate.exists():
                src_img = candidate
                break
        
        if src_img:
            dst_img = images_out / f"{scheme_name}.png"
            shutil.copy(src_img, dst_img)
        
        # Копировать аннотации
        src_label = labels_dir / f"{scheme_name}.txt"
        if src_label.exists():
            dst_label = labels_out / f"{scheme_name}.txt"
            shutil.copy(src_label, dst_label)
            copied += 1
    
    return copied


def split_data(
    images_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    test_ratio: float = 0.2,
    rare_classes: Optional[List[int]] = None,
    random_seed: int = 42
) -> Dict[str, Path]:
    """
    Разделение данных на Test и TrainVal (первый этап).
    
    Выполняет стратифицированное разбиение на уровне СХЕМ.
    Test получает полноразмерные оригинальные изображения.
    TrainVal далее пойдет на тайлинг.
    
    Args:
        images_dir: Директория с изображениями
        labels_dir: Директория с аннотациями
        output_dir: Директория для результатов
        test_ratio: Доля данных для теста
        rare_classes: Список редких классов для стратификации
        random_seed: Seed для воспроизводимости
        
    Returns:
        Словарь с путями: {"test": Path, "trainval": Path}
        
    Example:
        >>> paths = split_data(
        ...     images_dir=Path("./cleaned/images"),
        ...     labels_dir=Path("./cleaned/labels"),
        ...     output_dir=Path("./split"),
        ...     test_ratio=0.2,
        ...     rare_classes=[7, 9, 10, 12]
        ... )
    """
    images_dir = Path(images_dir)
    labels_dir = Path(labels_dir)
    output_dir = Path(output_dir)
    
    if rare_classes is None:
        rare_classes = []
    
    print("\n" + "="*60)
    print("РАЗБИЕНИЕ ДАННЫХ (схемы -> Test / TrainVal)")
    print("="*60)
    
    # Построить маппинги
    scheme_to_classes, class_to_schemes = build_class_scheme_mapping(labels_dir)
    
    # Стратифицированное разбиение
    test_schemes, trainval_schemes = stratified_split_schemes(
        scheme_to_classes=scheme_to_classes,
        class_to_schemes=class_to_schemes,
        test_ratio=test_ratio,
        rare_classes=rare_classes,
        random_seed=random_seed
    )
    
    # Проверка на пустые данные
    if not test_schemes and not trainval_schemes:
        raise ValueError(
            "Нет данных для разбиения!\n"
            "Проверьте:\n"
            f"  1. Путь к изображениям: {images_dir}\n"
            f"  2. Путь к аннотациям: {labels_dir}\n"
            "  3. Наличие .png файлов в директории изображений\n"
            "  4. Наличие .txt файлов в директории аннотаций"
        )
    
    # Копирование файлов
    print("\nКопирование файлов...")
    
    test_count = copy_split_data(
        test_schemes, images_dir, labels_dir, output_dir, "test"
    )
    trainval_count = copy_split_data(
        trainval_schemes, images_dir, labels_dir, output_dir, "trainval"
    )
    
    print(f"\nСкопировано:")
    print(f"  Test: {test_count} схем")
    print(f"  TrainVal: {trainval_count} схем")
    
    return {
        "test": output_dir / "test",
        "trainval": output_dir / "trainval"
    }


def stratified_split_tiles(
    tiles_dir: Path,
    output_dir: Path,
    val_ratio: float = 0.3,
    random_seed: int = 42
) -> Dict[str, Path]:
    """
    Стратифицированное разбиение тайлов на Train и Val (второй этап).
    
    Гарантирует что каждый класс присутствует минимум в одном тайле
    как в Train, так и в Val.
    
    Args:
        tiles_dir: Директория с тайлами (после тайлинга)
                  Ожидается структура: tiles_dir/images, tiles_dir/labels
        output_dir: Директория для результатов
        val_ratio: Доля данных для валидации
        random_seed: Seed для воспроизводимости
        
    Returns:
        Словарь с путями: {"train": Path, "val": Path}
        
    Example:
        >>> paths = stratified_split_tiles(
        ...     tiles_dir=Path("./trainval_tiles"),
        ...     output_dir=Path("./dataset"),
        ...     val_ratio=0.3
        ... )
    """
    random.seed(random_seed)
    
    tiles_dir = Path(tiles_dir)
    output_dir = Path(output_dir)
    
    labels_dir = tiles_dir / "labels"
    images_dir = tiles_dir / "images"
    masks_dir = tiles_dir / "forbidden_masks"
    
    print("\n" + "="*60)
    print("РАЗБИЕНИЕ ТАЙЛОВ (Train / Val)")
    print("="*60)
    
    # Собрать все тайлы и их классы
    all_tiles = [f.stem for f in labels_dir.glob("*.txt")]
    print(f"Всего тайлов: {len(all_tiles)}")
    
    tile_to_classes = {}
    for tile in all_tiles:
        classes = get_scheme_classes(labels_dir / f"{tile}.txt")
        tile_to_classes[tile] = classes
    
    # Все уникальные классы
    all_classes = set()
    for classes in tile_to_classes.values():
        all_classes.update(classes)
    
    print(f"Уникальных классов: {len(all_classes)}")
    
    # Стратифицированное разбиение
    train_tiles = set()
    val_tiles = set()
    remaining_tiles = set(all_tiles)
    
    # Гарантируем минимум 1 тайл каждого класса в train и val
    for cls_id in all_classes:
        cls_tiles = [t for t in all_tiles if cls_id in tile_to_classes[t]]
        
        # Один в train
        train_chosen = None
        for t in cls_tiles:
            if t not in train_tiles and t not in val_tiles:
                train_chosen = t
                break
        if train_chosen:
            train_tiles.add(train_chosen)
            remaining_tiles.discard(train_chosen)
        
        # Один в val
        val_chosen = None
        for t in cls_tiles:
            if t not in train_tiles and t not in val_tiles:
                val_chosen = t
                break
        if val_chosen:
            val_tiles.add(val_chosen)
            remaining_tiles.discard(val_chosen)
    
    # Распределяем оставшиеся для достижения target val_ratio
    target_val_count = int(len(all_tiles) * val_ratio)
    remaining_list = list(remaining_tiles)
    random.shuffle(remaining_list)
    
    for t in remaining_list:
        if len(val_tiles) < target_val_count:
            val_tiles.add(t)
        else:
            train_tiles.add(t)
    
    print(f"\nРезультат:")
    print(f"  Train: {len(train_tiles)} тайлов ({len(train_tiles)/len(all_tiles)*100:.1f}%)")
    print(f"  Val: {len(val_tiles)} тайлов ({len(val_tiles)/len(all_tiles)*100:.1f}%)")
    
    # Проверка покрытия классов
    train_classes = set()
    val_classes = set()
    for tile in train_tiles:
        train_classes.update(tile_to_classes[tile])
    for tile in val_tiles:
        val_classes.update(tile_to_classes[tile])
    
    print(f"  Классов в Train: {len(train_classes)}")
    print(f"  Классов в Val: {len(val_classes)}")
    
    if train_classes != all_classes:
        missing = all_classes - train_classes
        print(f"  ВНИМАНИЕ: отсутствуют в Train: {missing}")
    
    if val_classes != all_classes:
        missing = all_classes - val_classes
        print(f"  ВНИМАНИЕ: отсутствуют в Val: {missing}")
    
    # Копирование файлов
    print("\nКопирование файлов...")
    
    for split_name, tiles in [("train", train_tiles), ("val", val_tiles)]:
        split_images = output_dir / split_name / "images"
        split_labels = output_dir / split_name / "labels"
        split_masks = output_dir / split_name / "forbidden_masks"
        
        split_images.mkdir(parents=True, exist_ok=True)
        split_labels.mkdir(parents=True, exist_ok=True)
        split_masks.mkdir(parents=True, exist_ok=True)
        
        for tile_name in tqdm(tiles, desc=f"Copying {split_name}", unit="tile"):
            # Изображение
            src_img = images_dir / f"{tile_name}.png"
            if src_img.exists():
                shutil.copy(src_img, split_images / f"{tile_name}.png")
            
            # Аннотации
            src_label = labels_dir / f"{tile_name}.txt"
            if src_label.exists():
                shutil.copy(src_label, split_labels / f"{tile_name}.txt")
            
            # Маски (если есть)
            src_mask = masks_dir / f"{tile_name}_forbidden.png"
            if src_mask.exists():
                shutil.copy(src_mask, split_masks / f"{tile_name}_forbidden.png")
    
    return {
        "train": output_dir / "train",
        "val": output_dir / "val"
    }
