"""
modules/ocr/classify_engine.py — Generic classify engine.

Выполняет classify.rules из domain_profile.yaml сверху вниз.
Заменяет Python if/elif цепочки в KKSProfile.classify() и PidCyrillicProfile.classify().

Поддерживает:
  - pattern: "name" — один паттерн
  - patterns: ["a", "b"] — несколько паттернов (combined count без overlap)
  - condition: matches | match_only | match_count >= N | total_count >= N
  - exclude_pattern: "name" — исключить если тоже match
  - sub_rules: [...] — вложенные правила
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from modules.ocr.domain_profile import CompiledPattern

logger = logging.getLogger(__name__)


@dataclass
class ClassifyRule:
    """Одно правило классификации."""
    pattern: str = ""                  # Имя паттерна из patterns
    patterns: list[str] = field(default_factory=list)  # Несколько паттернов (combined)
    condition: str = "matches"         # "matches" | "match_only" | "match_count >= N" | "total_count >= N"
    condition_count: int = 0           # N для count conditions
    exclude_pattern: str = ""          # Исключить если этот паттерн тоже match
    result: str = ""                   # Категория (FULL, HEAD, ...) — если нет sub_rules
    sub_rules: list = field(default_factory=list)  # Вложенные правила


class ClassifyEngine:
    """
    Универсальный classify-движок.

    Выполняет правила из YAML, используя скомпилированные паттерны.
    """

    def __init__(
        self,
        rules_config: list[dict],
        patterns: dict[str, CompiledPattern],
        roles_config: dict[str, list[str]],
    ):
        self._patterns = patterns
        self._rules = self._parse_rules(rules_config)
        self._category_to_role: dict[str, str] = {}
        for role, categories in roles_config.items():
            for cat in categories:
                self._category_to_role[cat] = role

    def classify(self, text: str) -> str:
        """Классифицировать текст по правилам."""
        text = text.strip()
        if not text:
            return "OTHER"
        return self._eval_rules(self._rules, text)

    def category_role(self, category: str) -> str:
        """Категория → семантическая роль."""
        return self._category_to_role.get(category, "other")

    # ── Internal ─────────────────────────────────

    def _eval_rules(self, rules: list[ClassifyRule], text: str) -> str:
        for rule in rules:
            result = self._eval_rule(rule, text)
            if result is not None:
                return result
        return "OTHER"

    def _eval_rule(self, rule: ClassifyRule, text: str) -> Optional[str]:
        # Default rule (без pattern/patterns)
        if not rule.pattern and not rule.patterns:
            if rule.sub_rules:
                return self._eval_rules(rule.sub_rules, text)
            return rule.result if rule.result else None

        # Exclude check
        if rule.exclude_pattern:
            excl = self._patterns.get(rule.exclude_pattern)
            if excl and excl.regex.match(text.strip()):
                return None

        # Multi-pattern (combined count)
        if rule.patterns:
            matched = self._check_multi_condition(rule.patterns, rule.condition,
                                                   rule.condition_count, text)
        else:
            pattern = self._patterns.get(rule.pattern)
            if not pattern:
                return None
            matched = self._check_condition(pattern, rule.condition,
                                            rule.condition_count, text)

        if not matched:
            return None

        if rule.sub_rules:
            return self._eval_rules(rule.sub_rules, text)
        return rule.result if rule.result else None

    def _check_condition(self, pattern: CompiledPattern, condition: str,
                         count: int, text: str) -> bool:
        if condition == "matches":
            return bool(pattern.regex.search(text))
        if condition == "match_only":
            return bool(pattern.regex.match(text.strip()))
        if condition == "match_count":
            return len(pattern.regex.findall(text)) >= count
        return bool(pattern.regex.search(text))

    def _check_multi_condition(self, pattern_names: list[str], condition: str,
                               count: int, text: str) -> bool:
        """
        Combined count: суммирует matches нескольких паттернов без overlap.

        Первый паттерн — полный findall.
        Каждый следующий — только matches не перекрывающиеся с уже найденными.
        """
        if condition == "total_count":
            total, _ = self._count_non_overlapping(pattern_names, text)
            return total >= count
        if condition == "matches":
            # Любой из паттернов матчит
            for pn in pattern_names:
                p = self._patterns.get(pn)
                if p and p.regex.search(text):
                    return True
            return False
        return False

    def _count_non_overlapping(self, pattern_names: list[str],
                                text: str) -> tuple[int, list[tuple[int, int]]]:
        """Посчитать non-overlapping matches от нескольких паттернов."""
        occupied: list[tuple[int, int]] = []  # (start, end)
        total = 0

        for pn in pattern_names:
            p = self._patterns.get(pn)
            if not p:
                continue
            for m in p.regex.finditer(text):
                start, end = m.start(), m.end()
                overlaps = any(s <= start < e for s, e in occupied)
                if not overlaps:
                    occupied.append((start, end))
                    total += 1

        return total, occupied

    def _parse_rules(self, config: list[dict]) -> list[ClassifyRule]:
        rules = []
        for item in config:
            rule = ClassifyRule(
                pattern=item.get("pattern", ""),
                patterns=item.get("patterns", []),
                condition=item.get("condition", "matches"),
                exclude_pattern=item.get("exclude_pattern", ""),
                result=item.get("result", ""),
            )

            # Parse condition count: "match_count >= 2" or "total_count >= 2"
            for prefix in ("match_count", "total_count"):
                if prefix in rule.condition:
                    parts = rule.condition.split(">=")
                    if len(parts) == 2:
                        try:
                            rule.condition_count = int(parts[1].strip())
                        except ValueError:
                            pass
                    rule.condition = prefix
                    break

            if "sub_rules" in item:
                rule.sub_rules = self._parse_rules(item["sub_rules"])

            rules.append(rule)
        return rules
