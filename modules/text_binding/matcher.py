"""
DEPRECATED — используйте modules.binding.matcher.UnifiedMatcher напрямую.

Этот файл — шим для обратной совместимости.
DiameterMatcher делегирует в UnifiedMatcher через modules.binding.compat.
"""

from modules.binding.compat import DiameterMatch, DiameterMatcher  # noqa: F401

__all__ = ["DiameterMatch", "DiameterMatcher"]
