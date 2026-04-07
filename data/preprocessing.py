"""
Предобработка изображений для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Единый модуль бинаризации и фильтрации аннотаций.
Все пайплайны (train, finetune, inference, test) используют одну функцию binarize_image().

ПАЙПЛАЙН БИНАРИЗАЦИИ:
--------------------
  full:  Grayscale → NLM → CLAHE → Otsu → Dilate  (обучение, максимальное качество)
  otsu:  Grayscale → Otsu                           (инференс, быстрая обработка)

Адаптация под размер изображения:
  - NLM автоматически уменьшает searchWindowSize для изображений > 8000px
    (иначе минуты на одну схему 14000×10000)

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.data.preprocessing import binarize_image, preprocess_images

    # Единичное изображение
    binary = binarize_image(Path("scheme.png"), method="full")
    binary = binarize_image(Path("scheme.png"), method="otsu")

    # Батч
    preprocess_images(input_dir, output_dir, method="full")
"""

import cv2
import shutil
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from tqdm import tqdm


# ─────────────────────────────────────────────────────────────────────────────
# Бинаризация
# ─────────────────────────────────────────────────────────────────────────────

def binarize_image(
    source: Union[Path, str, np.ndarray],
    method: str = "full",
    nlm_h: int = 10,
    nlm_template: int = 7,
    nlm_search: int = 21,
    clahe_clip: float = 2.0,
    clahe_grid: Tuple[int, int] = (8, 8),
    dilate_kernel: int = 2,
    dilate_iterations: int = 1,
    adaptive_nlm: bool = True,
    large_image_threshold: int = 8000,
) -> np.ndarray:
    """
    Бинаризация изображения P&ID схемы.

    Единая точка входа для всех модулей. Поддерживает два метода:
      - "full":  NLM → CLAHE → Otsu → Dilate (для обучения)
      - "otsu":  только Otsu (для инференса и быстрой обработки)

    Args:
        source: путь к файлу или numpy array (BGR/grayscale)
        method: "full" или "otsu"
        nlm_h: сила шумоподавления NLM (выше = сильнее, но теряет детали)
        nlm_template: размер окна шаблона NLM (нечётное)
        nlm_search: размер окна поиска NLM (нечётное, основной параметр скорости)
        clahe_clip: порог контраста CLAHE
        clahe_grid: размер сетки CLAHE
        dilate_kernel: размер ядра дилатации (утолщение чёрных линий)
        dilate_iterations: количество итераций дилатации
        adaptive_nlm: уменьшать searchWindow для больших изображений
        large_image_threshold: порог "большого" изображения (max dimension, px)

    Returns:
        Бинарное изображение (uint8, 0/255, одноканальное)

    Raises:
        ValueError: если изображение не удалось загрузить
    """
    # Загрузка
    if isinstance(source, (str, Path)):
        img = cv2.imread(str(source))
        if img is None:
            raise ValueError(f"Не удалось загрузить изображение: {source}")
    else:
        img = source

    # Grayscale
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img.copy()

    if method == "otsu":
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return binary

    # method == "full"
    # 1. NLM — шумоподавление с сохранением краёв
    if adaptive_nlm:
        max_dim = max(gray.shape[:2])
        if max_dim > large_image_threshold:
            # Для больших изображений уменьшаем окно поиска
            # 14000px → searchWindow=11, 10000px → searchWindow=15
            scale = large_image_threshold / max_dim
            nlm_search = max(11, int(nlm_search * scale) | 1)  # | 1 → нечётное

    filtered = cv2.fastNlMeansDenoising(
        gray, None,
        h=nlm_h,
        templateWindowSize=nlm_template,
        searchWindowSize=nlm_search,
    )

    # 2. CLAHE — адаптивное выравнивание контраста
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=clahe_grid)
    enhanced = clahe.apply(filtered)

    # 3. Otsu — автоматический порог
    _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 4. Дилатация — утолщение чёрных линий
    #    erode на белом фоне = утолщение чёрных линий
    if dilate_kernel > 0 and dilate_iterations > 0:
        kernel = np.ones((dilate_kernel, dilate_kernel), np.uint8)
        binary = cv2.erode(binary, kernel, iterations=dilate_iterations)

    return binary


def binarize_for_yolo(
    source: Union[Path, str, np.ndarray],
    method: str = "full",
    **kwargs,
) -> np.ndarray:
    """
    Бинаризация + конвертация в 3 канала (BGR) для YOLO.

    Обёртка над binarize_image() для инференса, где YOLO ожидает 3-канальный вход.

    Returns:
        3-канальное бинарное изображение (uint8, BGR)
    """
    binary = binarize_image(source, method=method, **kwargs)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


# Обратная совместимость: старое имя → новая функция
def convert_to_grayscale(image_path: Path) -> np.ndarray:
    """Deprecated: используйте binarize_image(). Оставлено для обратной совместимости."""
    return binarize_image(image_path, method="full")


# ─────────────────────────────────────────────────────────────────────────────
# Батч-обработка
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_images(
    input_dir: Path,
    output_dir: Path,
    method: str = "full",
    extensions: List[str] = None,
    skip_existing: bool = False,
) -> int:
    """
    Бинаризация всех изображений в директории.

    Args:
        input_dir: директория с исходными изображениями
        output_dir: директория для результатов (PNG)
        method: метод бинаризации ("full" или "otsu")
        extensions: расширения файлов (по умолчанию .png, .jpg, .jpeg)
        skip_existing: пропускать уже обработанные

    Returns:
        Количество обработанных изображений
    """
    if extensions is None:
        extensions = [".png", ".jpg", ".jpeg"]

    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for ext in extensions:
        files.extend(input_dir.glob(f"*{ext}"))

    if not files:
        print(f"Внимание: не найдено изображений в {input_dir}")
        return 0

    method_label = "NLM+CLAHE+Otsu+Dilate" if method == "full" else "Otsu"
    processed = 0

    for img_path in tqdm(files, desc=f"Binarize ({method_label})", unit="img"):
        output_path = output_dir / f"{img_path.stem}.png"

        if skip_existing and output_path.exists():
            continue

        try:
            result = binarize_image(img_path, method=method)
            cv2.imwrite(str(output_path), result)
            processed += 1
        except ValueError as e:
            print(f"Ошибка: {e}")

    print(f"Обработано: {processed}/{len(files)}")
    return processed


# ─────────────────────────────────────────────────────────────────────────────
# Фильтрация аннотаций
# ─────────────────────────────────────────────────────────────────────────────

def filter_labels(
    label_path: Path,
    classes_to_remove: List[int],
    reindex_mapping: Optional[Dict[int, int]] = None,
) -> Tuple[List[str], int, int]:
    """
    Фильтрация YOLO-аннотаций: удаление классов + переиндексация.

    Args:
        label_path: путь к .txt файлу аннотаций
        classes_to_remove: ID классов для удаления
        reindex_mapping: старый_id → новый_id (например {35: 34, 38: 35})

    Returns:
        (отфильтрованные строки, удалено, всего)
    """
    remove_set = set(classes_to_remove)
    reindex = reindex_mapping or {}

    with open(label_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total = len(lines)
    filtered = []
    removed = 0

    for line in lines:
        parts = line.strip().split()
        if len(parts) < 5:
            continue

        try:
            class_id = int(parts[0])
        except ValueError:
            continue

        if class_id in remove_set:
            removed += 1
        else:
            new_id = reindex.get(class_id, class_id)
            filtered.append(f"{new_id} {' '.join(parts[1:])}\n")

    return filtered, removed, total


def remove_classes(
    images_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    classes_to_remove: List[int],
    reindex_mapping: Optional[Dict[int, int]] = None,
    skip_empty: bool = True,
) -> dict:
    """
    Удаление классов из датасета: копирование изображений + фильтрация аннотаций.

    Args:
        images_dir: директория с изображениями
        labels_dir: директория с YOLO-аннотациями
        output_dir: директория результатов (создаёт images/ и labels/)
        classes_to_remove: ID классов для удаления
        reindex_mapping: маппинг переиндексации
        skip_empty: пропускать изображения без аннотаций после фильтрации

    Returns:
        Словарь со статистикой
    """
    images_dir = Path(images_dir)
    labels_dir = Path(labels_dir)
    output_dir = Path(output_dir)

    output_images = output_dir / "images"
    output_labels = output_dir / "labels"
    output_images.mkdir(parents=True, exist_ok=True)
    output_labels.mkdir(parents=True, exist_ok=True)

    stats = {
        "total_files": 0,
        "processed_files": 0,
        "skipped_empty": 0,
        "total_annotations": 0,
        "removed_annotations": 0,
        "kept_annotations": 0,
    }

    label_files = list(labels_dir.glob("*.txt"))
    stats["total_files"] = len(label_files)

    for label_path in tqdm(label_files, desc="Removing classes", unit="file"):
        filtered_lines, removed, total = filter_labels(
            label_path, classes_to_remove, reindex_mapping
        )

        stats["total_annotations"] += total
        stats["removed_annotations"] += removed
        stats["kept_annotations"] += len(filtered_lines)

        if skip_empty and not filtered_lines:
            stats["skipped_empty"] += 1
            continue

        # Найти изображение
        img_name = label_path.stem
        img_path = None
        for ext in [".png", ".jpg", ".jpeg"]:
            candidate = images_dir / f"{img_name}{ext}"
            if candidate.exists():
                img_path = candidate
                break

        if img_path is None:
            print(f"Внимание: изображение не найдено для {label_path.name}")
            continue

        # Сохранить
        with open(output_labels / label_path.name, "w", encoding="utf-8") as f:
            f.writelines(filtered_lines)
        shutil.copy(img_path, output_images / f"{img_name}.png")

        stats["processed_files"] += 1

    print(f"\n=== Статистика удаления классов ===")
    print(f"Файлов всего:     {stats['total_files']}")
    print(f"Обработано:       {stats['processed_files']}")
    print(f"Пропущено пустых: {stats['skipped_empty']}")
    print(f"Аннотаций всего:  {stats['total_annotations']}")
    print(f"Удалено:          {stats['removed_annotations']}")
    print(f"Оставлено:        {stats['kept_annotations']}")

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Полный пайплайн
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_pipeline(
    raw_images_dir: Path,
    raw_labels_dir: Path,
    output_dir: Path,
    classes_to_remove: List[int],
    reindex_mapping: Optional[Dict[int, int]] = None,
    grayscale: bool = True,
    binarize_method: str = "full",
) -> Path:
    """
    Полный пайплайн: бинаризация → удаление классов → переиндексация.

    Args:
        raw_images_dir: исходные изображения
        raw_labels_dir: исходные аннотации
        output_dir: директория результатов
        classes_to_remove: классы для удаления
        reindex_mapping: маппинг переиндексации
        grayscale: выполнять бинаризацию
        binarize_method: метод бинаризации ("full" / "otsu")

    Returns:
        Путь к output_dir / "cleaned"
    """
    output_dir = Path(output_dir)

    print("\n" + "=" * 60)
    print("ПРЕДОБРАБОТКА ДАННЫХ")
    print("=" * 60)

    if grayscale:
        method_label = "NLM+CLAHE+Otsu+Dilate" if binarize_method == "full" else "Otsu"
        print(f"\n[1/2] Бинаризация ({method_label})...")
        grayscale_dir = output_dir / "grayscale"
        preprocess_images(raw_images_dir, grayscale_dir, method=binarize_method)
        images_for_cleaning = grayscale_dir
    else:
        images_for_cleaning = raw_images_dir

    print("\n[2/2] Удаление нерелевантных классов...")
    if reindex_mapping:
        print(f"Переиндексация: {reindex_mapping}")

    cleaned_dir = output_dir / "cleaned"
    remove_classes(
        images_dir=images_for_cleaning,
        labels_dir=raw_labels_dir,
        output_dir=cleaned_dir,
        classes_to_remove=classes_to_remove,
        reindex_mapping=reindex_mapping,
    )

    print(f"\nРезультат: {cleaned_dir}")
    return cleaned_dir