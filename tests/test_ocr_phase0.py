#!/usr/bin/env python3
"""
Фаза 0: Проверка совместимости OCR worker.

Запуск внутри контейнера worker_ocr:
  docker run --rm --gpus all -v ./tests:/app/tests -v ./models/ocr:/models/ocr \
    -e HF_HOME=/models/ocr/hf_cache pid_worker_ocr python /app/tests/test_ocr_phase0.py

Проверяет:
  1. torch + CUDA
  2. Surya API (FoundationPredictor / RecognitionPredictor / DetectionPredictor)
  3. Surya OCR на белом изображении + кириллица
  4. PaddlePaddle + PaddleX (create_model + predict формат)
  5. PaddleOCR new API (optional)
  6. torch + paddle совместимость
  7. Шрифты

ВАЖНО: Surya модели загружаются ОДИН раз и переиспользуются.
"""

import sys
import traceback
import gc

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results = []


def check(name, fn):
    try:
        result = fn()
        results.append((PASS, name, result))
        print(f"  {PASS} {name}: {result}", flush=True)
    except Exception as e:
        results.append((FAIL, name, str(e)))
        print(f"  {FAIL} {name}: {e}", flush=True)
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════
# 1. PyTorch + CUDA
# ═══════════════════════════════════════════════════════════
print("\n=== 1. PyTorch + CUDA ===", flush=True)


def test_torch():
    import torch
    cuda_ok = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_ok else "N/A"
    vram = torch.cuda.get_device_properties(0).total_mem // (1024**2) if cuda_ok else 0
    return f"torch {torch.__version__}, CUDA={cuda_ok}, GPU={gpu_name}, VRAM={vram}MB"


check("PyTorch + CUDA", test_torch)

# ═══════════════════════════════════════════════════════════
# 2. Surya — импорты
# ═══════════════════════════════════════════════════════════
print("\n=== 2. Surya OCR imports ===", flush=True)


def test_surya_import():
    import surya
    return f"surya {surya.__version__ if hasattr(surya, '__version__') else 'OK'}"


def test_surya_classes():
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    from surya.detection import DetectionPredictor
    return "FoundationPredictor, RecognitionPredictor, DetectionPredictor — all importable"


check("Surya import", test_surya_import)
check("Surya classes", test_surya_classes)

# ═══════════════════════════════════════════════════════════
# 3. Surya — создание предикторов + OCR тесты (ОДИН РАЗ)
# ═══════════════════════════════════════════════════════════
print("\n=== 3. Surya predictors + OCR tests ===", flush=True)

_surya_foundation = None
_surya_rec = None
_surya_det = None


def _load_surya():
    global _surya_foundation, _surya_rec, _surya_det
    if _surya_foundation is not None:
        return
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    from surya.detection import DetectionPredictor

    print("  ... Loading Surya models (first time, may download ~1.4GB) ...", flush=True)
    _surya_foundation = FoundationPredictor()
    _surya_rec = RecognitionPredictor(_surya_foundation)
    _surya_det = DetectionPredictor()
    print("  ... Surya models loaded.", flush=True)


def test_surya_create():
    _load_surya()
    return "All 3 predictors created OK"


def test_surya_ocr_blank():
    """OCR на белом изображении — ожидаем 0 строк."""
    _load_surya()
    from PIL import Image
    img = Image.new("RGB", (200, 50), "white")
    preds = _surya_rec([img], det_predictor=_surya_det)
    n_lines = len(preds[0].text_lines) if preds else 0
    return f"Blank image: {n_lines} lines (expect 0)"


def test_surya_cyrillic():
    """OCR на синтетическом изображении с кириллицей."""
    _load_surya()
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (400, 60), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except (OSError, IOError):
        font = ImageFont.load_default()
    draw.text((10, 10), "ТЕСТ DN50 ABC", fill="black", font=font)
    preds = _surya_rec([img], det_predictor=_surya_det)
    texts = [line.text for line in preds[0].text_lines] if preds else []
    return f"Recognized: {texts}"


check("Create Surya predictors", test_surya_create)
check("Surya OCR blank image", test_surya_ocr_blank)
check("Surya cyrillic", test_surya_cyrillic)

# Освобождаем GPU
print("  ... Releasing Surya GPU memory ...", flush=True)
del _surya_rec, _surya_det, _surya_foundation
_surya_rec = _surya_det = _surya_foundation = None
import torch
torch.cuda.empty_cache()
gc.collect()
print("  ... GPU memory released.", flush=True)

# ═══════════════════════════════════════════════════════════
# 4. PaddlePaddle + PaddleX
# ═══════════════════════════════════════════════════════════
print("\n=== 4. PaddlePaddle + PaddleX ===", flush=True)


def test_paddle_import():
    import paddle
    return f"paddle {paddle.__version__}, device={paddle.get_device()}"


def test_paddlex_import():
    from paddlex import create_model
    return "paddlex.create_model importable"


def test_paddlex_rec_model_and_predict():
    """Создать модель + predict на белом кропе. Проверить формат ответа."""
    import numpy as np
    from paddlex import create_model

    model = create_model("PP-OCRv5_server_rec")
    model_type = type(model).__name__

    # Белый crop 100x30
    img = np.ones((30, 100, 3), dtype=np.uint8) * 255
    results = list(model.predict(img))

    if not results:
        return f"Model={model_type}, predict returned EMPTY results"

    res = results[0]
    # Определить формат ответа
    if hasattr(res, '__dict__'):
        attrs = list(res.__dict__.keys())[:15]
        # Попробовать достать rec_text
        rec_text = None
        rec_score = None
        if hasattr(res, 'rec_text'):
            rec_text = res.rec_text
            rec_score = getattr(res, 'rec_score', None)
        elif hasattr(res, 'res') and isinstance(res.res, dict):
            rec_text = res.res.get('rec_text')
            rec_score = res.res.get('rec_score')
        return (f"Model={model_type}, result_type=object, attrs={attrs}, "
                f"rec_text={rec_text!r}, rec_score={rec_score}")
    elif isinstance(res, dict):
        keys = list(res.keys())[:10]
        rec_text = res.get('rec_text') or (res.get('res', {}) or {}).get('rec_text')
        return f"Model={model_type}, result_type=dict, keys={keys}, rec_text={rec_text!r}"
    else:
        return f"Model={model_type}, result_type={type(res).__name__}, value={str(res)[:200]}"


check("PaddlePaddle import", test_paddle_import)
check("PaddleX import", test_paddlex_import)
check("PaddleX server_rec model + predict", test_paddlex_rec_model_and_predict)

# ═══════════════════════════════════════════════════════════
# 4b. PaddleOCR new API (optional)
# ═══════════════════════════════════════════════════════════
print("\n=== 4b. PaddleOCR new API (optional) ===", flush=True)


def test_paddleocr_new_api():
    try:
        from paddleocr import TextRecognition
        model = TextRecognition(model_name="PP-OCRv5_server_rec")
        return f"TextRecognition created: {type(model).__name__}"
    except ImportError:
        return "paddleocr not installed (optional — paddlex API works)"
    except Exception as e:
        return f"paddleocr available but TextRecognition failed: {e}"


check("paddleocr.TextRecognition", test_paddleocr_new_api)

# ═══════════════════════════════════════════════════════════
# 5. torch + paddle совместимость
# ═══════════════════════════════════════════════════════════
print("\n=== 5. torch + paddle coexistence ===", flush=True)


def test_coexistence():
    import torch
    import paddle
    import numpy as np

    t = torch.tensor([1.0, 2.0, 3.0])
    p = paddle.to_tensor([1.0, 2.0, 3.0])
    np_from_torch = t.numpy()
    np_from_paddle = p.numpy()
    assert np.allclose(np_from_torch, np_from_paddle)
    return f"torch {torch.__version__} + paddle {paddle.__version__} coexist OK"


check("torch + paddle coexistence", test_coexistence)

# ═══════════════════════════════════════════════════════════
# 6. Шрифты
# ═══════════════════════════════════════════════════════════
print("\n=== 6. Fonts ===", flush=True)


def test_fonts():
    from PIL import ImageFont
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    found = []
    for fp in font_paths:
        try:
            ImageFont.truetype(fp, 14)
            found.append(fp.split("/")[-1])
        except (OSError, IOError):
            pass
    if found:
        return f"Found: {', '.join(found)}"
    return "No fonts found — visualization will fail!"


check("DejaVu fonts", test_fonts)

# ═══════════════════════════════════════════════════════════
# 7. Кэш-пути (для docker volumes)
# ═══════════════════════════════════════════════════════════
print("\n=== 7. Cache paths ===", flush=True)


def test_cache_paths():
    import os
    paths = {
        "HF_HOME": os.environ.get("HF_HOME", "NOT SET"),
        "~/.cache/datalab": os.path.exists(os.path.expanduser("~/.cache/datalab")),
        "~/.cache/huggingface": os.path.exists(os.path.expanduser("~/.cache/huggingface")),
        "~/.paddlex": os.path.exists(os.path.expanduser("~/.paddlex")),
    }
    return str(paths)


check("Cache paths", test_cache_paths)

# ═══════════════════════════════════════════════════════════
# Итого
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*60}", flush=True)
print("SUMMARY:", flush=True)
passed = sum(1 for s, _, _ in results if s == PASS)
failed = sum(1 for s, _, _ in results if s == FAIL)
print(f"  Passed: {passed}, Failed: {failed}")

if failed:
    print("\nFAILED:")
    for s, name, msg in results:
        if s == FAIL:
            print(f"  {FAIL} {name}: {msg}")

print(flush=True)
sys.exit(1 if failed else 0)
