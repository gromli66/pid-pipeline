"""
modules/binding/matcher.py — Universal code/diameter matcher.

Заменяет: kks_binding/matcher.py (KksMatcher) + text_binding/matcher.py (DiameterMatcher).
Driven by code_types из domain_profile.yaml.

Цепочка: raw → regex → OCR corrections → regex → compact → regex.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

from modules.ocr.domain_profile import CodeMatch, DiamMatch
from .config import DomainBindingConfig, CodeTypeConfig

logger = logging.getLogger(__name__)


class UnifiedMatcher:
    """
    Универсальный matcher для всех типов кодов.

    Пробует все code_types из конфига, возвращает первый match.
    Применяет OCR-коррекции (кириллица→латиница, digit corrections).
    """

    def __init__(self, config: DomainBindingConfig):
        self._config = config
        self._cyr_map = self._build_cyr_map(config.ocr_corrections.cyr_to_lat,
                                             config.ocr_corrections.symbol_fixes)
        self._digit_map = self._build_digit_map(config.ocr_corrections.digit_corrections)
        self._known_blocks = config.ocr_corrections.known_blocks
        self._unit_blacklist = set(config.ocr_corrections.unit_blacklist)
        self._known_units = set(config.unit_to_classes.keys())

    # ── Public API ───────────────────────────────

    def match_equipment(self, text: str) -> Optional[CodeMatch]:
        """Распознать код оборудования в тексте."""
        ct = self._config.code_types.get("equipment_code")
        if not ct:
            return None
        return self._match_code_type(text, ct)

    def match_diameter(self, text: str) -> Optional[DiamMatch]:
        """Распознать диаметр в тексте."""
        ct = self._config.code_types.get("diameter")
        if not ct:
            return None
        return self._match_diameter_type(text, ct)

    def match_any(self, text: str) -> Optional[CodeMatch | DiamMatch]:
        """Пробует все code_types, возвращает первый match."""
        text = text.strip()
        if not text:
            return None

        # Equipment first, then diameter
        eq = self.match_equipment(text)
        if eq:
            return eq
        dm = self.match_diameter(text)
        if dm:
            return dm
        return None

    # ── Equipment matching ───────────────────────

    def _match_code_type(self, text: str, ct: CodeTypeConfig) -> Optional[CodeMatch]:
        text = text.strip()
        if not text:
            return None

        # 1. Raw
        m = self._try_patterns(text, ct, confidence=1.0)
        if m:
            return m

        # 2. Cyrillic corrections
        corrected = self._apply_cyr_corrections(text)
        if corrected != text:
            m = self._try_patterns(corrected, ct, confidence=0.9)
            if m:
                m.corrected_text = corrected
                return m

        # 3. Digit corrections
        digit_corrected = self._apply_digit_corrections(text)
        if digit_corrected != text:
            m = self._try_patterns(digit_corrected, ct, confidence=0.85)
            if m:
                m.corrected_text = digit_corrected
                return m

        # 4. Both corrections
        both = self._apply_digit_corrections(corrected)
        if both != corrected and both != digit_corrected:
            m = self._try_patterns(both, ct, confidence=0.8)
            if m:
                m.corrected_text = both
                return m

        # 5. Compact (remove spaces)
        compact = text.replace(" ", "")
        if compact != text:
            m = self._try_patterns(compact, ct, confidence=0.85)
            if m:
                return m
            corrected_compact = self._apply_cyr_corrections(compact)
            if corrected_compact != compact:
                m = self._try_patterns(corrected_compact, ct, confidence=0.8)
                if m:
                    m.corrected_text = corrected_compact
                    return m

        return None

    def _try_patterns(self, text: str, ct: CodeTypeConfig,
                      confidence: float) -> Optional[CodeMatch]:
        """Пробует все match_patterns для code_type."""
        for mp in ct.match_patterns:
            for m in mp.regex.finditer(text):
                result = self._extract_code_match(m, mp.name, ct, confidence)
                if result is not None:
                    return result
        return None

    def _extract_code_match(self, m: re.Match, pattern_name: str,
                            ct: CodeTypeConfig,
                            confidence: float) -> Optional[CodeMatch]:
        """Извлечь CodeMatch из regex match."""
        try:
            # Извлечь все named groups из regex match
            groups = {k: (v or "") for k, v in m.groupdict().items() if v is not None}

            block = self._fix_digits(groups.get('block', ''))
            system = self._fix_letters(groups.get('s', groups.get('system', ''))).upper()
            fn = self._fix_digits(groups.get('fn', ''))
            unit = self._fix_letters(groups.get('unit', '')).upper()
            num = self._fix_digits(groups.get('num', ''))
            suffix = self._fix_letters(groups.get('suffix', '') or '').upper()

            # Коррекция блока: "0"→первый known
            if block in ("0", "00") and self._known_blocks:
                block = self._known_blocks[0]

            # Blacklist (диаметры)
            if unit in self._unit_blacklist:
                return None

            full = f"{block}{system}{fn}{unit}{num}{suffix}"

            return CodeMatch(
                full=full,
                block=block,
                system=system,
                fn=fn,
                unit=unit,
                unit_num=num,
                num=num,
                suffix=suffix,
                confidence=confidence,
                span=(m.start(), m.end()),
                code_type=ct.name,
            )

        except (IndexError, re.error):
            return None

    # ── Diameter matching ────────────────────────

    def _match_diameter_type(self, text: str, ct: CodeTypeConfig) -> Optional[DiamMatch]:
        text = text.strip()
        if not text:
            return None

        # 1. Raw
        m = self._try_diameter_patterns(text, ct)
        if m:
            m.confidence = 1.0
            return m

        # 2. OCR corrections (code_type level)
        corrected = text
        for oc in ct.ocr_corrections:
            corrected = oc.pattern.sub(oc.replace, corrected)
        if corrected != text:
            m = self._try_diameter_patterns(corrected, ct)
            if m:
                m.confidence = 0.9
                return m

        # 3. Compact
        compact = text.replace(" ", "")
        if compact != text:
            m = self._try_diameter_patterns(compact, ct)
            if m:
                m.confidence = 0.85
                return m

        return None

    def _try_diameter_patterns(self, text: str, ct: CodeTypeConfig) -> Optional[DiamMatch]:
        for mp in ct.match_patterns:
            m = mp.regex.search(text)
            if m:
                try:
                    prefix = m.group("prefix")
                    diameter = int(m.group("diameter"))
                    suffix = (m.group("suffix") or "").strip()
                except (IndexError, ValueError):
                    continue
                if ct.valid_values and diameter not in ct.valid_values:
                    continue
                return DiamMatch(
                    prefix=prefix,
                    diameter=diameter,
                    suffix=suffix,
                    text=f"{prefix}{diameter}{suffix}".strip(),
                )
        return None

    # ── OCR corrections ──────────────────────────

    def _apply_cyr_corrections(self, text: str) -> str:
        return text.translate(self._cyr_map)

    def _apply_digit_corrections(self, text: str) -> str:
        if not text:
            return text
        result = list(text)
        for i, ch in enumerate(result):
            if ch in self._digit_map:
                has_digit_neighbor = False
                if i > 0 and result[i-1].isdigit():
                    has_digit_neighbor = True
                if i < len(result) - 1 and result[i+1].isdigit():
                    has_digit_neighbor = True
                if has_digit_neighbor:
                    result[i] = self._digit_map[ch]
        return "".join(result)

    def _fix_digits(self, s: str) -> str:
        return "".join(self._digit_map.get(ch, ch) for ch in s)

    def _fix_letters(self, s: str) -> str:
        return s.translate(self._cyr_map)

    @staticmethod
    def _build_cyr_map(cyr_to_lat: dict, symbol_fixes: dict) -> dict:
        combined = dict(cyr_to_lat)
        combined.update(symbol_fixes)
        return str.maketrans(combined)

    @staticmethod
    def _build_digit_map(digit_corrections: dict) -> dict:
        dm = dict(digit_corrections)
        dm.update({"Z": "2", "S": "5", "B": "8", "z": "2", "s": "5"})
        dm.pop("L", None)
        dm.pop("l", None)
        return dm
