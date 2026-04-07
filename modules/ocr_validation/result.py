"""
Структуры данных для результатов OCR-валидации.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class BlockType(str, Enum):
    KKS = "kks"
    DIAMETER = "diameter"
    ANNOTATION = "annotation"
    UNKNOWN = "unknown"


class MatchQuality(str, Enum):
    EXACT = "exact"           # clean regex, без коррекций
    CORRECTED = "corrected"   # match после OCR-corrections
    NONE = "none"             # regex не сработал


class ValidationColor(str, Enum):
    GREEN = "green"           # exact match
    YELLOW = "yellow"         # corrected match
    ORANGE = "orange"         # не match, но похоже на код
    DEFAULT = "default"       # аннотация, не код


class ConfirmStatus(str, Enum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    DELETED = "deleted"


@dataclass
class BlockClassification:
    """Результат классификации одного OCR-блока."""
    block_idx: int
    block_type: BlockType
    match_quality: MatchQuality
    color: ValidationColor
    confirm_status: ConfirmStatus = ConfirmStatus.UNCONFIRMED

    # KKS (если найден):
    kks_full: Optional[str] = None        # "10LBG30AA904"
    kks_block: Optional[str] = None
    kks_system: Optional[str] = None
    kks_fn: Optional[str] = None
    kks_unit: Optional[str] = None
    kks_num: Optional[str] = None
    kks_suffix: Optional[str] = None
    kks_span: Optional[tuple] = None      # (start, end) в тексте

    # Diameter (если найден):
    diameter_text: Optional[str] = None
    diameter_value: Optional[int] = None
    diameter_prefix: Optional[str] = None
    diameter_suffix: Optional[str] = None

    # Общее:
    remaining_text: str = ""
    original_text: str = ""
    corrected_text: str = ""


@dataclass
class ValidationReport:
    """Суммарный результат валидации всех блоков."""
    classifications: list[BlockClassification] = field(default_factory=list)
    total_blocks: int = 0
    kks_exact: int = 0
    kks_corrected: int = 0
    diameter_exact: int = 0
    diameter_corrected: int = 0
    orange_count: int = 0
    annotation_count: int = 0
