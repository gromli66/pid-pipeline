"""
Step 3.5: Сохранение кропов на диск.

Извлечено из: recognize_blocks.py (prepare_crops + save_crops)

Вход: original/image.png + reclustered_blocks.json
Выход: ocr/crops/ с PNG файлами
"""

import json
import logging
from pathlib import Path
from typing import List, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None

PAD = 5
MIN_CROP_SIZE = 64


def crop_with_pad(image: Image.Image, bbox: List[int], pad: int = PAD) -> Tuple[Image.Image, Tuple[int, int]]:
    """Кроп из оригинала с фиксированным паддингом."""
    W, H = image.size
    x1, y1, x2, y2 = bbox
    cx1 = max(0, x1 - pad)
    cy1 = max(0, y1 - pad)
    cx2 = min(W, x2 + pad)
    cy2 = min(H, y2 + pad)
    return image.crop((cx1, cy1, cx2, cy2)), (cx1, cy1)


def pad_to_min_size(crop: Image.Image, min_size: int = MIN_CROP_SIZE) -> Image.Image:
    """Добивка белым до минимального размера."""
    w, h = crop.size
    if w >= min_size and h >= min_size:
        return crop
    new_w = max(w, min_size)
    new_h = max(h, min_size)
    canvas = Image.new("RGB", (new_w, new_h), (255, 255, 255))
    canvas.paste(crop, ((new_w - w) // 2, (new_h - h) // 2))
    return canvas


def save_crops_from_blocks(original_path: str, blocks_json_path: str, ocr_dir: str):
    """
    Создать и сохранить кропы для всех блоков.

    Args:
        original_path: путь к original/image.png
        blocks_json_path: путь к reclustered_blocks.json
        ocr_dir: папка OCR (кропы → ocr_dir/crops/)
    """
    image = Image.open(original_path).convert("RGB")
    with open(blocks_json_path, encoding="utf-8") as f:
        blocks = json.load(f)

    crops_dir = Path(ocr_dir) / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    for i, block in enumerate(blocks):
        bbox = block["bbox"]
        crop, _ = crop_with_pad(image, bbox)
        crop = pad_to_min_size(crop)
        x1, y1, x2, y2 = bbox
        fname = f"{i:04d}_{x1}_{y1}_{x2}_{y2}.png"
        crop.save(str(crops_dir / fname))

    logger.info("Saved %d crops → %s", len(blocks), crops_dir)
