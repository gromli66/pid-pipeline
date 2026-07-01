"""
test_clean_ocr.py — быстрая проверка чистого OCR (П2) на одном изображении.

Запуск ВНУТРИ контейнера worker_ocr (там есть torch/surya/ultralytics/cv2):

  docker compose exec worker_ocr python /app/worker/test_clean_ocr.py \
      --image /storage/diagrams/<uid>/original/image.png \
      --out /storage/clean_ocr_test

Результат (в --out, он же ./storage на хосте):
  <name>__overlay.png  — картинка с зелёными боксами и распознанным текстом
  <name>__result.json  — то, что уйдёт в ocr_result.json (target[]/stats)

Опции: --whiten (выбеление фона), --expand-frac 0.15, --pad-frac 0.25,
        --device cuda|cpu (по умолч. из PID_DEVICE), --model путь к best.pt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/modules")

from modules.ocr.pipeline_clean import run_ocr_pipeline_clean  # noqa: E402


def _font(size=16):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                pass
    return ImageFont.load_default()


def draw_overlay(image_path, items, out_png):
    bgr = cv2.imread(str(image_path))
    img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    font = _font()
    for it in items:
        x0, y0, x1, y1 = it["bbox"]
        d.rectangle([x0, y0, x1, y1], outline=(0, 150, 0), width=2)
        t = it.get("text", "")
        if t:
            ty = max(0, y0 - 18)
            bb = d.textbbox((x0, ty), t, font=font)
            d.rectangle(bb, fill=(255, 255, 255))
            d.text((x0, ty), t, fill=(200, 0, 0), font=font)
    cv2.imwrite(str(out_png), cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default="/storage/clean_ocr_test")
    ap.add_argument("--model", default=os.getenv("TEXT_YOLO_WEIGHTS", "/models/text_detect/best.pt"))
    ap.add_argument("--device", default=os.getenv("PID_DEVICE", "auto"))
    ap.add_argument("--expand-frac", type=float, default=0.15,
                    help="реальное расширение бокса (возврат срезанных букв)")
    ap.add_argument("--pad-frac", type=float, default=0.25,
                    help="белое поле вокруг (quiet zone)")
    ap.add_argument("--whiten", action="store_true")
    args = ap.parse_args()

    device = args.device
    if device in ("auto", ""):
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = Path(args.image).stem

    print(f"[test] image={args.image}")
    print(f"[test] model={args.model} device={device} expand_frac={args.expand_frac} "
          f"pad_frac={args.pad_frac} whiten={args.whiten}")

    result = run_ocr_pipeline_clean(
        image_path=args.image,
        output_dir=out_dir,
        model_path=args.model,
        device=device,
        expand_frac=args.expand_frac,
        pad_frac=args.pad_frac,
        whiten=args.whiten,
    )

    items = result["target"]
    print("\n=== STATS ===")
    for k, v in result["stats"].items():
        print(f"  {k}: {v}")

    print(f"\n=== РАСПОЗНАНО {len(items)} блоков (первые 40) ===")
    for it in items[:40]:
        print(f"  {tuple(it['bbox'])}  conf={it['confidence']:.2f}  '{it['text']}'")

    out_json = out_dir / f"{name}__result.json"
    out_png = out_dir / f"{name}__overlay.png"
    json.dump(result, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    draw_overlay(args.image, items, out_png)
    print(f"\nГотово:\n  оверлей: {out_png}\n  json:    {out_json}")


if __name__ == "__main__":
    main()
