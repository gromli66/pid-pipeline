"""
Step 5: Объединение OCR результатов из 3 источников.

Адаптировано из: merge_ocr_results.py

Источники:
  1. surya_raw.json — Surya на полном изображении (лучший текст, боксы могут быть крупнее)
  2. surya_crops.json — Surya на кропах (точные боксы)
  3. paddle_crops.json — PaddleOCR на кропах (опционально)

Выход: ocr_final.json + viz_confidence.png
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw

from modules.ocr.utils import (
    bbox_area, iou, containment, clean_text,
    get_font, load_image_cv2,
)

logger = logging.getLogger(__name__)

# ── Настройки ─────────────────────────────────────────────
IOU_MATCH_THRESH = 0.5
CONTAINMENT_THRESH = 0.6
MAX_AREA_RATIO = 2.5


def _find_best_match(bbox: List[int], surya_orig: List[Dict]) -> Tuple[Optional[Dict], float, bool]:
    """Найти лучший match из surya_orig для эталонного бокса."""
    etalon_area = bbox_area(bbox)
    best_iou = 0.0
    best_contain = 0.0
    best = None

    for so in surya_orig:
        sb = so["bbox"]
        iou_v = iou(bbox, sb)
        contain_v = containment(bbox, sb)

        if iou_v > best_iou:
            best_iou = iou_v
            best_contain = contain_v
            best = so
        elif contain_v > best_contain and best_iou < IOU_MATCH_THRESH:
            best_contain = contain_v
            best = so

    area_ok = True
    if best and etalon_area > 0:
        if bbox_area(best["bbox"]) / etalon_area > MAX_AREA_RATIO:
            area_ok = False
        # Проверка по каждой оси отдельно:
        # surya_orig бокс не должен быть значительно шире/выше эталона
        etalon_w = bbox[2] - bbox[0]
        etalon_h = bbox[3] - bbox[1]
        orig_w = best["bbox"][2] - best["bbox"][0]
        orig_h = best["bbox"][3] - best["bbox"][1]
        if etalon_w > 0 and orig_w / etalon_w > 1.8:
            area_ok = False
        if etalon_h > 0 and orig_h / etalon_h > 1.8:
            area_ok = False

    is_good = (best_iou >= IOU_MATCH_THRESH or best_contain >= CONTAINMENT_THRESH) and area_ok
    return best, max(best_iou, best_contain), is_good


def _merge(surya_orig: List[Dict], surya_crops: List[Dict],
           paddle_crops: Optional[List[Dict]]) -> Tuple[List[Dict], Dict]:
    """Основная логика мерджа."""
    merged = []
    stats = {"surya_orig": 0, "surya_crops": 0, "paddle": 0, "empty": 0, "total": len(surya_crops)}

    for i, sc in enumerate(surya_crops):
        bbox = sc["bbox"]

        sc_text = clean_text(sc.get("text", ""))
        sc_conf = sc.get("confidence", 0) if sc_text else 0

        pc_text, pc_conf = "", 0.0
        if paddle_crops and i < len(paddle_crops):
            pc = paddle_crops[i]
            pc_text = clean_text(pc.get("text", ""))
            pc_conf = pc.get("confidence", 0) if pc_text else 0

        # Match из surya_orig
        so_match, _, is_good = _find_best_match(bbox, surya_orig)

        chosen_text = ""
        chosen_conf = 0.0
        chosen_source = "empty"

        if is_good and so_match:
            so_text = clean_text(so_match.get("text", ""))
            so_conf = so_match.get("confidence", 0) if so_text else 0
            if so_text:
                chosen_text = so_text
                chosen_conf = so_conf
                chosen_source = "surya_orig"

        if not chosen_text:
            if sc_text and pc_text:
                if sc_conf >= pc_conf:
                    chosen_text, chosen_conf, chosen_source = sc_text, sc_conf, "surya_crops"
                else:
                    chosen_text, chosen_conf, chosen_source = pc_text, pc_conf, "paddle"
            elif sc_text:
                chosen_text, chosen_conf, chosen_source = sc_text, sc_conf, "surya_crops"
            elif pc_text:
                chosen_text, chosen_conf, chosen_source = pc_text, pc_conf, "paddle"

        stats[chosen_source] = stats.get(chosen_source, 0) + 1

        merged.append({
            "bbox": bbox,
            "text": chosen_text,
            "confidence": round(chosen_conf, 4),
            "source": chosen_source,
            "origin": sc.get("origin", "unknown"),
        })

    return merged, stats


def _create_confidence_viz(image_path: str, merged: List[Dict], output_path: str):
    """Визуализация по confidence: зелёный >=94%, оранжевый 80-94%."""
    try:
        img = load_image_cv2(image_path)
    except FileNotFoundError:
        logger.warning("Cannot load image for visualization: %s", image_path)
        return

    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    font = get_font(13)

    for block in merged:
        text = block.get("text", "")
        conf = block.get("confidence", 0)
        if not text or conf < 0.80:
            continue

        bbox = [int(c) for c in block["bbox"]]
        x1, y1, x2, y2 = bbox
        color = (0, 210, 0) if conf >= 0.94 else (255, 160, 0)

        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
        label = f"{text[:30]} {conf:.0%}"
        label_y = max(0, y1 - 16)
        text_bbox = draw.textbbox((x1, label_y), label, font=font)
        draw.rectangle(text_bbox, fill=(0, 0, 0, 200))
        draw.text((x1, label_y), label, fill=color, font=font)

    img_pil.save(str(output_path))


def run_merge(ocr_dir: str, has_paddle: bool = True, has_surya_crops: bool = True):
    """
    Объединение OCR результатов.

    Args:
        ocr_dir: папка OCR (содержит surya_raw.json, surya_crops.json, paddle_crops.json)
        has_paddle: True если paddle_crops.json доступен
        has_surya_crops: True если surya_crops.json доступен
    """
    ocr_path = Path(ocr_dir)

    # Загрузка
    with open(ocr_path / "surya_raw.json", encoding="utf-8") as f:
        surya_orig = json.load(f)

    surya_crops = None
    if has_surya_crops:
        surya_crops_path = ocr_path / "surya_crops.json"
        if surya_crops_path.exists():
            with open(surya_crops_path, encoding="utf-8") as f:
                surya_crops = json.load(f)

    paddle_crops = None
    if has_paddle:
        paddle_path = ocr_path / "paddle_crops.json"
        if paddle_path.exists():
            with open(paddle_path, encoding="utf-8") as f:
                paddle_crops = json.load(f)

    logger.info("Merging: surya_orig=%d, surya_crops=%s, paddle=%s",
                len(surya_orig),
                len(surya_crops) if surya_crops else "N/A",
                len(paddle_crops) if paddle_crops else "N/A")

    # Если surya_crops отсутствует, но есть paddle — используем paddle как основу
    if surya_crops is None and paddle_crops is not None:
        # paddle_crops становится эталоном вместо surya_crops
        surya_crops = paddle_crops
        paddle_crops = None
        logger.info("Surya crops skipped — using paddle as primary crop source")
    elif surya_crops is None and paddle_crops is None:
        # Только surya_orig — конвертируем в итоговый формат
        logger.warning("No crop results available — using surya_orig only")
        merged = []
        for block in surya_orig:
            merged.append({
                "bbox": block["bbox"],
                "text": clean_text(block.get("text", "")),
                "confidence": round(block.get("confidence", 0), 4),
                "source": "surya_orig",
                "origin": "original",
            })
        with open(ocr_path / "ocr_final.json", "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        logger.info("Saved (surya_orig only): %d blocks", len(merged))
        return

    # Мердж
    merged, stats = _merge(surya_orig, surya_crops, paddle_crops)

    with_text = sum(1 for m in merged if m["text"])
    logger.info("Merged: %d total, %d with text", len(merged), with_text)
    logger.info("Sources: orig=%d, surya_crops=%d, paddle=%d, empty=%d",
                stats["surya_orig"], stats["surya_crops"], stats["paddle"], stats["empty"])

    # Сохранение
    out_path = ocr_path / "ocr_final.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    logger.info("Saved: %s", out_path)

    # Визуализация (если original доступен)
    # Ищем original image для визуализации
    for candidate in [
        ocr_path / "cleaned_for_ocr.png",
        ocr_path.parent / "original" / "image.png",
    ]:
        if candidate.exists():
            viz_path = ocr_path / "viz_confidence.png"
            _create_confidence_viz(str(candidate), merged, str(viz_path))
            logger.info("Visualization: %s", viz_path)
            break
