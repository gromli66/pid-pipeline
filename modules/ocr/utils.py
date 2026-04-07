"""
Общие утилиты для OCR pipeline.

Извлечено из: 07_surya_all_2.py, recluster_ocr_blocks.py,
recognize_blocks.py, merge_ocr_results.py.
"""

import html
import re
from typing import List, Tuple

import cv2
import numpy as np
from PIL import Image, ImageFont


# ── Константы ─────────────────────────────────────────────

# Убираем лимит PIL на огромные изображения (P&ID бывают 8000x6000)
Image.MAX_IMAGE_PIXELS = None


# ── Геометрия bbox ────────────────────────────────────────

def bbox_area(b: List[int]) -> int:
    """Площадь bbox [x1, y1, x2, y2]."""
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def intersection_area(a: List[int], b: List[int]) -> int:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    return max(0, x2 - x1) * max(0, y2 - y1)


def iou(a: List[int], b: List[int]) -> float:
    """Intersection over Union для двух bbox."""
    inter = intersection_area(a, b)
    union = bbox_area(a) + bbox_area(b) - inter
    return inter / union if union > 0 else 0.0


def containment(inner: List[int], outer: List[int]) -> float:
    """Какая доля площади inner лежит внутри outer."""
    inter = intersection_area(inner, outer)
    area = bbox_area(inner)
    return inter / area if area > 0 else 0.0


# ── Текст ─────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Очистка OCR-текста: html теги, html entities, управляющие символы."""
    if not text:
        return ""
    text = html.unescape(text)
    # <br> → пробел (перенос строки)
    text = re.sub(r'<br\s*/?>', ' ', text)
    # Strip остальных HTML tags: <b>, </b>, <i>, etc.
    text = re.sub(r'</?[a-zA-Z][^>]*>', '', text)
    text = text.replace('\n', ' ')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def is_garbage_text(text: str) -> bool:
    """Текст — мусор (только спецсимволы, <2 значимых символов)."""
    stripped = text.strip()
    if len(stripped) < 2:
        return True
    if all(c in "|-_/\\.,;:!@#$%^&*()[]{}<> \t\n" for c in stripped):
        return True
    return False


# ── Шрифты ────────────────────────────────────────────────

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

_font_cache = {}


def get_font(size: int = 13) -> ImageFont.FreeTypeFont:
    """Загрузить шрифт с поддержкой кириллицы (кэшируется)."""
    if size in _font_cache:
        return _font_cache[size]
    for fp in _FONT_PATHS:
        try:
            font = ImageFont.truetype(fp, size)
            _font_cache[size] = font
            return font
        except (OSError, IOError):
            continue
    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


# ── I/O ───────────────────────────────────────────────────

def load_image_cv2(path: str) -> np.ndarray:
    """Загрузка изображения через OpenCV (поддержка Unicode путей)."""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Cannot load image: {path}")
    return img


def load_image_pil(path: str) -> Image.Image:
    """Загрузка изображения через PIL."""
    return Image.open(str(path)).convert("RGB")
