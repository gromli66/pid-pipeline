"""
modules/binding/validator.py — Валидация привязки unit↔class + reclassify.

Заменяет: kks_binding/binder.py._check_unit_class() + class_to_kks_config.yaml.
Читает class_rules из domain_profile.yaml → binding секция.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from .config import DomainBindingConfig, ClassBindingRule

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Результат валидации привязки."""
    is_valid: bool
    message: str = ""
    reclassify_to: Optional[str] = None


class BindingValidator:
    """Валидация: допустим ли unit для class_name?"""

    def __init__(self, config: DomainBindingConfig):
        self._config = config

    def validate(self, unit: str, class_name: str) -> ValidationResult:
        """
        Проверить допустимость unit для class_name.

        Returns:
            ValidationResult с is_valid, message, reclassify_to
        """
        if not unit or not class_name:
            return ValidationResult(is_valid=True)

        rule = self._config.get_class_rule(class_name)
        if not rule:
            return ValidationResult(is_valid=True)

        # unknow → reclassify
        if rule.kks_target == "reclassify":
            new_class = rule.reclassify_rules.get(unit)
            if new_class:
                return ValidationResult(
                    is_valid=True,
                    message=f"{class_name} → {new_class}",
                    reclassify_to=new_class,
                )
            return ValidationResult(is_valid=True)

        # none → не участвует в привязке
        if rule.kks_target == "none":
            return ValidationResult(
                is_valid=False,
                message=f"{class_name} не участвует в KKS-привязке",
            )

        # Обычная проверка
        if unit in rule.expected_units:
            return ValidationResult(is_valid=True)

        allowed = self._config.get_expected_classes(unit)
        return ValidationResult(
            is_valid=False,
            message=f"Unit {unit} не ожидается для {class_name} "
                    f"(допустимые классы: {allowed})",
        )

    def get_bindable_classes(self, graph_nodes: list[dict]) -> list[dict]:
        """Фильтр: nodes пригодные для привязки."""
        result = []
        for node in graph_nodes:
            if node.get("type") == "connector":
                continue
            cls_name = node.get("class_name", "")
            rule = self._config.get_class_rule(cls_name)
            if rule and rule.kks_target == "none":
                continue
            result.append(node)
        return result

    def get_expected_classes_for_unit(self, unit: str) -> list[str]:
        """Допустимые классы для данного unit code."""
        return self._config.get_expected_classes(unit)
