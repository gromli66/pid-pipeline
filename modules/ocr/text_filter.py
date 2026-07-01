"""
text_filter.py — пост-обработка распознанных боксов (домен-универсально).

Порт pid_detect/pid_filter.py:
  junk_reason(text, conf) -> причина дропа или None (мусор/символы/чужой алфавит/повтор)
  dedup(items)            -> убирает вложенные/дублирующиеся боксы

Без проектных констант: набор символов тега + геометрия (доли). conf — опционально.
Согласуется с классикой post-OCR (доля не-букв, чужой алфавит, вырожденный повтор,
опц. порог уверенности) и доменной спецификой P&ID (символы тегов ISA/KKS: Ø, №, /, -).
"""
from __future__ import annotations

import re

_CYR = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DIG = "0123456789"
ALNUM = set(_CYR + _LAT + _DIG)
ALLOWED = ALNUM | set("-/.,:№Ø∅ØøΦ()°%+ ")   # допустимые в тегах символы


def _alnum(t):
    return [c for c in t if c in ALNUM]


def junk_reason(text, conf=1.0, min_alnum_ratio=0.34, max_foreign_ratio=0.34,
                conf_short=0.0):
    """Возвращает причину, по которой бокс — мусор, или None если это валидный текст."""
    t = (text or "").strip()
    if not t:
        return "empty"
    ns = t.replace(" ", "")
    if not ns:
        return "empty"
    al = _alnum(ns)
    # 1) почти одни символы/пунктуация (но Ø25, №8 проходят — там есть цифры)
    if len(al) / len(ns) < min_alnum_ratio:
        return "mostly_symbols"
    # 2) чужой алфавит (деванагари/CJK/матзнаки от кривой вертикали)
    foreign = [c for c in ns if c not in ALLOWED]
    if len(foreign) / len(ns) > max_foreign_ratio:
        return "foreign_script"
    # 3) вырожденный повтор одного символа
    if len(al) >= 4 and len(set(al)) == 1:
        return "repeat_char"
    # 4) низкая уверенность + короткий (опционально, по умолчанию выкл)
    if conf_short > 0 and conf < conf_short and len(al) <= 2:
        return "low_conf_short"
    return None


def _iou_ioa(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    aa = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    ab = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    iou = inter / (aa + ab - inter)
    ioa = inter / min(aa, ab)        # доля меньшего внутри пересечения
    return iou, ioa


def _norm(s):
    return re.sub(r"\s+", "", (s or "").lower())


def dedup(items, iou_thr=0.5, ioa_thr=0.6):
    """items: [{bbox,text,conf,...}]. Помечает дубли. Возврат: (kept, dropped[(item,reason)])."""
    n = len(items)
    drop = [False] * n
    order = sorted(range(n), key=lambda i: -(items[i].get("conf", 0)))  # сильные первыми
    for ii in range(n):
        i = order[ii]
        if drop[i]:
            continue
        a = items[i]["bbox"]
        for jj in range(ii + 1, n):
            j = order[jj]
            if drop[j]:
                continue
            b = items[j]["bbox"]
            iou, ioa = _iou_ioa(a, b)
            ti, tj = _norm(items[i].get("text")), _norm(items[j].get("text"))
            text_dup = ti and tj and (ti == tj or ti in tj or tj in ti)
            if ioa >= ioa_thr or iou >= iou_thr or (text_dup and ioa > 0.2):
                drop[j] = True            # i сильнее -> j дубль
    kept = [items[i] for i in range(n) if not drop[i]]
    dropped = [(items[i], "duplicate") for i in range(n) if drop[i]]
    return kept, dropped
