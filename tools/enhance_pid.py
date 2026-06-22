"""
Пакетная обработка сканов P&ID чертежей (оптимизированная версия).

ИСПОЛЬЗОВАНИЕ:
  python enhance_pid.py ./input ./output
  python enhance_pid.py ./input ./output -w 4
  python enhance_pid.py ./input ./output --no-denoise
  python enhance_pid.py ./input ./output --no-frame
  python enhance_pid.py scan.png result.png

ЗАВИСИМОСТИ:
  pip install opencv-python numpy
"""

import cv2
import numpy as np
import os
import sys
import argparse
import time
from multiprocessing import Pool


# ──────────────────────────────────────────
#  БЫСТРЫЕ УТИЛИТЫ
# ──────────────────────────────────────────

def fast_blur(img, sigma):
    """Быстрый blur: downscale → blur(σ=5) → upscale. В 20× быстрее GaussianBlur(σ=50)."""
    h, w = img.shape[:2]
    scale = max(1, int(sigma / 5))
    small = cv2.resize(img, (max(w // scale, 1), max(h // scale, 1)), interpolation=cv2.INTER_AREA)
    blurred = cv2.GaussianBlur(small, (0, 0), 5)
    return cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)


def fast_morph_open_line(binary, length, axis):
    """Быстрое морфологическое открытие для длинных линий через downscale."""
    h, w = binary.shape
    # Минимальный scale чтобы ядро было >= 20px
    scale = max(1, length // 40)
    small_len = max(length // scale, 3)

    if scale > 1:
        if axis == 0:  # горизонтальное
            small = cv2.resize(binary, (max(w // scale, 1), h), interpolation=cv2.INTER_AREA)
        else:  # вертикальное
            small = cv2.resize(binary, (w, max(h // scale, 1)), interpolation=cv2.INTER_AREA)
        _, small = cv2.threshold(small, 127, 255, cv2.THRESH_BINARY)
    else:
        small = binary

    if axis == 0:
        kern = cv2.getStructuringElement(cv2.MORPH_RECT, (small_len, 1))
    else:
        kern = cv2.getStructuringElement(cv2.MORPH_RECT, (1, small_len))

    opened = cv2.morphologyEx(small, cv2.MORPH_OPEN, kern)

    if scale > 1:
        opened = cv2.resize(opened, (w, h), interpolation=cv2.INTER_NEAREST)
        _, opened = cv2.threshold(opened, 127, 255, cv2.THRESH_BINARY)

    return opened


# ──────────────────────────────────────────
#  ЭТАПЫ
# ──────────────────────────────────────────

def detect_lines(gray_f):
    """Детекция линий: сильные + слабые (контраст с фоном)."""
    local_bg = fast_blur(gray_f, 50)
    contrast = local_bg - gray_f
    strong = (gray_f < 215).astype(np.float32)
    weak = ((gray_f >= 215) & (gray_f < 237) & (contrast > 2)).astype(np.float32)
    line_mask = np.clip(strong + weak, 0, 1)
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    line_wide = cv2.dilate(line_mask.astype(np.uint8), kern, iterations=2).astype(np.float32)
    return cv2.GaussianBlur(line_wide, (5, 5), 0)


def denoise_background(img, line_mask, h_param=12):
    """NLMeans только на фоне."""
    cleaned = cv2.fastNlMeansDenoisingColored(img, None, h_param, h_param, 7, 21)
    m = line_mask[:, :, None]
    return np.clip(img.astype(np.float32) * m +
                   cleaned.astype(np.float32) * (1 - m), 0, 255).astype(np.uint8)


def whiten_background(img, line_mask=None):
    """Шум фона → белый. line_mask защищает обнаруженные линии."""
    white = np.float32(255)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    local_bg = fast_blur(gray, 50)
    noise = ((gray > 220) & ((local_bg - gray) < 3)).astype(np.float32)
    if line_mask is not None:
        noise = noise * (1.0 - line_mask)  # не трогаем пиксели, опознанные как линии
    noise = cv2.GaussianBlur(noise, (5, 5), 0)
    n3 = noise[:, :, None]
    return np.clip(img.astype(np.float32) * (1 - n3) + white * n3, 0, 255).astype(np.uint8)


def enhance_lines(orig, whitened):
    """Усиление линий по levels-маске."""
    gray = cv2.cvtColor(whitened, cv2.COLOR_BGR2GRAY)
    # LUT вместо цикла
    lut = np.zeros(256, dtype=np.uint8)
    lut[121:250] = np.linspace(0, 255, 129).astype(np.uint8)
    lut[250:] = 255
    levels = lut[gray]
    mask = np.clip((1.0 - levels.astype(np.float32) / 255.0) * 5, 0, 1)
    mask = cv2.GaussianBlur(mask, (3, 3), 0)

    lab = cv2.cvtColor(orig, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l_out = np.clip(l.astype(np.float32) * (1.0 - mask * 0.6), 0, 255).astype(np.uint8)
    a_out = np.clip(128 + (a.astype(np.float32) - 128) * (1.0 + mask), 0, 255).astype(np.uint8)
    b_out = np.clip(128 + (b.astype(np.float32) - 128) * (1.0 + mask), 0, 255).astype(np.uint8)
    return cv2.cvtColor(cv2.merge([l_out, a_out, b_out]), cv2.COLOR_LAB2BGR)


def remove_dust(img, max_area=25):
    """Удаление пыли через морфологическое открытие (быстрее CC)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    dark = (gray < 245).astype(np.uint8)
    # Open с ядром 6×6 убивает объекты < ~25px (≈ 5×5)
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (6, 6))
    opened = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kern)
    # Dilate обратно чтобы восстановить размер выживших
    survived = cv2.dilate(opened, kern, iterations=1)
    # Пыль = было тёмное, но не выжило
    dust = dark & (~survived.astype(bool)).astype(np.uint8)
    dust = cv2.GaussianBlur(dust.astype(np.float32), (3, 3), 0)
    white = np.float32(255)
    return np.clip(img.astype(np.float32) * (1 - dust[:, :, None]) +
                   white * dust[:, :, None], 0, 255).astype(np.uint8)


def remove_frame(img):
    """Удаление рамки ГОСТ: геометрический поиск границ рамки от краёв."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

    # Плотность тёмных пикселей по строкам и столбцам
    row_dens = np.sum(binary > 0, axis=1).astype(np.float64) / w
    col_dens = np.sum(binary > 0, axis=0).astype(np.float64) / h

    # Порог: линия рамки = строка/столбец с >40% тёмных пикселей
    line_thr = 0.40
    # Порог «пусто»: плотность < 2% = белое поле / промежуток
    gap_thr = 0.02
    search_limit = min(h, w) // 4

    def _find_content_edge(density, size, from_start=True):
        """Ищем от края первую линию рамки, потом первый 'пустой' ряд после неё."""
        rng = range(size) if from_start else range(size - 1, -1, -1)
        found_line = False
        last_line = -1
        steps = 0
        for i in rng:
            steps += 1
            if steps > search_limit:
                return 0 if from_start else size - 1  # рамка не найдена
            if density[i] > line_thr:
                found_line = True
                last_line = i
            elif found_line and density[i] < gap_thr:
                return i
        return 0 if from_start else size - 1

    top = _find_content_edge(row_dens, h, from_start=True)
    bot = _find_content_edge(row_dens, h, from_start=False)
    left = _find_content_edge(col_dens, w, from_start=True)
    right = _find_content_edge(col_dens, w, from_start=False)

    # Проверка: рамка должна занимать хотя бы немного
    frame_area = top * w + (h - bot) * w + h * left + h * (w - right)
    if frame_area < h * w * 0.005:
        return img

    result = img.copy()
    result[:top, :] = 255
    result[bot + 1:, :] = 255
    result[:, :left] = 255
    result[:, right + 1:] = 255
    return result


# ──────────────────────────────────────────
#  ПАЙПЛАЙН
# ──────────────────────────────────────────

def process_image(img, no_denoise=False, no_frame=False):
    if len(img.shape) == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    gray_f = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    line_mask = detect_lines(gray_f)

    if not no_denoise:
        denoised = denoise_background(img, line_mask)
    else:
        denoised = img

    whitened = whiten_background(denoised, line_mask)
    enhanced = enhance_lines(img, whitened)
    result = whiten_background(enhanced, line_mask)
    result = remove_dust(result)

    if not no_frame:
        result = remove_frame(result)

    return result


# ──────────────────────────────────────────
#  ПАКЕТНАЯ ОБРАБОТКА
# ──────────────────────────────────────────

EXTENSIONS = (".png", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg")


def _process_one(args):
    in_path, out_path, no_denoise, no_frame = args
    try:
        t = time.time()
        img = cv2.imread(in_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return in_path, False, 0, "ошибка чтения"
        result = process_image(img, no_denoise=no_denoise, no_frame=no_frame)
        cv2.imwrite(out_path, result)
        return in_path, True, time.time() - t, ""
    except Exception as e:
        return in_path, False, 0, str(e)


def batch_process(input_path, output_path, workers=1, no_denoise=False, no_frame=False):
    if os.path.isfile(input_path):
        files = [(input_path, output_path)]
    else:
        os.makedirs(output_path, exist_ok=True)
        names = sorted(f for f in os.listdir(input_path) if f.lower().endswith(EXTENSIONS))
        if not names:
            print(f"Нет изображений в {input_path}")
            return
        files = [(os.path.join(input_path, f),
                  os.path.join(output_path, os.path.splitext(f)[0] + ".png"))
                 for f in names]

    total = len(files)
    print(f"Файлов: {total} | Воркеров: {workers} | "
          f"NLMeans: {'выкл' if no_denoise else 'вкл'} | "
          f"Рамка: {'выкл' if no_frame else 'вкл'}", flush=True)
    print("-" * 60, flush=True)

    tasks = [(inp, out, no_denoise, no_frame) for inp, out in files]
    t0 = time.time()

    if workers <= 1:
        for i, task in enumerate(tasks, 1):
            r = _process_one(task)
            fname = os.path.basename(r[0])
            if r[1]:
                print(f"  [{i}/{total}] {fname} → OK ({r[2]:.1f}s)", flush=True)
            else:
                print(f"  [{i}/{total}] {fname} → ОШИБКА: {r[3]}", flush=True)
    else:
        with Pool(workers) as pool:
            for i, r in enumerate(pool.imap(_process_one, tasks), 1):
                fname = os.path.basename(r[0])
                if r[1]:
                    print(f"  [{i}/{total}] {fname} → OK ({r[2]:.1f}s)", flush=True)
                else:
                    print(f"  [{i}/{total}] {fname} → ОШИБКА: {r[3]}", flush=True)

    elapsed = time.time() - t0
    print("-" * 60, flush=True)
    print(f"Готово! {total} файлов за {elapsed:.0f}с ({elapsed / max(total, 1):.1f}с/файл)",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description="Обработка сканов P&ID")
    parser.add_argument("input", help="Папка или файл")
    parser.add_argument("output", help="Папка или файл")
    parser.add_argument("-w", "--workers", type=int, default=1)
    parser.add_argument("--no-denoise", action="store_true")
    parser.add_argument("--no-frame", action="store_true")
    args = parser.parse_args()

    batch_process(args.input, args.output,
                  workers=args.workers,
                  no_denoise=args.no_denoise,
                  no_frame=args.no_frame)


if __name__ == "__main__":
    main()
