"""
OCR Pipeline Module — 3-проходный OCR для P&ID.

Новый pipeline (Phase B):
  - Surya OCR с мультимасштабным тайлингом (3 итерации)
  - Доменные профили (KKS, ISA S5.1)
  - Семантическая регруппировка блоков

API:
  from modules.ocr.pipeline import run_ocr_pipeline
  from modules.ocr.domain_profile import BaseDomainProfile, load_profile
  from modules.ocr.evaluate import semantic_regroup

Профили хранятся в configs/projects/<project>/ocr_profile.py
"""
