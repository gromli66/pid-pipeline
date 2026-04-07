"""
PaddleOCR Recognition HTTP Service.

Принимает изображение (crop), возвращает распознанный текст + confidence.
Работает в чистом Python контейнере без torch — не зависает.

Endpoints:
  POST /recognize       — распознать один crop (multipart/form-data)
  POST /recognize-batch — распознать несколько crops
  GET  /health          — проверка здоровья
"""

import io
import logging
import time
from typing import Optional

import cv2
import numpy as np
from PIL import Image
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PaddleOCR Service", version="1.0.0")

# ── Глобальная модель ─────────────────────────────────────

_model = None
MODEL_NAME = "PP-OCRv5_server_rec"
MAX_SIDE = 1000


def _get_model():
    global _model
    if _model is None:
        logger.info("Loading PaddleX model: %s ...", MODEL_NAME)
        t0 = time.time()
        from paddlex import create_model
        _model = create_model(MODEL_NAME)
        logger.info("Model loaded in %.1fs", time.time() - t0)
    return _model


def _recognize_image(img: np.ndarray) -> dict:
    """Распознать текст на одном изображении."""
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        sc = MAX_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * sc), int(h * sc)))

    model = _get_model()
    results = list(model.predict(img))

    texts = []
    confs = []
    for res in results:
        if hasattr(res, "rec_text"):
            texts.append(str(res.rec_text))
            confs.append(float(getattr(res, "rec_score", 0.0)))
        elif isinstance(res, dict) and "rec_text" in res:
            texts.append(str(res["rec_text"]))
            confs.append(float(res.get("rec_score", 0.0)))
        elif isinstance(res, dict) and "rec_texts" in res:
            for i in range(len(res["rec_texts"])):
                texts.append(str(res["rec_texts"][i]))
                confs.append(float(res["rec_scores"][i]))

    text = " ".join(texts).strip()
    avg_conf = sum(confs) / len(confs) if confs else 0.0

    return {"text": text, "confidence": round(avg_conf, 4)}


# ── Endpoints ─────────────────────────────────────────────

@app.get("/health")
async def health():
    """Проверка здоровья + загрузка модели при первом вызове."""
    try:
        _get_model()
        return {"status": "healthy", "model": MODEL_NAME}
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)},
        )


@app.post("/recognize")
async def recognize(file: UploadFile = File(...)):
    """
    Распознать текст на одном crop изображении.

    Принимает: image/png или image/jpeg
    Возвращает: {"text": "...", "confidence": 0.95}
    """
    try:
        content = await file.read()
        nparr = np.frombuffer(content, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(status_code=400, detail="Cannot decode image")

        result = _recognize_image(img)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Recognition failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


class BatchItem(BaseModel):
    index: int
    text: str
    confidence: float


@app.post("/recognize-batch")
async def recognize_batch(files: list[UploadFile] = File(...)):
    """
    Распознать текст на нескольких crop изображениях.

    Принимает: multipart/form-data с несколькими файлами
    Возвращает: [{"index": 0, "text": "...", "confidence": 0.95}, ...]
    """
    results = []
    t0 = time.time()

    for i, file in enumerate(files):
        try:
            content = await file.read()
            nparr = np.frombuffer(content, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if img is None:
                results.append({"index": i, "text": "", "confidence": 0.0, "error": "decode_failed"})
                continue

            result = _recognize_image(img)
            results.append({"index": i, "text": result["text"], "confidence": result["confidence"]})

        except Exception as e:
            logger.warning("Batch item %d failed: %s", i, e)
            results.append({"index": i, "text": "", "confidence": 0.0, "error": str(e)})

    elapsed = time.time() - t0
    logger.info("Batch: %d crops in %.1fs (%.2fs/crop)", len(files), elapsed,
                elapsed / max(1, len(files)))

    return results


# ── Предзагрузка модели при старте ────────────────────────

@app.on_event("startup")
async def startup():
    """Загрузить модель при старте сервиса."""
    logger.info("Pre-loading model on startup...")
    try:
        _get_model()
        logger.info("Model ready")
    except Exception as e:
        logger.error("Failed to pre-load model: %s", e)
