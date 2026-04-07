"""
profiles/isa_s5.py — ISA S5.1 Instrument Tag Profile.

ISA-теги: FIC-101, LT-200A, PDI-3001
Line numbers: 6"-HCA-001, 10"-PW-100

Значительно проще KKS:
  - Нет head/tail сборки (теги самодостаточные)
  - Нет кириллической нормализации
  - Нет multi-code split
  - Нет secondary script processing
"""

import re
from pathlib import Path
from typing import Optional

from modules.ocr.domain_profile import BaseDomainProfile


class ISAProfile(BaseDomainProfile):

    def __init__(self, yaml_path=None, config=None):
        if yaml_path is None and config is None:
            default_yaml = Path(__file__).with_suffix('.yaml')
            if default_yaml.exists():
                yaml_path = str(default_yaml)
        super().__init__(yaml_path=yaml_path, config=config)

        self._full_tag = self.patterns.get('full_tag')
        self._line_num = self.patterns.get('line_number')
        self._prefix = self.patterns.get('prefix')
        self._suffix = self.patterns.get('suffix')

    def classify(self, text: str) -> str:
        text = text.strip()
        if not text:
            return 'OTHER'
        if self._line_num and self._line_num.regex.search(text):
            return 'LINE'
        if self._full_tag and self._full_tag.regex.search(text):
            return 'FULL'
        if self._prefix and self._prefix.regex.match(text):
            return 'HEAD'
        if self._suffix and self._suffix.regex.match(text):
            return 'TAIL'
        return 'OTHER'

    def is_noise(self, text_raw: str) -> bool:
        text = re.sub(r'</?b>', '', text_raw).replace('<br>', ' ').strip()
        if not text:
            return True
        if len(text) == 1 and not text.isalnum():
            return True
        al = sum(1 for c in text if c.isalpha())
        di = sum(1 for c in text if c.isdigit())
        if al == 0 and di == 0:
            return True
        if self.has_any_target_pattern(text):
            return False
        if len(text) <= 2:
            return True
        if self._check_noise_regexes(text):
            return True
        if len(text) > 30 and al > di:
            return True
        if len(text.split()) >= 5:
            return True
        return False

    def is_target(self, text: str) -> bool:
        return (bool(self._full_tag and self._full_tag.regex.search(text))
                or bool(self._line_num and self._line_num.regex.search(text)))

    def has_any_target_pattern(self, text: str) -> bool:
        if self._full_tag and self._full_tag.regex.search(text):
            return True
        if self._line_num and self._line_num.regex.search(text):
            return True
        return False

    def category_role(self, category: str) -> str:
        _ROLE_MAP = {
            'FULL': 'full',
            'LINE': 'standalone',
            'HEAD': 'head',
            'TAIL': 'tail',
        }
        return _ROLE_MAP.get(category, 'other')

    def split_inline(self, text: str) -> list[str]:
        text = re.sub(r'</?[a-z]+>', '', text).strip()
        tokens = []
        if self._full_tag:
            for m in self._full_tag.regex.finditer(text):
                tokens.append((m.start(), m.end(), m.group()))
        if self._line_num:
            for m in self._line_num.regex.finditer(text):
                if not any(t[0] <= m.start() < t[1] for t in tokens):
                    tokens.append((m.start(), m.end(), m.group()))
        tokens.sort(key=lambda t: t[0])
        return ([t[2].strip() for t in tokens]
                if len(tokens) > 1 else [text])
