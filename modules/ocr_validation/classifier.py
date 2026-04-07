"""
OcrBlockClassifier — единый классификатор OCR-блоков.

Двухпроходный алгоритм для каждого блока:
1. DiameterMatcher → найти диаметр, вычесть из текста
2. KksMatcher → найти KKS в оставшемся тексте
3. Тип = по найденному; цвет = по quality match

Порядок важен: вычитание диаметра решает проблему "10LBG50 Dy25 AA401",
где regex без вычитания ловит unit=Dy (ложноположительный KKS).
"""

import logging
import re

from modules.text_binding.matcher import DiameterMatcher, DiameterMatch
from modules.kks_binding.matcher import KksMatcher, KksMatch
from .result import (
    BlockClassification, ValidationReport,
    BlockType, MatchQuality, ValidationColor,
)

logger = logging.getLogger(__name__)

# Маппинг quality → color
_QUALITY_TO_COLOR = {
    MatchQuality.EXACT: ValidationColor.GREEN,
    MatchQuality.CORRECTED: ValidationColor.YELLOW,
    MatchQuality.NONE: ValidationColor.ORANGE,
}


class OcrBlockClassifier:
    """Классифицирует OCR-блоки: KKS / diameter / annotation / unknown."""

    def __init__(
        self,
        diameter_matcher: DiameterMatcher,
        kks_matcher: KksMatcher,
    ):
        self._diam = diameter_matcher
        self._kks = kks_matcher
        # Для защиты от жадного суффикса диаметра (Dy25AA → суффикс AA = unit)
        self._known_units = kks_matcher._known_units

    def classify_all(self, ocr_blocks: list[dict]) -> ValidationReport:
        """Классифицировать все OCR-блоки, вернуть отчёт."""
        report = ValidationReport(total_blocks=len(ocr_blocks))

        for idx, block in enumerate(ocr_blocks):
            if block.get("merged_into") is not None:
                continue
            cl = self._classify_one(idx, block)
            report.classifications.append(cl)

            # Счётчики
            if cl.block_type == BlockType.KKS:
                if cl.match_quality == MatchQuality.EXACT:
                    report.kks_exact += 1
                else:
                    report.kks_corrected += 1
            elif cl.block_type == BlockType.DIAMETER:
                if cl.match_quality == MatchQuality.EXACT:
                    report.diameter_exact += 1
                else:
                    report.diameter_corrected += 1
            elif cl.color == ValidationColor.ORANGE:
                report.orange_count += 1
            else:
                report.annotation_count += 1

        return report

    def reclassify_one(self, idx: int, block: dict) -> BlockClassification:
        """Переклассифицировать один блок (после редактирования текста)."""
        return self._classify_one(idx, block)

    def _classify_one(self, idx: int, block: dict) -> BlockClassification:
        """Классифицировать один OCR-блок."""
        text = block.get("text", "").strip()
        cl = BlockClassification(
            block_idx=idx,
            block_type=BlockType.UNKNOWN,
            match_quality=MatchQuality.NONE,
            color=ValidationColor.DEFAULT,
            original_text=text,
        )
        if not text:
            return cl

        working = text

        # ─── Шаг 1: Найти и вычесть диаметр ───
        dm = self._diam.match(working)
        if dm:
            # Защита от жадного суффикса: если suffix = known KKS unit (AA, CT, ...),
            # это не суффикс диаметра, а начало KKS. Удаляем только prefix+number.
            strip_suffix = True
            if dm.suffix and dm.suffix.upper() in self._known_units:
                strip_suffix = False
                # Обнулить суффикс — он не часть диаметра
                dm = DiameterMatch(
                    prefix=dm.prefix, diameter=dm.diameter, suffix="",
                    text=f"{dm.prefix}{dm.diameter}",
                    confidence=dm.confidence, pattern_name=dm.pattern_name,
                )
            working = _remove_diameter(working, dm, strip_suffix=strip_suffix)

        # ─── Шаг 2: Найти KKS в оставшемся ───
        km = self._kks.match(working) if working.strip() else None

        # ─── Шаг 3: Определить тип и цвет ───
        if km and dm:
            # Блок содержит и KKS и диаметр
            cl.block_type = BlockType.KKS
            _fill_kks(cl, km)
            _fill_diameter(cl, dm)
            cl.remaining_text = _remove_kks(working, km)

        elif km:
            cl.block_type = BlockType.KKS
            _fill_kks(cl, km)
            cl.remaining_text = _remove_kks(working, km)

        elif dm:
            cl.block_type = BlockType.DIAMETER
            _fill_diameter(cl, dm)
            cl.match_quality = (
                MatchQuality.EXACT if dm.confidence >= 1.0
                else MatchQuality.CORRECTED
            )
            cl.remaining_text = working.strip()

        else:
            # Ничего не нашли
            if _looks_like_code(text):
                cl.color = ValidationColor.ORANGE
            else:
                cl.block_type = BlockType.ANNOTATION
                cl.color = ValidationColor.DEFAULT
            cl.remaining_text = text
            return cl

        # Цвет по match quality
        cl.color = _QUALITY_TO_COLOR.get(cl.match_quality, ValidationColor.ORANGE)
        return cl


# ─── Helpers ──────────────────────────────────────────────────────


def _fill_kks(cl: BlockClassification, km: KksMatch) -> None:
    cl.kks_full = km.full
    cl.kks_block = km.block
    cl.kks_system = km.system
    cl.kks_fn = km.fn
    cl.kks_unit = km.unit
    cl.kks_num = km.num
    cl.kks_suffix = km.suffix
    cl.kks_span = km.span
    cl.corrected_text = km.corrected_text
    cl.match_quality = (
        MatchQuality.EXACT if km.confidence >= 1.0
        else MatchQuality.CORRECTED
    )


def _fill_diameter(cl: BlockClassification, dm: DiameterMatch) -> None:
    cl.diameter_text = dm.text
    cl.diameter_value = dm.diameter
    cl.diameter_prefix = dm.prefix
    cl.diameter_suffix = dm.suffix


def _remove_diameter(text: str, dm: DiameterMatch, strip_suffix: bool = True) -> str:
    """Вычесть диаметр из текста.

    Args:
        strip_suffix: если False, удаляем только prefix+number, не suffix.
            Нужно когда suffix = KKS unit code (AA, CT, ...).
    """
    esc_p = re.escape(dm.prefix)
    pat = esc_p + r"\s*" + str(dm.diameter)
    if strip_suffix and dm.suffix:
        esc_s = re.escape(dm.suffix)
        pat += r"\s*" + esc_s
    result = re.sub(pat, "", text, count=1, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


def _remove_kks(text: str, km: KksMatch) -> str:
    """Вычесть KKS из текста по span."""
    if km.span:
        result = text[:km.span[0]] + text[km.span[1]:]
        return re.sub(r"\s+", " ", result).strip()
    return text


def _looks_like_code(text: str) -> bool:
    """Текст похож на код? (цифры + буквы, длина 6+)."""
    clean = text.replace(" ", "")
    if len(clean) < 6:
        return False
    return bool(
        re.search(r"\d{2,}", clean)
        and re.search(r"[A-Za-zА-Яа-я]{2,}", clean)
    )
