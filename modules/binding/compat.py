"""
modules/binding/compat.py — Мост между legacy API и новым binding.

Позволяет legacy-коду (UI, ocr_validation) работать через новый UnifiedMatcher,
сохраняя старые имена классов и форматы данных.

Используется шим-файлами в kks_binding/ и text_binding/.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

from .config import (
    DomainBindingConfig, GlobalOcrCorrections,
    CodeTypeConfig, CodeTypeBindingRule, MatchPattern, CodeTypeField,
)
from .matcher import UnifiedMatcher
from modules.ocr.domain_profile import CodeMatch, DiamMatch

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# KksMatch — legacy-совместимый результат (обёртка над CodeMatch)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class KksMatch:
    """Результат распознавания KKS (legacy-совместимый)."""
    full: str
    block: str
    system: str
    fn: str
    unit: str
    num: str
    suffix: str
    confidence: float
    pattern_name: str
    span: tuple[int, int]
    corrected_text: str = ""

    @classmethod
    def from_code_match(cls, cm: CodeMatch, pattern_name: str = "") -> "KksMatch":
        return cls(
            full=cm.full,
            block=cm.block,
            system=cm.system,
            fn=cm.fn,
            unit=cm.unit,
            num=cm.num,
            suffix=cm.suffix,
            confidence=cm.confidence,
            pattern_name=pattern_name or cm.code_type,
            span=cm.span,
            corrected_text=cm.corrected_text,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# DiameterMatch — legacy-совместимый результат (обёртка над DiamMatch)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DiameterMatch:
    """Результат распознавания диаметра (legacy-совместимый)."""
    prefix: str
    diameter: int
    suffix: str
    text: str
    confidence: float
    pattern_name: str = ""

    @classmethod
    def from_diam_match(cls, dm: DiamMatch, pattern_name: str = "") -> "DiameterMatch":
        return cls(
            prefix=dm.prefix,
            diameter=dm.diameter,
            suffix=dm.suffix,
            text=dm.text,
            confidence=dm.confidence,
            pattern_name=pattern_name,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# KksMatcher — legacy-совместимая обёртка над UnifiedMatcher
# ═══════════════════════════════════════════════════════════════════════════════

class KksMatcher:
    """Legacy-совместимый KKS matcher. Делегирует в UnifiedMatcher."""

    def __init__(self, config):
        """
        Принимает:
          - KksConfig (legacy) — конвертирует в DomainBindingConfig
          - DomainBindingConfig (new) — использует напрямую
        """
        if isinstance(config, DomainBindingConfig):
            self._binding_config = config
        else:
            self._binding_config = _kks_config_to_binding(config)

        self._matcher = UnifiedMatcher(self._binding_config)
        self._known_units = set(self._binding_config.unit_to_classes.keys())

    def match(self, text: str) -> Optional[KksMatch]:
        cm = self._matcher.match_equipment(text)
        if cm is None:
            return None
        return KksMatch.from_code_match(cm)


# ═══════════════════════════════════════════════════════════════════════════════
# DiameterMatcher — legacy-совместимая обёртка над UnifiedMatcher
# ═══════════════════════════════════════════════════════════════════════════════

class DiameterMatcher:
    """Legacy-совместимый diameter matcher. Делегирует в UnifiedMatcher."""

    def __init__(self, config):
        """
        Принимает:
          - DiameterConfig (legacy) — конвертирует в DomainBindingConfig
          - DomainBindingConfig (new) — использует напрямую
        """
        if isinstance(config, DomainBindingConfig):
            self._binding_config = config
        else:
            self._binding_config = _diameter_config_to_binding(config)

        self._matcher = UnifiedMatcher(self._binding_config)

    def match(self, text: str) -> Optional[DiameterMatch]:
        dm = self._matcher.match_diameter(text)
        if dm is None:
            return None
        return DiameterMatch.from_diam_match(dm)


# ═══════════════════════════════════════════════════════════════════════════════
# Конвертеры: legacy config → DomainBindingConfig
# ═══════════════════════════════════════════════════════════════════════════════

def _kks_config_to_binding(kks_cfg) -> DomainBindingConfig:
    """KksConfig → DomainBindingConfig для UnifiedMatcher."""

    # Скомпилировать match_patterns из KksConfig.patterns
    match_patterns = []
    for p in kks_cfg.patterns:
        flags = re.IGNORECASE if "IGNORECASE" in p.flags.upper() else 0
        try:
            match_patterns.append(MatchPattern(
                name=p.name,
                regex=re.compile(p.regex, flags),
            ))
        except re.error as e:
            logger.error("Bad KKS regex '%s': %s", p.regex, e)

    equipment_ct = CodeTypeConfig(
        name="equipment_code",
        binding=CodeTypeBindingRule(target="node", max_distance=200),
        match_patterns=match_patterns,
    )

    cfg = DomainBindingConfig(
        code_types={"equipment_code": equipment_ct},
        ocr_corrections=GlobalOcrCorrections(
            cyr_to_lat=kks_cfg.ocr_corrections_cyr,
            digit_corrections=kks_cfg.ocr_corrections_digit,
            symbol_fixes={
                "(": "C", "[": "C", "{": "C",
                ")": "D", "]": "J", "|": "I",
            },
            unit_blacklist=["DY", "DV", "DN", "DН"],
            known_blocks=list(kks_cfg.known_blocks),
        ),
        unit_to_classes={u: [] for u in kks_cfg.known_units},
        edge_binding_units=kks_cfg.edge_binding_units,
    )
    return cfg


def _diameter_config_to_binding(diam_cfg) -> DomainBindingConfig:
    """DiameterConfig → DomainBindingConfig для UnifiedMatcher."""
    from modules.binding.config import OcrCorrectionRule

    match_patterns = []
    for p in diam_cfg.patterns:
        flags = re.IGNORECASE if "IGNORECASE" in p.flags.upper() else 0
        try:
            match_patterns.append(MatchPattern(
                name=p.name,
                regex=re.compile(p.regex, flags),
            ))
        except re.error as e:
            logger.error("Bad diameter regex '%s': %s", p.regex, e)

    ocr_corrections = []
    for c in diam_cfg.ocr_corrections:
        try:
            ocr_corrections.append(OcrCorrectionRule(
                pattern=re.compile(c.pattern),
                replace=c.replace,
            ))
        except re.error as e:
            logger.error("Bad OCR correction: %s", e)

    diameter_ct = CodeTypeConfig(
        name="diameter",
        binding=CodeTypeBindingRule(target="edge", max_distance=diam_cfg.max_distance),
        match_patterns=match_patterns,
        ocr_corrections=ocr_corrections,
        valid_values=diam_cfg.valid_diameters or [],
    )

    cfg = DomainBindingConfig(
        code_types={"diameter": diameter_ct},
    )
    return cfg
