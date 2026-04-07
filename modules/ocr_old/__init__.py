"""
OCR Pipeline Module — распознавание текста на P&ID.

Steps:
  1. clean_pid      — очистка диаграммы от коммуникаций
  2. surya_detect   — Surya detection+recognition на полном изображении (тайлинг)
  3. recluster      — рекластеризация OCR-блоков
  3.5 crop_saver    — сохранение кропов на диск
  4a. surya_recognize — Surya recognition на кропах
  4b. paddle_recognize — PaddleOCR recognition на кропах (CPU)
  5. merge_results  — объединение 3 источников → ocr_final.json

Использование из task:
  from modules.ocr.clean_pid import run_clean
  from modules.ocr.surya_detect import load_surya_models, run_detect
  from modules.ocr.recluster import run_recluster
  from modules.ocr.crop_saver import save_crops_from_blocks
  from modules.ocr.surya_recognize import run_recognize
  from modules.ocr.paddle_recognize import run_paddle_recognize
  from modules.ocr.merge_results import run_merge
"""
