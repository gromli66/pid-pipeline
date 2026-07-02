#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fxml_preview.py — быстрый предпросмотр FXML-листа в PNG БЕЗ JavaFX/SceneBuilder.

Рисует лист так, как его увидит SceneBuilder:
  - Rectangle / Line / Polyline / Polygon / Text — как есть;
  - кастом-контролы — реальный «след» скина: бокс layout (пунктир) и
    вписанную по аспекту скина графику (заливка) + ось контакта (талия).
Если графика внутри пунктирного бокса смещена — так же будет и в SceneBuilder.
После --fix-skins бокс и графика совпадают.

Запуск:
    python3 tools/fxml_preview.py out_1920x1080.fxml -o preview.png
"""

import argparse, json, re
from pathlib import Path
from lxml import etree
from PIL import Image, ImageDraw, ImageFont

FONT_RE = re.compile(r"-fx-font-size\s*:\s*([0-9]*\.?[0-9]+)", re.I)


def load_font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(p, max(6, int(size)))
        except Exception:
            pass
    return ImageFont.load_default()


def fnum(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def lname(el):
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else None


def parse_color(c, default=(60, 60, 60)):
    if not c:
        return default
    c = c.strip()
    if c.upper() in ("TRANSPARENT",):
        return None
    if c.startswith("#"):
        h = c[1:]
        if len(h) in (6, 8):
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
        if len(h) == 3:
            return tuple(int(h[i]*2, 16) for i in range(3))
    named = {"WHITE": (255,255,255), "BLACK": (0,0,0), "RED": (220,40,40),
             "GREEN": (110,240,110)}
    return named.get(c.upper(), default)


def render(in_path, out_path, geo_path=None, scale=0.5, show_boxes=True):
    tree = etree.parse(str(in_path))
    root = tree.getroot()
    W = int(fnum(root.get("prefWidth"), 1920) * scale)
    H = int(fnum(root.get("prefHeight"), 1080) * scale)
    bg = parse_color(re.search(r"-fx-background-color:\s*([^;]+)",
                     root.get("style", "")).group(1).split()[0]
                     if "-fx-background-color" in root.get("style", "") else "#d4d4d4",
                     (212, 212, 212)) or (212, 212, 212)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img, "RGBA")

    geo = json.loads(Path(geo_path).read_text(encoding="utf-8")) if geo_path and Path(geo_path).exists() else {"skins": {}, "defaults": {}}
    skins = geo.get("skins", {})
    default = geo.get("defaults", {"aspect_hw": 1.0, "contact": [0, 0]})

    def S(v):
        return v * scale

    # рамка листа
    d.rectangle([0, 0, W-1, H-1], outline=(120, 120, 120), width=2)

    for el in root.iter():
        if el is root:
            continue
        name = lname(el)
        a = el.attrib
        lx, ly = fnum(a.get("layoutX"), 0.0), fnum(a.get("layoutY"), 0.0)

        if name == "Rectangle":
            w, h = fnum(a.get("width"), 0), fnum(a.get("height"), 0)
            fill = parse_color(a.get("fill"), None)
            stroke = parse_color(a.get("stroke"), (120,120,120))
            box = [S(lx), S(ly), S(lx+w), S(ly+h)]
            if fill:
                d.rectangle(box, fill=fill)
            if stroke:
                d.rectangle(box, outline=stroke, width=1)

        elif name == "Line":
            sx, sy = fnum(a.get("startX"),0), fnum(a.get("startY"),0)
            ex, ey = fnum(a.get("endX"),0), fnum(a.get("endY"),0)
            has_l = ("layoutX" in a) or ("layoutY" in a)
            ox, oy = (lx, ly) if has_l else (0.0, 0.0)
            d.line([S(ox+sx), S(oy+sy), S(ox+ex), S(oy+ey)],
                   fill=parse_color(a.get("stroke"), (40,40,40)),
                   width=max(1, int(S(fnum(a.get("strokeWidth"),1)))))

        elif name in ("Polyline", "Polygon"):
            nums = [fnum(x) for x in re.split(r"[,\s]+", a.get("points","").strip()) if x!=""]
            nums = [n for n in nums if n is not None]
            has_l = ("layoutX" in a) or ("layoutY" in a)
            ox, oy = (lx, ly) if has_l else (0.0, 0.0)
            pts = [(S(ox+nums[i]), S(oy+nums[i+1])) for i in range(0, len(nums)-1, 2)]
            if len(pts) >= 2:
                if name == "Polygon":
                    d.polygon(pts, fill=parse_color(a.get("fill"), (200,200,240))+(160,),
                              outline=parse_color(a.get("stroke"), (60,60,60)))
                else:
                    d.line(pts, fill=parse_color(a.get("stroke"), (40,40,40)),
                           width=max(1, int(S(fnum(a.get("strokeWidth"),1)))))

        elif name == "Text":
            fs = 12.0
            m = FONT_RE.search(a.get("style",""))
            if m: fs = fnum(m.group(1), 12)
            # nested Font
            for ch in el.iter():
                if lname(ch) == "Font" and ch.get("size"):
                    fs = fnum(ch.get("size"), fs)
            fnt = load_font(S(fs))
            col = parse_color(a.get("fill"), (40,40,40))
            d.text((S(lx), S(ly)-S(fs)*0.1), a.get("text",""), fill=col, font=fnt)

        elif "skinType" in a:
            st = a["skinType"]
            g = skins.get(st, default)
            aspect = float(g.get("aspect_hw", 1.0))
            hf, vf = g.get("contact", [0.0, 0.0])
            orient = a.get("orientation", "HORIZONTAL")
            vertical = orient.startswith("VERTICAL")
            r = (1.0/aspect) if vertical else aspect     # prefH/prefW target
            pw, ph = fnum(a.get("prefWidth"),0), fnum(a.get("prefHeight"),0)
            # вписать графику скина по аспекту в бокс (letterbox)
            if pw*r <= ph:
                fw, fh = pw, pw*r
            else:
                fh, fw = ph, ph/r
            fx, fy = lx + (pw-fw)/2, ly + (ph-fh)/2
            lay = [S(lx), S(ly), S(lx+pw), S(ly+ph)]
            foot = [S(fx), S(fy), S(fx+fw), S(fy+fh)]
            if show_boxes:
                # пунктир layout-бокса
                d.rectangle(lay, outline=(150,150,150), width=1)
            # графика скина
            ctrl = g.get("control", "")
            fill = (110,240,110,150)
            if ctrl == "PumpControl" or st in ("PUMP","FAN"):
                d.ellipse(foot, fill=fill, outline=(30,120,30))
            elif "VLV" in st:      # бабочка/бантик
                x0,y0,x1,y1 = foot; cx,cy=(x0+x1)/2,(y0+y1)/2
                d.polygon([(x0,y0),(x1,y1),(x0,y1),(x1,y0)], fill=fill, outline=(30,120,30))
            else:
                d.rectangle(foot, fill=fill, outline=(30,120,30))
            # ось контакта (талия)
            x0,y0,x1,y1 = foot
            if orient.startswith("HORIZONTAL"):
                wy = (y0+y1)/2 + hf*(y1-y0)
                d.line([x0, wy, x1, wy], fill=(220,40,40), width=1)
            else:
                wx = (x0+x1)/2 - vf*(x1-x0)
                d.line([wx, y0, wx, y1], fill=(220,40,40), width=1)
            # подпись типа скина
            d.text((S(lx), S(ly)-9), st, fill=(20,20,120), font=load_font(9))

    img.save(out_path)
    print(f"-> {out_path}  ({W}x{H})")


def main():
    ap = argparse.ArgumentParser(description="PNG-предпросмотр FXML без SceneBuilder")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--geo", default=str(Path(__file__).with_name("skin_geometry.json")))
    ap.add_argument("--scale", type=float, default=0.5)
    args = ap.parse_args()
    out = args.output or str(Path(args.input).with_suffix(".png"))
    render(args.input, out, geo_path=args.geo, scale=args.scale)


if __name__ == "__main__":
    main()
