"""Прослойка нормализации COCO.

COCO приходит из CVAT и служит источником истины для всех последующих этапов
пайплайна (редакторы труб/стыков, построение графа, экспорт). Разные этапы умеют
работать с полигоном ``[[x1, y1, ...]]`` и с bbox, но НЕ умеют с RLE-маской
(``segmentation`` — это словарь ``{"counts": ..., "size": [h, w]}``): на таком
элементе, например «эллипс» из CVAT, отрисовка падает
(``QPainterPath.moveTo(str, str)``).

Идея: один раз, при чтении COCO из CVAT и ПЕРЕД записью на диск, привести данные
к правилам пайплайна. Тогда ниже по потоку ничего менять не нужно.

Текущие правила (``normalize_coco_segmentation``):
  * RLE-маска -> полигон (эллипс дальше ведёт себя как обычный полигон);
  * если RLE не удалось преобразовать -> пустая сегментация ``[]``
    (этап отрисует элемент по bbox — «чтобы не падало»).

Сюда же добавляются будущие правила для новых нестандартных форматов.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


def _decode_rle_to_mask(seg: dict) -> Optional[np.ndarray]:
    """RLE (dict counts/size) -> бинарная маска (h, w) uint8. None если не вышло."""
    try:
        h, w = int(seg["size"][0]), int(seg["size"][1])
        counts = seg["counts"]

        # Uncompressed RLE: counts — список длин серий (column-major / Fortran order).
        if isinstance(counts, (list, tuple)):
            total = h * w
            flat = np.zeros(total, dtype=np.uint8)
            idx, val = 0, 0
            for run in counts:
                run = int(run)
                if run < 0:
                    return None
                end = min(idx + run, total)
                if val:
                    flat[idx:end] = 1
                idx = end
                val ^= 1
                if idx >= total:
                    break
            return flat.reshape((h, w), order="F")

        # Compressed RLE: counts — строка/байты. Нужен pycocotools.
        from pycocotools import mask as mask_utils

        if isinstance(counts, str):
            counts = counts.encode("ascii")
        mask = mask_utils.decode({"counts": counts, "size": [h, w]})
        return np.asarray(mask, dtype=np.uint8)
    except Exception:
        return None


def rle_to_polygon(seg: dict, eps_frac: float = 0.005) -> Optional[list]:
    """COCO RLE (dict) -> плоский полигон ``[x1, y1, x2, y2, ...]``.

    Возвращает None, если декодировать/аппроксимировать не удалось
    (тогда потребитель должен откатиться на bbox).
    """
    mask = _decode_rle_to_mask(seg)
    if mask is None or int(mask.sum()) == 0:
        return None
    try:
        import cv2

        cnts, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not cnts:
            return None
        contour = max(cnts, key=cv2.contourArea)
        eps = eps_frac * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
        if len(approx) < 3:
            return None
        return [float(v) for xy in approx for v in xy]
    except Exception:
        return None


def normalize_coco_segmentation(coco: Optional[dict]) -> Optional[dict]:
    """Привести сегментации COCO к правилам пайплайна (на месте) и вернуть тот же dict.

    RLE-аннотации превращаются в полигоны ``[[x, y, ...]]``. Если RLE не удалось
    сконвертировать — сегментация становится пустой ``[]`` (потребитель нарисует bbox).

    Вызывать один раз при получении COCO из CVAT, перед записью на диск.
    """
    if not isinstance(coco, dict):
        return coco
    for ann in coco.get("annotations", []):
        seg = ann.get("segmentation")
        if isinstance(seg, dict) and "counts" in seg:
            poly = rle_to_polygon(seg)
            ann["segmentation"] = [poly] if poly else []
            ann["iscrowd"] = 0
    return coco
