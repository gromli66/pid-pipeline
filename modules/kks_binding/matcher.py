"""
DEPRECATED — используйте modules.binding.matcher.UnifiedMatcher напрямую.

Этот файл — шим для обратной совместимости.
KksMatcher делегирует в UnifiedMatcher через modules.binding.compat.
"""

from modules.binding.compat import KksMatch, KksMatcher  # noqa: F401

__all__ = ["KksMatch", "KksMatcher"]
