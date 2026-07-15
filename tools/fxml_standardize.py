#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fxml_standardize.py — привести ЛЮБОЙ выходной FXML-лист к стандарту 1920x1080
и убрать смещение/разрывы кастом-контрол скинов в Scene Builder.

Пайплайн (всё считается уже в целевых координатах 1920x1080):

  1. Читает FXML (генератор graph_to_fxml или каталог скинов — не важно).
  2. Считает общий bbox содержимого и РАВНОМЕРНО (letterbox) вписывает его в
     1920x1080 с центрированием. Пересчитывает ВСЕ координаты/размеры: layoutX/Y,
     prefW/H, width/height, Line, Polyline, Polygon points, Rectangle, Circle/Arc,
     strokeWidth, шрифты (Font size и -fx-font-size), Rotate pivot.
  3. КЛАПАНЫ: измеренную «талию» скина (waist из skin_geometry.json) сажает на
     ось подключённой трубы, затем ДОТЯГИВАЕТ концы труб до реальной графики
     скина через паддинг (ортогонально, вдоль сегмента) — скин не трогаем, линию
     продолжаем сквозь letterbox-поле бокса.
  4. ДАТЧИКИ (DetectorControl): у скина жёсткий минимум (~20px, текст), он не
     сжимается. Держим датчик читаемым через scaleX/scaleY = s·K (пивот=центр) и
     прижимаем РЕНДЕР-РЕБРО к концу подводящей трубы (тело уходит в сторону, не
     наезжая на ребро).
  5. РАЗРЫВЫ МОСТОВ (id `*_b0/_b1`): раздвигает половинки до видимого зазора
     (min 6px / 2.5·strokeWidth), иначе на сжатом листе разрыв не виден.
  6. Ставит корневому AnchorPane prefWidth=1920 prefHeight=1080.

Замечание про корень проблемы смещения полигонов (потерянные контакты у
symbol-полигонов): он в ГЕНЕРАТОРЕ — труба приходила к bbox детекции, а полигон
рисуется по контуру SAM2. Это чинится в graph_to_fxml.py (проекция конца трубы
на контур, project_endpoint_to_contour), НЕ здесь.

Режимы (--mode):
  letterbox  — РЕКОМЕНДУЕТСЯ. Размер 1920x1080 + пассы 3–5 выше. Раскладка и
               привязки линий сохраняются; форму боксов клапанов не меняем.
  aspect     — ЭКСПЕРИМЕНТ. Дополнительно подгоняет форму боксов контролов под
               аспект скина по оси реально подходящих линий. На тройниках/крестах
               часть привязок может отойти — такие случаи чинятся в генераторе.
  full       — как aspect + поднять талию на трубу (contact-offset) для сырых
               боксов без поправки.

Зависимости: lxml.  Комментарии и <?import?> сохраняются.

Примеры:
    python3 tools/fxml_standardize.py out.fxml -o out_1920.fxml
    python3 tools/fxml_standardize.py out.fxml -o out_1920.fxml --mode aspect
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path
from lxml import etree

TARGET_W = 1920.0
TARGET_H = 1080.0

# Мосты: зазор разрыва = MULT * base * (1 + ALPHA*log2(sw_max/sw)), где base растёт
# для тонких линий (лог-шкала), с клампом по длине сегмента — половинки не исчезают.
# BRIDGE_GAP_MULT — «ручка» (стандарт x2); её потом можно тянуть из UI.
BRIDGE_GAP_MULT = 2.0
BRIDGE_THIN_ALPHA = 0.5
BRIDGE_GAP_MIN = 6.0

X_LAYOUT = {"layoutX", "translateX"}
Y_LAYOUT = {"layoutY", "translateY"}
X_COORD = {"startX", "endX", "centerX"}
Y_COORD = {"startY", "endY", "centerY"}
SIZE_ATTRS = {
    "prefWidth", "prefHeight", "minWidth", "minHeight", "maxWidth", "maxHeight",
    "width", "height", "radius", "radiusX", "radiusY", "fitWidth", "fitHeight",
    "strokeWidth", "wrappingWidth", "arcWidth", "arcHeight", "kksFontSize",
}
FONT_RE = re.compile(r"(-fx-font-size\s*:\s*)([0-9]*\.?[0-9]+)(px)?", re.I)


def lname(el):
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def iter_elems(root):
    yield root
    for el in root.iter():
        if el is not root:
            yield el


def element_extent(el):
    a = el.attrib
    name = lname(el)
    lx, ly = fnum(a.get("layoutX")), fnum(a.get("layoutY"))
    has_layout = ("layoutX" in a) or ("layoutY" in a)
    bx = lx or 0.0
    by = ly or 0.0
    if "points" in a:
        nums = [fnum(x) for x in re.split(r"[,\s]+", a["points"].strip()) if x != ""]
        nums = [n for n in nums if n is not None]
        xs = nums[0::2]; ys = nums[1::2]
        if xs and ys:
            if has_layout:
                return (bx + min(xs), by + min(ys), bx + max(xs), by + max(ys))
            return (min(xs), min(ys), max(xs), max(ys))
    if name == "Line":
        sx, sy = fnum(a.get("startX", 0)), fnum(a.get("startY", 0))
        ex, ey = fnum(a.get("endX", 0)), fnum(a.get("endY", 0))
        ox, oy = (bx, by) if has_layout else (0.0, 0.0)
        return (min(ox+sx, ox+ex), min(oy+sy, oy+ey), max(ox+sx, ox+ex), max(oy+sy, oy+ey))
    w = fnum(a.get("width")) or fnum(a.get("prefWidth"))
    h = fnum(a.get("height")) or fnum(a.get("prefHeight"))
    if has_layout and (w is not None or h is not None):
        w = w or 0.0; h = h or 0.0
        return (bx, by, bx + w, by + h)
    if name in ("Circle", "Ellipse", "Arc"):
        cx, cy = fnum(a.get("centerX", 0)) or 0.0, fnum(a.get("centerY", 0)) or 0.0
        r = fnum(a.get("radius"))
        rx = fnum(a.get("radiusX")) or r or 0.0
        ry = fnum(a.get("radiusY")) or r or 0.0
        ox, oy = (bx, by) if has_layout else (0.0, 0.0)
        return (ox + cx - rx, oy + cy - ry, ox + cx + rx, oy + cy + ry)
    if name == "Text" and has_layout:
        fs = 12.0
        m = FONT_RE.search(a.get("style", ""))
        if m:
            fs = fnum(m.group(2)) or 12.0
        txt = a.get("text", "") or ""
        est = max(1, len(txt)) * fs * 0.6
        return (bx, by - fs, bx + est, by + fs * 0.3)
    return (bx, by, bx, by) if has_layout else None


def content_bbox(root):
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for el in iter_elems(root):
        if el is root:
            continue
        ext = element_extent(el)
        if not ext:
            continue
        x0, y0, x1, y1 = ext
        minx, miny = min(minx, x0), min(miny, y0)
        maxx, maxy = max(maxx, x1), max(maxy, y1)
    return None if minx == float("inf") else (minx, miny, maxx, maxy)


def apply_transform(root, s, offx, offy, scale_fonts=True):
    def tx(v):
        return v * s + offx
    def ty(v):
        return v * s + offy
    for el in iter_elems(root):
        if el is root:
            continue
        a = el.attrib
        name = lname(el)
        has_layout = ("layoutX" in a) or ("layoutY" in a)
        for attr in list(a.keys()):
            v = fnum(a[attr])
            if v is None:
                if attr == "style" and scale_fonts:
                    a[attr] = FONT_RE.sub(
                        lambda m: f"{m.group(1)}{(fnum(m.group(2)) or 0)*s:.2f}{m.group(3) or ''}",
                        a[attr])
                continue
            if attr in X_LAYOUT:
                a[attr] = f"{tx(v):.2f}"
            elif attr in Y_LAYOUT:
                a[attr] = f"{ty(v):.2f}"
            elif attr in X_COORD:
                a[attr] = f"{(v*s if has_layout else tx(v)):.2f}"
            elif attr in Y_COORD:
                a[attr] = f"{(v*s if has_layout else ty(v)):.2f}"
            elif attr == "size" and name == "Font":
                a[attr] = f"{v*s:.2f}" if scale_fonts else a[attr]
            elif attr in ("pivotX", "pivotY"):
                a[attr] = f"{v*s:.2f}"
            elif attr in SIZE_ATTRS:
                a[attr] = f"{v*s:.2f}"
        if "points" in a:
            nums = [fnum(x) for x in re.split(r"[,\s]+", a["points"].strip()) if x != ""]
            nums = [n for n in nums if n is not None]
            out = []
            for i in range(0, len(nums) - 1, 2):
                x, y = nums[i], nums[i+1]
                out += ([x*s, y*s] if has_layout else [tx(x), ty(y)])
            a["points"] = ", ".join(f"{c:.2f}" for c in out)


def load_geo(path):
    if not path:
        return None
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def collect_endpoints(root):
    """Все концы линий/полилиний (в исходных координатах, до преобразования)."""
    eps = []
    for e in root.iter():
        n = lname(e); a = e.attrib
        if n == "Line":
            eps.append((fnum(a.get("startX")), fnum(a.get("startY"))))
            eps.append((fnum(a.get("endX")), fnum(a.get("endY"))))
        elif n == "Polyline":
            nums = [fnum(x) for x in re.split(r"[,\s]+", a.get("points", "").strip()) if x != ""]
            nums = [x for x in nums if x is not None]
            for i in range(0, len(nums) - 1, 2):
                eps.append((nums[i], nums[i+1]))
    return [(x, y) for (x, y) in eps if x is not None and y is not None]


def fix_skins(root, geo, apply_contact=False):
    """Подогнать prefW:prefH контролов под аспект скина, СОХРАНЯЯ ось трубы.

    Ось (горизонт/вертикаль) определяется по реально подходящим к боксу концам
    линий; размер и положение вдоль оси не трогаем, меняем только поперечный
    размер и центрируем его на осевой линии трубы. Так графика скина перестаёт
    letterbox-иться, а привязки по оси сохраняются.
    """
    skins = geo["skins"]
    default = geo.get("defaults", {"aspect_hw": 1.0, "contact": [0.0, 0.0]})
    eps = collect_endpoints(root)
    n = 0
    for el in iter_elems(root):
        a = el.attrib
        st = a.get("skinType")
        if not st:
            continue
        g = skins.get(st, default)
        aspect_hw = float(g.get("aspect_hw", 1.0))
        h_frac, v_frac = g.get("contact", [0.0, 0.0])
        orient = a.get("orientation", "HORIZONTAL")
        pw, ph = fnum(a.get("prefWidth")), fnum(a.get("prefHeight"))
        lx, ly = fnum(a.get("layoutX")) or 0.0, fnum(a.get("layoutY")) or 0.0
        if pw is None or ph is None:
            continue
        cx, cy = lx + pw/2.0, ly + ph/2.0
        margin = max(6.0, 0.35 * min(pw, ph))
        left = right = top = bottom = 0
        ys = []; xs = []
        for (px, py) in eps:
            if lx-margin <= px <= lx+pw+margin and ly-margin <= py <= ly+ph+margin:
                dl, dr, dt, db = abs(px-lx), abs(px-(lx+pw)), abs(py-ly), abs(py-(ly+ph))
                m = min(dl, dr, dt, db)
                if m == dl: left += 1; ys.append(py)
                elif m == dr: right += 1; ys.append(py)
                elif m == dt: top += 1; xs.append(px)
                else: bottom += 1; xs.append(px)
        hc, vc = left + right, top + bottom
        if hc > vc:
            axis = "H"
        elif vc > hc:
            axis = "V"
        else:
            axis = "V" if orient.startswith("VERTICAL") else "H"
        if axis == "H":
            perp = (sum(ys)/len(ys)) if ys else cy
            nw = pw; nh = pw * aspect_hw; nlx = lx; nly = perp - nh/2.0
            if apply_contact:
                nly -= h_frac * nh
        else:
            perp = (sum(xs)/len(xs)) if xs else cx
            nh = ph; nw = ph * aspect_hw; nly = ly; nlx = perp - nw/2.0
            if apply_contact:
                # VERTICAL_REVERSE зеркалит талию: знак поперечной поправки инвертируется.
                nlx += (-v_frac if orient == "VERTICAL_REVERSE" else v_frac) * nw
        a["prefWidth"] = f"{nw:.2f}"
        a["prefHeight"] = f"{nh:.2f}"
        a["layoutX"] = f"{max(0.0, nlx):.2f}"
        a["layoutY"] = f"{max(0.0, nly):.2f}"
        n += 1
    return n


def _standardize_tree(tree, root, geo=None, mode="letterbox", margin=0.0,
                      pad_top=0.0, pad_bottom=0.0, pad_left=0.0, pad_right=0.0,
                      bridge_gap_mult=BRIDGE_GAP_MULT, bridge_thin_alpha=BRIDGE_THIN_ALPHA):
    """Ядро пайплайна: применяет все пассы к уже разобранному дереву (координаты
    приводятся к 1920x1080). Общее для файлового и in-memory входа.

    pad_top/bottom/left/right — асимметричные поля (px) под подписи: холст ОСТАЁТСЯ
    1920x1080, контент пропорционально вписывается и центрируется в прямоугольнике
    между полями (масштаб единый, без искажений). Если поле = 0, берётся `margin`.
    bridge_gap_mult — множитель зазора мостов («ручка»)."""
    nfix = fix_skins(root, geo, apply_contact=(mode == "full")) if (mode in ("aspect", "full") and geo) else 0

    bbox = content_bbox(root)
    if not bbox:
        print("!! не нашёл содержимого с координатами", file=sys.stderr)
        return None
    minx, miny, maxx, maxy = bbox
    cw, ch = max(1e-6, maxx - minx), max(1e-6, maxy - miny)
    pt = pad_top or margin
    pb = pad_bottom or margin
    pl = pad_left or margin
    pr = pad_right or margin
    avail_w, avail_h = TARGET_W - pl - pr, TARGET_H - pt - pb
    s = min(avail_w / cw, avail_h / ch)
    offx = pl + (avail_w - cw * s) / 2.0 - minx * s
    offy = pt + (avail_h - ch * s) / 2.0 - miny * s

    apply_transform(root, s, offx, offy)

    # ===== helpers для пост-пассов (координаты уже 1920x1080) =====
    def _content_rect(st, orient, lx, ly, pw, ph):
        """Реальный «след» графики скина в боксе (letterbox по aspect_hw)."""
        g = (geo or {}).get("skins", {}).get(st, (geo or {}).get("defaults", {"aspect_hw": 1.0}))
        ar = float(g.get("aspect_hw", 1.0))
        r = (1.0 / ar) if orient.startswith("VERTICAL") else ar
        if pw * r <= ph:
            sw, sh = pw, pw * r
        else:
            sh, sw = ph, ph / r
        return lx + (pw - sw) / 2.0, ly + (ph - sh) / 2.0, sw, sh

    def _pipe_ends():
        """[el, kind, x, y, adj_x, adj_y] по концам всех Line/Polyline."""
        eps = []
        for e in root.iter():
            n = lname(e); a = e.attrib
            if n == "Line":
                sx, sy = fnum(a.get("startX")), fnum(a.get("startY"))
                ex, ey = fnum(a.get("endX")), fnum(a.get("endY"))
                if None not in (sx, sy, ex, ey):
                    eps.append([e, "start", sx, sy, ex, ey])
                    eps.append([e, "end", ex, ey, sx, sy])
            elif n == "Polyline":
                nums = [fnum(x) for x in re.split(r"[,\s]+", a.get("points", "").strip()) if x != ""]
                nums = [x for x in nums if x is not None]
                if len(nums) >= 4:
                    eps.append([e, "pfirst", nums[0], nums[1], nums[2], nums[3]])
                    eps.append([e, "plast", nums[-2], nums[-1], nums[-4], nums[-3]])
        return eps

    def _set_end(e, kind, x, y):
        a = e.attrib
        if kind == "start":
            a["startX"] = f"{x:.2f}"; a["startY"] = f"{y:.2f}"
        elif kind == "end":
            a["endX"] = f"{x:.2f}"; a["endY"] = f"{y:.2f}"
        else:
            nums = [fnum(v) for v in re.split(r"[,\s]+", a.get("points", "").strip()) if v != ""]
            nums = [v for v in nums if v is not None]
            if kind == "pfirst":
                nums[0], nums[1] = x, y
            else:
                nums[-2], nums[-1] = x, y
            a["points"] = ", ".join(f"{c:.2f}" for c in nums)

    # ===== (1) КЛАПАНЫ: талия -> на трубу, затем дотянуть линии до графики скина =====
    n_ext = 0
    if geo:
        SKIN_CTRL = {"ValveControl", "PumpControl", "HeaterControl", "FunctionControl", "ButtonControl"}
        ends = _pipe_ends()
        # (a) измеренная талия скина -> на ось подключённой трубы (клапан садится на трубу)
        for el in iter_elems(root):
            if lname(el) not in SKIN_CTRL:
                continue
            a = el.attrib; st = a.get("skinType")
            g = geo["skins"].get(st) if st else None
            if not g or "waist" not in g:
                continue
            lx, ly = fnum(a.get("layoutX")), fnum(a.get("layoutY"))
            pw, ph = fnum(a.get("prefWidth")), fnum(a.get("prefHeight"))
            if None in (lx, ly, pw, ph):
                continue
            ins = [(px, py) for (e, kind, px, py, ax, ay) in ends
                   if lx - 2 <= px <= lx + pw + 2 and ly - 2 <= py <= ly + ph + 2]
            if not ins:
                continue
            orient = a.get("orientation", "HORIZONTAL")
            if orient.startswith("VERTICAL"):
                wu = g["waist"][0]
                if orient.endswith("REVERSE"):
                    wu = 1.0 - wu   # разворот на 180° зеркалит талию по горизонтали
                xs = sorted(p[0] for p in ins); pipe_x = xs[len(xs) // 2]
                a["layoutX"] = f"{max(0.0, lx + (pipe_x - (lx + wu * pw))):.2f}"
            else:
                wv = g["waist"][1]
                ys = sorted(p[1] for p in ins); pipe_y = ys[len(ys) // 2]
                a["layoutY"] = f"{max(0.0, ly + (pipe_y - (ly + wv * ph))):.2f}"
        # (b) дотянуть концы труб до реальной графики скина (ортогонально, вдоль сегмента)
        for el in iter_elems(root):
            if lname(el) not in SKIN_CTRL:
                continue
            a = el.attrib; st = a.get("skinType")
            if not st:
                continue
            lx, ly = fnum(a.get("layoutX")), fnum(a.get("layoutY"))
            pw, ph = fnum(a.get("prefWidth")), fnum(a.get("prefHeight"))
            if None in (lx, ly, pw, ph):
                continue
            sx, sy, sw, sh = _content_rect(st, a.get("orientation", "HORIZONTAL"), lx, ly, pw, ph)
            if abs(sw - pw) < 0.5 and abs(sh - ph) < 0.5:
                continue
            TOL = 2.0
            for ep in ends:
                e, kind, px, py, ax, ay = ep
                if px is None:
                    continue
                if not (lx - TOL <= px <= lx + pw + TOL and ly - TOL <= py <= ly + ph + TOL):
                    continue
                if abs(py - ay) >= abs(px - ax):        # вертикальный сегмент -> тянем по Y
                    if py < sy - 0.5:
                        _set_end(e, kind, px, sy); ep[3] = sy; n_ext += 1
                    elif py > sy + sh + 0.5:
                        _set_end(e, kind, px, sy + sh); ep[3] = sy + sh; n_ext += 1
                else:                                    # горизонтальный сегмент -> тянем по X
                    if px < sx - 0.5:
                        _set_end(e, kind, sx, py); ep[2] = sx; n_ext += 1
                    elif px > sx + sw + 0.5:
                        _set_end(e, kind, sx + sw, py); ep[2] = sx + sw; n_ext += 1

    # ===== (2) ДАТЧИКИ: держим читаемыми через scaleX/scaleY=s·K (пивот=центр),
    #     рендер-ребро прижимаем к концу подводящей трубы (тело уходит в сторону) =====
    DET_MIN_W, DET_ASPECT, DET_SCALE_K = 20.0, 106.0 / 156.0, 1.5   # K — крупность датчика
    epadj = []
    for e in root.iter():
        n = lname(e); a = e.attrib
        if n == "Line":
            sx, sy = fnum(a.get("startX")), fnum(a.get("startY"))
            ex, ey = fnum(a.get("endX")), fnum(a.get("endY"))
            if None not in (sx, sy, ex, ey):
                epadj.append((sx, sy, ex, ey)); epadj.append((ex, ey, sx, sy))
        elif n == "Polyline":
            nums = [fnum(x) for x in re.split(r"[,\s]+", a.get("points", "").strip()) if x != ""]
            nums = [x for x in nums if x is not None]
            if len(nums) >= 4:
                epadj.append((nums[0], nums[1], nums[2], nums[3]))
                epadj.append((nums[-2], nums[-1], nums[-4], nums[-3]))
    for el in iter_elems(root):
        if lname(el) != "DetectorControl":
            continue
        a = el.attrib
        npw, nph = fnum(a.get("prefWidth")), fnum(a.get("prefHeight"))
        nlx, nly = fnum(a.get("layoutX")) or 0.0, fnum(a.get("layoutY")) or 0.0
        if npw is None or nph is None:
            continue
        cx, cy = nlx + npw / 2.0, nly + nph / 2.0
        sc = s if s > 1e-6 else 1.0
        ow = max(npw / sc, DET_MIN_W); oh = max(nph / sc, DET_MIN_W * DET_ASPECT)
        rw = ow * sc * DET_SCALE_K; rh = oh * sc * DET_SCALE_K     # реальный рендер-размер
        best, bd = None, 1e18
        for (px, py, ax, ay) in epadj:
            dd = (px - cx) ** 2 + (py - cy) ** 2
            if dd < bd:
                bd, best = dd, (px, py, ax, ay)
        bcx, bcy = cx, cy
        if best is not None and bd ** 0.5 < 8 * max(npw, nph):
            px, py, ax, ay = best
            dx, dy = px - ax, py - ay                              # труба -> к датчику
            if abs(dx) >= abs(dy):
                ux, uy, ext = (1.0 if dx >= 0 else -1.0), 0.0, rw
            else:
                ux, uy, ext = 0.0, (1.0 if dy >= 0 else -1.0), rh
            bcx = px + ux * ext / 2.0; bcy = py + uy * ext / 2.0   # рендер-ребро со стороны трубы -> на P
        a["prefWidth"] = f"{ow:.2f}"; a["prefHeight"] = f"{oh:.2f}"
        a["layoutX"] = f"{max(0.0, bcx - ow / 2.0):.2f}"; a["layoutY"] = f"{max(0.0, bcy - oh / 2.0):.2f}"
        a["scaleX"] = f"{sc * DET_SCALE_K:.4f}"; a["scaleY"] = f"{sc * DET_SCALE_K:.4f}"
        kf = fnum(a.get("kksFontSize"))
        if kf is not None:
            a["kksFontSize"] = f"{kf / sc:.2f}"

    # ===== (3) РАЗРЫВЫ МОСТОВ: раздвинуть половинки до видимого зазора =====
    def _seg_ends(e):
        a = e.attrib; n = lname(e)
        if n == "Line":
            return [("start", fnum(a.get("startX")), fnum(a.get("startY"))),
                    ("end", fnum(a.get("endX")), fnum(a.get("endY")))]
        nums = [fnum(x) for x in re.split(r"[,\s]+", a.get("points", "").strip()) if x != ""]
        nums = [x for x in nums if x is not None]
        return [("pfirst", nums[0], nums[1]), ("plast", nums[-2], nums[-1])] if len(nums) >= 4 else []

    bseg = {}
    for el in iter_elems(root):
        if lname(el) not in ("Line", "Polyline"):
            continue
        m = re.match(r"(.+)_b(\d+)$", el.attrib.get("{http://javafx.com/fxml/1}id", "") or "")
        if m:
            bseg.setdefault(m.group(1), []).append((int(m.group(2)), el))
    # опорная толщина для лог-шкалы зазора: макс. strokeWidth среди мостовых сегментов
    _sws = [fnum(e.attrib.get("strokeWidth")) for _segs in bseg.values() for _i, e in _segs]
    _sws = [v for v in _sws if v]
    sw_max = max(_sws) if _sws else 1.0
    for base, segs in bseg.items():
        if len(segs) < 2:
            continue
        segs.sort()
        for (i0, e0), (i1, e1) in zip(segs, segs[1:]):
            E0, E1 = _seg_ends(e0), _seg_ends(e1)
            if not E0 or not E1:
                continue
            best = None
            for k0, x0, y0 in E0:
                for k1, x1, y1 in E1:
                    if None in (x0, y0, x1, y1):
                        continue
                    d = (x0 - x1) ** 2 + (y0 - y1) ** 2
                    if best is None or d < best[0]:
                        best = (d, (k0, x0, y0), (k1, x1, y1))
            if not best:
                continue
            d, (k0, x0, y0), (k1, x1, y1) = best
            gap = d ** 0.5
            sw = fnum(e0.attrib.get("strokeWidth")) or 1.0
            # база + лог-шкала (тоньше линия -> больше зазор) + множитель-«ручка»
            base_gap = max(BRIDGE_GAP_MIN, 2.5 * sw)
            thin = math.log2(sw_max / sw) if (sw_max > sw > 0) else 0.0
            target = bridge_gap_mult * base_gap * (1.0 + bridge_thin_alpha * thin)
            cxg, cyg = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            # защита от исчезновения: каждая половинка остаётся не короче keep
            outer0 = next(((xx, yy) for kk, xx, yy in E0 if kk != k0 and xx is not None), None)
            outer1 = next(((xx, yy) for kk, xx, yy in E1 if kk != k1 and xx is not None), None)
            keep = max(2.0, sw)
            if outer0 and outer1:
                d0 = ((outer0[0] - cxg) ** 2 + (outer0[1] - cyg) ** 2) ** 0.5
                d1 = ((outer1[0] - cxg) ** 2 + (outer1[1] - cyg) ** 2) ** 0.5
                half_max = min(d0, d1) - keep
                if half_max > 0:
                    target = min(target, 2.0 * half_max)
            if gap >= target:
                continue
            dx, dy = x1 - x0, y1 - y0
            L = (dx * dx + dy * dy) ** 0.5
            if L < 1e-6:
                continue
            ux, uy = dx / L, dy / L; half = target / 2.0
            _set_end(e0, k0, cxg - ux * half, cyg - uy * half)
            _set_end(e1, k1, cxg + ux * half, cyg + uy * half)

    root.attrib["prefWidth"] = f"{TARGET_W:.1f}"
    root.attrib["prefHeight"] = f"{TARGET_H:.1f}"
    for k in ("minWidth", "maxWidth"):
        if k in root.attrib:
            root.attrib[k] = f"{TARGET_W:.1f}"
    for k in ("minHeight", "maxHeight"):
        if k in root.attrib:
            root.attrib[k] = f"{TARGET_H:.1f}"

    return dict(scale=s, offx=offx, offy=offy, bbox=bbox, nfix=nfix, ext=n_ext,
                minx=minx, miny=miny, maxx=maxx, maxy=maxy, cw=cw, ch=ch)


def standardize(in_path, out_path, geo=None, mode="letterbox", margin=0.0, verbose=True,
                pad_top=0.0, pad_bottom=0.0, pad_left=0.0, pad_right=0.0,
                bridge_gap_mult=BRIDGE_GAP_MULT, bridge_thin_alpha=BRIDGE_THIN_ALPHA):
    """Файловый вход: разобрать FXML-файл, применить пайплайн, записать результат."""
    parser = etree.XMLParser(remove_blank_text=False, remove_comments=False)
    tree = etree.parse(str(in_path), parser)
    root = tree.getroot()
    info = _standardize_tree(tree, root, geo=geo, mode=mode, margin=margin,
                             pad_top=pad_top, pad_bottom=pad_bottom, pad_left=pad_left, pad_right=pad_right,
                             bridge_gap_mult=bridge_gap_mult, bridge_thin_alpha=bridge_thin_alpha)
    if info is None:
        return None
    tree.write(str(out_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    if verbose:
        print(f"content bbox = ({info['minx']:.0f},{info['miny']:.0f})-({info['maxx']:.0f},{info['maxy']:.0f})  {info['cw']:.0f}x{info['ch']:.0f}")
        print(f"mode = {mode}   scale = {info['scale']:.4f}   offset = ({info['offx']:.1f},{info['offy']:.1f})   fixed skins = {info['nfix']}")
        print(f"extended pipe ends = {info['ext']}")
        print(f"-> {out_path}")
    return info


def standardize_xml(xml, geo=None, mode="letterbox", margin=0.0,
                    pad_top=0.0, pad_bottom=0.0, pad_left=0.0, pad_right=0.0,
                    bridge_gap_mult=BRIDGE_GAP_MULT, bridge_thin_alpha=BRIDGE_THIN_ALPHA):
    """In-memory вход: FXML-строка (или байты) -> стандартизованная FXML-строка.

    Для воркера: не пишет временных файлов. `<?import?>`/`<?xml?>` и комментарии
    сохраняются. При отсутствии содержимого возвращает исходный FXML без изменений.
    pad_* — поля под подписи (px); bridge_gap_mult — «ручка» зазора мостов.
    """
    import io
    parser = etree.XMLParser(remove_blank_text=False, remove_comments=False)
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    tree = etree.parse(io.BytesIO(data), parser)
    root = tree.getroot()
    if _standardize_tree(tree, root, geo=geo, mode=mode, margin=margin,
                         pad_top=pad_top, pad_bottom=pad_bottom, pad_left=pad_left, pad_right=pad_right,
                         bridge_gap_mult=bridge_gap_mult, bridge_thin_alpha=bridge_thin_alpha) is None:
        return xml if isinstance(xml, str) else data.decode("utf-8")
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8",
                          pretty_print=True).decode("utf-8")


def main():
    ap = argparse.ArgumentParser(description="Привести FXML-лист к 1920x1080 (letterbox).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--geo", default=str(Path(__file__).with_name("skin_geometry.json")))
    ap.add_argument("--mode", choices=["letterbox", "aspect", "full"], default="letterbox",
                    help="letterbox (реком.): только размер; aspect: + форма боксов под аспект "
                         "скина по оси подходящих линий; full: + contact-offset")
    ap.add_argument("--fix-skins", action="store_true", help="алиас для --mode full")
    ap.add_argument("--margin", type=float, default=0.0)
    ap.add_argument("--pad-top", type=float, default=0.0, help="поле сверху под подписи, px")
    ap.add_argument("--pad-bottom", type=float, default=0.0, help="поле снизу под подписи, px")
    ap.add_argument("--pad-left", type=float, default=0.0)
    ap.add_argument("--pad-right", type=float, default=0.0)
    ap.add_argument("--bridge-gap-mult", type=float, default=BRIDGE_GAP_MULT,
                    help="множитель зазора мостов («ручка»); стандарт 2.0")
    args = ap.parse_args()
    mode = "full" if args.fix_skins else args.mode
    out = args.output or str(Path(args.input).with_suffix("").as_posix() + "_1920x1080.fxml")
    geo = load_geo(args.geo)
    standardize(args.input, out, geo=geo, mode=mode, margin=args.margin,
                pad_top=args.pad_top, pad_bottom=args.pad_bottom,
                pad_left=args.pad_left, pad_right=args.pad_right,
                bridge_gap_mult=args.bridge_gap_mult)


if __name__ == "__main__":
    main()
