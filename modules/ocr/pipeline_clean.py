"""
pipeline_clean.py — ЧИСТЫЙ OCR для P&ID (доменная детекция YOLO + Surya из коробки).

Порт боевого pid_detect/pid_yolo_ocr.py в воркер, БЕЗ конфигов/профилей/reclustering:
    YOLO best.pt (тайлинг + merge_overlap)  ->  Surya 0.17.1 (расширение бокса + паддинг/
    опц.выбеление, вертикаль->вправо)  ->  cleanup  ->  фильтр мусора (junk_reason + dedup).

Выход совместим со старым контрактом UI (ocr_result.json):
    {"profile", "target": [{bbox,text,source,confidence}], "secondary": [], "stats": {...}}
Все распознанные блоки кладём в "target"; "secondary" пуст.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import cv2

from modules.ocr.text_detect_yolo import predict_tiled, merge_overlap
from modules.ocr.recognize_surya import load_surya_recognizer, recognize_boxes
from modules.ocr.text_filter import junk_reason, dedup

logger = logging.getLogger(__name__)


def _yolo_device(device: str):
    """resolve_device() -> формат ultralytics: 0 для GPU, 'cpu' для процессора."""
    return 0 if str(device).lower().startswith("cuda") else "cpu"


def _ensure_torch_device(device: str):
    """Surya читает TORCH_DEVICE; выставим, если воркер ещё не проставил."""
    if str(device).lower().startswith("cuda"):
        os.environ.setdefault("TORCH_DEVICE", "cuda")
    else:
        os.environ.setdefault("TORCH_DEVICE", "cpu")


def run_ocr_pipeline_clean(
    image_path,
    output_dir,
    model_path,
    *,
    device: str = "cpu",
    tile: int = 1280,
    overlap: int = 256,
    conf: float = 0.3,
    merge: bool = True,
    expand_frac: float = 0.15,
    pad_frac: float = 0.25,
    whiten: bool = False,
    filter_junk: bool = True,
    rec_batch=None,
) -> dict:
    """Полный чистый OCR одного листа. Возвращает dict в формате ocr_result.json."""
    from ultralytics import YOLO

    image_path = Path(image_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    model_path = Path(model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"YOLO text-detector weights not found: {model_path}")

    _ensure_torch_device(device)
    bgr = cv2.imread(str(image_path))
    if bgr is None:
        raise FileNotFoundError(f"Cannot read image: {image_path}")

    t0 = time.perf_counter()

    # -- Детекция: YOLO тайлинг + схлопывание фрагментов/вложенных --
    ymodel = YOLO(str(model_path))
    boxes = predict_tiled(ymodel, bgr, tile, overlap, conf, _yolo_device(device))
    n_raw = len(boxes)
    if merge:
        boxes = merge_overlap(boxes)
    t1 = time.perf_counter()

    # -- Распознавание: Surya (расширение/паддинг/выбеление, вертикаль->вправо) --
    rec = load_surya_recognizer()
    texts = recognize_boxes(rec, bgr, boxes, expand_frac=expand_frac,
                            pad_frac=pad_frac, whiten=whiten, batch=rec_batch)
    t2 = time.perf_counter()

    # -- Сборка + фильтр мусора --
    items = []
    n_junk = 0
    for b, (t, cf) in zip(boxes, texts):
        jr = junk_reason(t, cf) if filter_junk else None
        if jr:
            n_junk += 1
            continue
        items.append({
            "bbox": [int(v) for v in b],
            "text": t,
            "source": "yolo_surya",
            "confidence": round(float(cf), 3),
            "conf": round(float(cf), 3),   # служебный для dedup
        })

    # -- Дедупликация вложенных/дублей --
    if filter_junk:
        items, dropped = dedup(items)
    else:
        dropped = []

    for it in items:
        it.pop("conf", None)  # служебный ключ убираем из результата

    elapsed = time.perf_counter() - t0
    logger.info(
        "OCR clean: raw=%d merged=%d kept=%d junk=%d dup=%d | det %.1fs + rec %.1fs = %.1fs",
        n_raw, len(boxes), len(items), n_junk, len(dropped),
        t1 - t0, t2 - t1, elapsed,
    )

    return {
        "profile": "clean_yolo_surya",
        "target": items,
        "secondary": [],
        "stats": {
            "boxes_detected": n_raw,
            "boxes_merged": len(boxes),
            "boxes_kept": len(items),
            "boxes_junk": n_junk,
            "boxes_dup": len(dropped),
            "time_det_sec": round(t1 - t0, 1),
            "time_rec_sec": round(t2 - t1, 1),
            "time_total_sec": round(elapsed, 1),
        },
    }


def recognize_given_boxes(
    image_path,
    boxes,
    *,
    device: str = "cpu",
    expand_frac: float = 0.15,
    pad_frac: float = 0.25,
    whiten: bool = False,
    filter_junk: bool = False,
) -> list:
    """РУЧНОЙ режим (П3): распознать переданные боксы БЕЗ детекции и merge.

    boxes: [[x0,y0,x1,y1], ...]. Возврат: [{bbox,text,source,confidence,junk}].
    Те же правила, что в авто: расширение/паддинг/выбеление -> Surya -> cleanup.
    """
    _ensure_torch_device(device)
    bgr = cv2.imread(str(image_path))
    if bgr is None:
        raise FileNotFoundError(f"Cannot read image: {image_path}")
    if not boxes:
        return []

    rec = load_surya_recognizer()
    texts = recognize_boxes(rec, bgr, boxes, expand_frac=expand_frac,
                            pad_frac=pad_frac, whiten=whiten)

    out = []
    for b, (t, cf) in zip(boxes, texts):
        jr = junk_reason(t, cf) if filter_junk else None
        out.append({
            "bbox": [int(v) for v in b],
            "text": t,
            "source": "manual",
            "confidence": round(float(cf), 3),
            "junk": jr,
        })
    return out
