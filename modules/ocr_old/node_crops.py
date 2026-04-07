"""
Step 3.6: Добавление кропов узлов из coco_validated.json.

KKS-коды могут быть написаны внутри узлов (оборудования).
После вырезания узлов маской (clean_pid) этот текст теряется.
Здесь мы вырезаем каждый узел из оригинального изображения и добавляем
его как дополнительный OCR-блок.

Вход:
  - detection/coco_validated.json  (COCO формат, может содержать полигоны)
  - original/image.png
  - ocr/reclustered_blocks.json    (существующие блоки)
  - ocr/crops/                     (папка с кропами)

Выход:
  - Обновлённый reclustered_blocks.json (с добавленными node_crop блоками)
  - Новые crop-файлы в ocr/crops/
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None

# Минимальный размер кропа узла (слишком мелкие — вряд ли содержат KKS)
MIN_NODE_CROP_SIDE = 30
MIN_NODE_CROP_AREA = 900  # 30x30

# Паддинг вокруг bbox узла
NODE_CROP_PAD = 5


def _polygon_to_bbox(segmentation: list) -> Optional[Tuple[int, int, int, int]]:
    """
    Извлечь bbox из COCO segmentation (список полигонов).

    COCO segmentation: [[x1,y1,x2,y2,...], ...] — один или несколько полигонов.
    Возвращает: (x1, y1, x2, y2) или None.
    """
    all_x, all_y = [], []
    for polygon in segmentation:
        if not polygon or len(polygon) < 6:
            continue
        xs = polygon[0::2]
        ys = polygon[1::2]
        all_x.extend(xs)
        all_y.extend(ys)

    if not all_x:
        return None

    return (
        int(min(all_x)),
        int(min(all_y)),
        int(max(all_x)),
        int(max(all_y)),
    )


def _create_polygon_mask(
    segmentation: list, bbox: Tuple[int, int, int, int]
) -> Optional[np.ndarray]:
    """
    Создать бинарную маску полигона внутри bbox.

    Returns:
        Маска (h, w) dtype=uint8, 255 внутри полигона, 0 снаружи.
        None если полигон невалидный.
    """
    x1, y1, x2, y2 = bbox
    h, w = y2 - y1, x2 - x1
    if h <= 0 or w <= 0:
        return None

    mask = np.zeros((h, w), dtype=np.uint8)

    for polygon in segmentation:
        if not polygon or len(polygon) < 6:
            continue
        pts = np.array(polygon, dtype=np.float32).reshape(-1, 2)
        # Сдвигаем координаты относительно bbox
        pts[:, 0] -= x1
        pts[:, 1] -= y1
        pts = pts.astype(np.int32)
        cv2.fillPoly(mask, [pts], 255)

    return mask if np.any(mask) else None


def _crop_node(
    image: np.ndarray,
    ann: dict,
    img_width: int,
    img_height: int,
) -> Optional[Tuple[np.ndarray, List[int]]]:
    """
    Вырезать кроп узла из изображения.

    Если есть segmentation — маскируем область за полигоном белым.
    Если только bbox — кропаем прямоугольник.

    Returns:
        (crop_bgr, [x1, y1, x2, y2]) или None.
    """
    H, W = image.shape[:2]

    segmentation = ann.get("segmentation", [])
    coco_bbox = ann.get("bbox", [])

    # Определяем bbox
    if segmentation and isinstance(segmentation, list) and len(segmentation) > 0:
        # Проверяем что это полигоны, а не RLE
        if isinstance(segmentation[0], list):
            poly_bbox = _polygon_to_bbox(segmentation)
            if poly_bbox:
                x1, y1, x2, y2 = poly_bbox
            elif coco_bbox and len(coco_bbox) == 4:
                x, y, w, h = coco_bbox
                x1, y1, x2, y2 = int(x), int(y), int(x + w), int(y + h)
            else:
                return None
        else:
            # RLE или другой формат — fallback на bbox
            if coco_bbox and len(coco_bbox) == 4:
                x, y, w, h = coco_bbox
                x1, y1, x2, y2 = int(x), int(y), int(x + w), int(y + h)
            else:
                return None
    elif coco_bbox and len(coco_bbox) == 4:
        x, y, w, h = coco_bbox
        x1, y1, x2, y2 = int(x), int(y), int(x + w), int(y + h)
    else:
        return None

    # Паддинг
    x1 = max(0, x1 - NODE_CROP_PAD)
    y1 = max(0, y1 - NODE_CROP_PAD)
    x2 = min(W, x2 + NODE_CROP_PAD)
    y2 = min(H, y2 + NODE_CROP_PAD)

    # Проверка размеров
    cw, ch = x2 - x1, y2 - y1
    if cw < MIN_NODE_CROP_SIDE or ch < MIN_NODE_CROP_SIDE:
        return None
    if cw * ch < MIN_NODE_CROP_AREA:
        return None

    crop = image[y1:y2, x1:x2].copy()

    # Если есть полигон — маскируем внешнюю область белым
    if (segmentation and isinstance(segmentation, list)
            and len(segmentation) > 0 and isinstance(segmentation[0], list)):
        poly_mask = _create_polygon_mask(segmentation, (x1, y1, x2, y2))
        if poly_mask is not None:
            # Белый фон за полигоном
            if crop.ndim == 3:
                crop[poly_mask == 0] = (255, 255, 255)
            else:
                crop[poly_mask == 0] = 255

    return crop, [x1, y1, x2, y2]


def add_node_crops(
    coco_path: str,
    original_path: str,
    blocks_json_path: str,
    crops_dir: str,
) -> int:
    """
    Добавить кропы узлов из coco_validated.json в OCR pipeline.

    Args:
        coco_path: путь к detection/coco_validated.json
        original_path: путь к original/image.png
        blocks_json_path: путь к reclustered_blocks.json (будет дополнен)
        crops_dir: папка с кропами (ocr/crops/)

    Returns:
        Количество добавленных node_crop блоков.
    """
    coco_file = Path(coco_path)
    if not coco_file.exists():
        logger.warning("coco_validated.json not found: %s", coco_path)
        return 0

    # Загрузка COCO
    with open(coco_file, encoding="utf-8") as f:
        coco_data = json.load(f)

    annotations = coco_data.get("annotations", [])
    if not annotations:
        logger.info("No annotations in coco_validated.json")
        return 0

    # Размеры изображения из COCO
    images = coco_data.get("images", [])
    img_width = images[0].get("width", 1) if images else 1
    img_height = images[0].get("height", 1) if images else 1

    # Категории
    categories = {cat["id"]: cat["name"] for cat in coco_data.get("categories", [])}

    # Загрузка оригинального изображения
    image = cv2.imread(str(original_path))
    if image is None:
        logger.error("Cannot load original image: %s", original_path)
        return 0

    # Загрузка существующих блоков
    blocks_path = Path(blocks_json_path)
    with open(blocks_path, encoding="utf-8") as f:
        blocks = json.load(f)

    existing_count = len(blocks)
    crops_path = Path(crops_dir)
    crops_path.mkdir(parents=True, exist_ok=True)

    added = 0
    for ann in annotations:
        result = _crop_node(image, ann, img_width, img_height)
        if result is None:
            continue

        crop_bgr, bbox = result
        x1, y1, x2, y2 = bbox

        # Индекс нового блока
        idx = existing_count + added

        # Сохранить кроп
        fname = f"{idx:04d}_{x1}_{y1}_{x2}_{y2}.png"
        cv2.imwrite(str(crops_path / fname), crop_bgr)

        # Добавить блок
        cat_id = ann.get("category_id", 0)
        cat_name = categories.get(cat_id, "")

        blocks.append({
            "bbox": bbox,
            "text": "",
            "confidence": 1.0,
            "source": "coco_node",
            "origin": "node_crop",
            "node_class": cat_name,
        })
        added += 1

    # Сохранить обновлённый blocks
    if added > 0:
        with open(blocks_path, "w", encoding="utf-8") as f:
            json.dump(blocks, f, ensure_ascii=False, indent=2)

    logger.info(
        "Node crops: %d annotations → %d crops added (total blocks: %d → %d)",
        len(annotations), added, existing_count, existing_count + added,
    )
    return added
