"""
Бинаризация изображений для YOLO-инференса.

Перенос единого препроцессинга из обучающего пакета pid_node_detection
(data/preprocessing.py). Модели ансамбля обучены на бинаризованных
изображениях, поэтому инференс должен использовать ту же бинаризацию.

ПАЙПЛАЙН:
  full:  Grayscale → NLM → CLAHE → Otsu → Erode  (максимальное качество)
  otsu:  Grayscale → Otsu                        (быстрая обработка)

Адаптация под размер изображения:
  - NLM автоматически уменьшает searchWindowSize для изображений > 8000px
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Tuple, Union


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

    Args:
        source: путь к файлу или numpy array (BGR/grayscale)
        method: "full" или "otsu"
        nlm_h: сила шумоподавления NLM
        nlm_template: размер окна шаблона NLM (нечётное)
        nlm_search: размер окна поиска NLM (нечётное)
        clahe_clip: порог контраста CLAHE
        clahe_grid: размер сетки CLAHE
        dilate_kernel: размер ядра утолщения чёрных линий
        dilate_iterations: количество итераций
        adaptive_nlm: уменьшать searchWindow для больших изображений
        large_image_threshold: порог "большого" изображения (px)

    Returns:
        Бинарное изображение (uint8, 0/255, одноканальное)

    Raises:
        ValueError: если изображение не удалось загрузить
    """
    if isinstance(source, (str, Path)):
        img = cv2.imread(str(source))
        if img is None:
            raise ValueError(f"Не удалось загрузить изображение: {source}")
    else:
        img = source

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

    # 4. Утолщение чёрных линий (erode на белом фоне)
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

    Returns:
        3-канальное бинарное изображение (uint8, BGR)
    """
    binary = binarize_image(source, method=method, **kwargs)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
