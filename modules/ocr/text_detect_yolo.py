"""
text_detect_yolo.py — доменная детекция текста YOLO (тайлинг + NMS + merge).

Порт боевого детектора из pid_detect (eval_text_yolo.py):
  - predict_tiled: инференс обученного YOLO по тайлам, боксы в координатах листа + NMS;
  - merge_overlap: схлопывание СИЛЬНО пересекающихся боксов (фрагменты одного тега /
    вложенные) в union — соседние теги не трогает.

Домен-универсально: пороги в долях / IoU, без проектных констант.
"""
from __future__ import annotations

import numpy as np


def nms(boxes, scores, iou_thr: float = 0.4):
    """Non-max suppression поxyxy-боксам."""
    if not boxes:
        return []
    b = np.array(boxes, float)
    s = np.array(scores)
    idx = s.argsort()[::-1]
    keep = []
    while len(idx):
        i = idx[0]
        keep.append(i)
        if len(idx) == 1:
            break
        x0 = np.maximum(b[i, 0], b[idx[1:], 0])
        y0 = np.maximum(b[i, 1], b[idx[1:], 1])
        x1 = np.minimum(b[i, 2], b[idx[1:], 2])
        y1 = np.minimum(b[i, 3], b[idx[1:], 3])
        inter = np.maximum(0, x1 - x0) * np.maximum(0, y1 - y0)
        ar = (b[idx[1:], 2] - b[idx[1:], 0]) * (b[idx[1:], 3] - b[idx[1:], 1])
        ai = (b[i, 2] - b[i, 0]) * (b[i, 3] - b[i, 1])
        iou = inter / (ai + ar - inter + 1e-6)
        idx = idx[1:][iou < iou_thr]
    return [boxes[i] for i in keep]


def iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x1 - x0) * max(0, y1 - y0)
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i + 1e-6)


def merge_overlap(boxes, iou_thr: float = 0.25, ios_thr: float = 0.6):
    """Схлопывает СИЛЬНО пересекающиеся боксы (фрагменты одного тега / вложенные) в union.
       Соседние теги не трогает — у них нет такого перекрытия."""
    n = len(boxes)
    if n == 0:
        return []
    par = list(range(n))

    def f(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    for i in range(n):
        a = boxes[i]
        aa = (a[2] - a[0]) * (a[3] - a[1])
        for j in range(i + 1, n):
            b = boxes[j]
            ab = (b[2] - b[0]) * (b[3] - b[1])
            x0, y0 = max(a[0], b[0]), max(a[1], b[1])
            x1, y1 = min(a[2], b[2]), min(a[3], b[3])
            inter = max(0, x1 - x0) * max(0, y1 - y0)
            if inter <= 0:
                continue
            uni = aa + ab - inter
            ios = inter / max(1.0, min(aa, ab))       # доля меньшего внутри
            if inter / max(1.0, uni) >= iou_thr or ios >= ios_thr:
                par[f(i)] = f(j)
    g = {}
    for i in range(n):
        g.setdefault(f(i), []).append(i)
    out = []
    for idx in g.values():
        out.append([min(boxes[i][0] for i in idx), min(boxes[i][1] for i in idx),
                    max(boxes[i][2] for i in idx), max(boxes[i][3] for i in idx)])
    return out


def predict_tiled(model, bgr, tile: int, overlap: int, conf: float, device="0"):
    """Тайловый инференс YOLO-детектора текста -> боксы [x0,y0,x1,y1] в координатах листа (NMS).

    model  — ultralytics YOLO
    bgr    — изображение (numpy BGR)
    device — "0"/0 = GPU, "cpu" = процессор.
    """
    H, W = bgr.shape[:2]
    step = tile - overlap
    boxes, scores = [], []
    for y in range(0, max(1, H - overlap), step):
        for x in range(0, max(1, W - overlap), step):
            x1, y1 = min(x + tile, W), min(y + tile, H)
            patch = bgr[y:y1, x:x1]
            if patch.shape[0] < 32 or patch.shape[1] < 32:
                continue
            r = model.predict(patch, imgsz=tile, conf=conf, max_det=1000,
                              verbose=False, device=device)[0]
            for bb, sc in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()):
                boxes.append([bb[0] + x, bb[1] + y, bb[2] + x, bb[3] + y])
                scores.append(float(sc))
    return nms(boxes, scores)
