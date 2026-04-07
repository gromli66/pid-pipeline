"""
modules/ocr/noise_engine.py — Generic noise filter engine.

Выполняет noise.rules + noise.thresholds из domain_profile.yaml.
Заменяет магические числа в KKSProfile.is_noise() и PidCyrillicProfile.is_noise().
"""

import re
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class NoiseEngine:
    """
    Универсальный фильтр шума.

    Порядок проверок:
    1. Пустой текст → noise
    2. math-тег → noise
    3. Один неалфанумерик → noise
    4. Нет букв и цифр → noise
    5. Защита: has_target → не noise
    6. Короткий без target → noise
    7. Noise regex из YAML → noise (если нет target)
    8. Длинный текст без target → noise
    9. Много слов без target → noise
    10. Много спецсимволов в коротком → noise
    """

    def __init__(
        self,
        noise_regexes: list[tuple[str, re.Pattern]],
        thresholds: dict,
        has_target_fn: Callable[[str], bool],
    ):
        """
        Args:
            noise_regexes: [(name, compiled_regex), ...]
            thresholds: noise.thresholds из YAML
            has_target_fn: функция has_any_target_pattern из профиля
        """
        self._regexes = noise_regexes
        self._has_target = has_target_fn

        # Пороги (с дефолтами совместимыми с текущей логикой)
        self._min_len_no_target = thresholds.get("min_length_without_target", 2)
        self._max_len_text_only = thresholds.get("max_length_text_only", 25)
        self._max_words_no_target = thresholds.get("max_word_count_no_target", 4)
        self._max_spec_ratio = thresholds.get("max_special_char_ratio_short", 0.5)
        self._short_max_len = thresholds.get("short_text_max_length", 5)

    def is_noise(self, text_raw: str) -> bool:
        """Является ли текст шумом?"""
        text = re.sub(r'</?b>', '', text_raw).replace('<br>', ' ').strip()

        # 1. Пустой
        if not text:
            return True

        # 2. math-тег
        if '<math>' in text_raw:
            return True

        # 3. Один символ не алфанумерик
        if len(text) == 1 and not text[0].isalnum():
            return True

        # 4. Нет букв и цифр
        al = sum(1 for c in text if c.isalpha())
        di = sum(1 for c in text if c.isdigit())
        if al == 0 and di == 0:
            return True

        # 5-6. Защита target / короткий без target
        has_target = self._has_target(text)
        if len(text) <= self._min_len_no_target and not has_target:
            return True

        # 7. Noise regex из YAML
        if self._check_regexes(text):
            return not has_target

        # 8. Длинный нецелевой текст (больше букв чем цифр)
        if len(text) > self._max_len_text_only and not has_target and al > di:
            return True

        # 9. Много слов без target
        if len(text.split()) >= self._max_words_no_target and not has_target:
            return True

        # 10. Много спецсимволов в коротком тексте
        spec = sum(1 for c in text if not c.isalnum() and c != ' ')
        if (len(text) <= self._short_max_len
                and spec > len(text) * self._max_spec_ratio
                and not has_target):
            return True

        return False

    def _check_regexes(self, text: str) -> bool:
        """Проверить noise-regex из YAML."""
        for name, rx in self._regexes:
            if rx.search(text):
                return True
        return False
