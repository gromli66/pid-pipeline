"""
recognize_surya.py — распознавание текст-боксов через Surya 0.17.1 (из коробки).

По ТЗ:
  - РАСШИРЕНИЕ бокса реальными пикселями (возврат срезанных YOLO букв);
  - опц. ВЫБЕЛЕНИЕ фона (whiten);
  - ВЕРТИКАЛЬ -> поворот ВПРАВО (по часовой) => дальше всё горизонтально;
  - ПАДДИНГ (белое поле, quiet zone) на каждый сегмент;
  - НАРЕЗКА длинной строки: у Surya 0.17.1 фиксированный вход распознавания, длинная
    строка ужимается и не читается (возвращает пусто). Поэтому длинную ГОРИЗОНТАЛЬНУЮ
    строку режем по столбцам с минимумом «чернил» (провалы между символами — буквы не
    рвём), распознаём каждый кусок и склеиваем слева-направо.

Surya читает устройство из TORCH_DEVICE (в воркере ставится через apply_torch_device_env()).
"""
from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

from modules.ocr.text_clean import cleanup

REC_KW = dict(math_mode=False, drop_repeated_text=True)


def load_surya_recognizer():
    """RecognitionPredictor (Surya 0.17.1). Устройство — из TORCH_DEVICE окружения."""
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    return RecognitionPredictor(FoundationPredictor())


def _pil(bgr):
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def _full_poly(im):
    h, w = im.shape[:2]
    return [[0, 0], [w, 0], [w, h], [0, h]]


def _conf(line):
    return float(getattr(line, "confidence", 1.0) or 1.0)


def _whiten(crop, thr: int = 200):
    """Светлый фон/шум -> чистый белый; тёмные штрихи символов не трогаем."""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    out = crop.copy()
    out[g > thr] = (255, 255, 255)
    return out


def _crop_oriented(bgr, box, expand_frac: float, whiten: bool):
    """Кроп бокса с РЕАЛЬНЫМ расширением (+опц. выбеление); вертикаль -> поворот
    ВПРАВО. На выходе — всегда ГОРИЗОНТАЛЬНАЯ строка (или None)."""
    H, W = bgr.shape[:2]
    bx0, by0, bx1, by1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    if bx1 - bx0 < 3 or by1 - by0 < 3:
        return None
    ch = min(by1 - by0, bx1 - bx0)
    m = max(3, int(round(expand_frac * ch))) if expand_frac > 0 else 0
    x0 = max(0, bx0 - m)
    y0 = max(0, by0 - m)
    x1 = min(W, bx1 + m)
    y1 = min(H, by1 + m)
    crop = bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    if whiten:
        crop = _whiten(crop)
    if (by1 - by0) > 1.4 * (bx1 - bx0):     # вертикальный -> распрямляем вправо
        crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
    return crop


def _pad_white(crop, pad_frac: float):
    """Белое поле (quiet zone) вокруг строки — Surya обучена на тексте с полями."""
    if pad_frac <= 0:
        return crop
    ch = min(crop.shape[0], crop.shape[1])
    pad = max(8, int(round(pad_frac * ch)))
    return cv2.copyMakeBorder(crop, pad, pad, pad, pad,
                              cv2.BORDER_CONSTANT, value=(255, 255, 255))


def _split_horizontal_gaps(img, seg_target: int = 250, thr: int = 160):
    """Разрезать длинную ГОРИЗОНТАЛЬНУЮ строку по столбцам с минимумом чернил
    (между символами — буквы не рвём). Возврат: список кусков слева-направо."""
    w = img.shape[1]
    n = max(1, int(round(w / float(seg_target))))
    if n <= 1:
        return [img]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ink = (gray < thr).astype(np.int32).sum(axis=0)   # «чернила» по столбцам
    win = max(4, int(seg_target * 0.35))
    cuts = [0]
    for i in range(1, n):
        target = int(i * w / n)
        lo = max(cuts[-1] + 8, target - win)
        hi = min(w - 8, target + win)
        if hi <= lo:
            cut = min(max(target, cuts[-1] + 8), w - 1)
        else:
            cut = lo + int(np.argmin(ink[lo:hi]))   # столбец-провал
        cuts.append(cut)
    cuts.append(w)
    segs = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        if b - a >= 5:
            segs.append(img[:, a:b])
    return segs if segs else [img]


def recognize_boxes(rec, bgr, boxes, expand_frac: float = 0.15,
                    pad_frac: float = 0.25, whiten: bool = False, batch=None,
                    max_line_px: int = 350, seg_target: int = 250):
    """boxes: [[x0,y0,x1,y1], ...]. Возврат: [(text, conf)] в исходном порядке.

    Каждый бокс -> расширение/выбеление -> (вертикаль: поворот вправо) -> длинную
    строку режем по провалам чернил на куски -> паддинг -> Surya (одним батчем) ->
    склейка кусков слева-направо -> cleanup.
    """
    if not boxes:
        return []
    bs = {"recognition_batch_size": batch} if batch else {}

    # 1) собрать куски (crops) на каждый бокс
    box_crops = []                       # bi -> [crop, ...]
    for b in boxes:
        base = _crop_oriented(bgr, b, expand_frac, whiten)
        if base is None:
            box_crops.append([])
            continue
        if base.shape[1] > max_line_px:
            segs = _split_horizontal_gaps(base, seg_target)
        else:
            segs = [base]
        box_crops.append([_pad_white(s, pad_frac) for s in segs])

    # 2) один батч на все куски
    imgs, meta = [], []                  # meta: (box_idx, seg_idx)
    for bi, crops in enumerate(box_crops):
        for si, c in enumerate(crops):
            imgs.append(_pil(c))
            meta.append((bi, si))

    seg_res = {}                         # (bi, si) -> (text, conf)
    if imgs:
        np_imgs = [np.array(im) for im in imgs]
        polygons = [[_full_poly(a)] for a in np_imgs]
        res = rec(imgs, polygons=polygons, **REC_KW, **bs)
        for j, r in enumerate(res):
            ln = r.text_lines[0] if r.text_lines else None
            t, cf = (ln.text, _conf(ln)) if ln else ("", 0.0)
            seg_res[meta[j]] = (t, cf)

    # 3) склейка кусков (слева-направо = порядок чтения)
    out = []
    for bi in range(len(boxes)):
        n = len(box_crops[bi])
        parts, confs = [], []
        for si in range(n):
            t, cf = seg_res.get((bi, si), ("", 0.0))
            if t:
                parts.append(t)
            confs.append(cf)
        joined = " ".join(parts)
        conf = min(confs) if confs else 0.0
        out.append((cleanup(joined), conf))
    return out
