# ADR-004: Отдельный worker для SAM2

**Статус:** Принято  
**Дата:** 2026-01  
**Контекст:** Архитектура Celery workers для GPU-задач

---

## Контекст

Pipeline использует Celery workers для GPU-интенсивных задач. Основные стадии (detection, segmentation, skeleton, junction) работают последовательно через один worker на queue `gpu`. SAM2 (contour extraction) может работать параллельно с graph building и OCR.

## Рассмотренные альтернативы

**Один worker `gpu` для всего.** Простая архитектура, один процесс. Проблема: SAM2 base model (~150 MB GPU RAM) + LoRA + inference buffer → ~4 GB. Если загружен одновременно с другими моделями, суммарно > 8 GB — превышает RTX 4070 Laptop.

**SAM2 в отдельном worker/queue.** Два процесса: `gpu` (detection, segmentation, junction) и `sam2` (contour extraction). Каждый загружает только свои модели. Минус: два процесса, координация через Celery.

## Решение

Отдельный worker для SAM2 на queue `sam2`.

## Обоснование

1. **GPU memory isolation.** RTX 4070 Laptop — 8 GB VRAM. Detection (YOLO ~4 GB) + UNet++ (~3 GB) уже занимают основную часть. SAM2 не может работать одновременно с ними в одном процессе. Раздельные workers загружают модели по очереди или на разных GPU (при наличии).
2. **Параллелизм.** SAM2 contour extraction запускается после detection (нужны bbox), но не зависит от segmentation/skeleton/junction/OCR. Параллельный запуск сокращает wall-time обработки схемы.
3. **Fault isolation.** Падение SAM2 worker (OOM, CUDA error) не блокирует основной pipeline. Contour extraction помечается как failed, остальные стадии продолжают.

## Последствия

- `docker-compose.yml` содержит отдельный сервис `worker_sam2` с `--queues sam2`.
- Contour task ставится в queue `sam2` через `app.send_task('contours.extract', queue='sam2')`.
- При отсутствии SAM2 worker — задачи накапливаются в Redis, не блокируя остальные.
- На системах с 2× GPU: `gpu` worker на GPU:0, `sam2` worker на GPU:1 — полный параллелизм.
