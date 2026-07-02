"""
ResponsiveToolbar — тулбар в одну строку с авто-подгонкой под ширину окна.

Кнопки не переносятся на второй ряд. Чтобы весь ряд помещался на любом экране,
подгоняются ТРИ вещи (в порядке влияния на ширину):
  1) паддинг/минимальная ширина кнопок — один раз убираются «раздутые» отступы,
     чтобы кнопки жались к тексту (иначе внутри кнопок пустое место);
  2) промежутки между кнопками (в т.ч. внутри панелей состояний) — сжимаются
     пропорционально нехватке места;
  3) кегль шрифта — ужимается в последнюю очередь, до нижнего порога.
При расширении окна всё возвращается к базовым значениям.

Переиспользуемо на всём десктопе:

    from ui.widgets.responsive_toolbar import install_responsive_toolbar
    install_responsive_toolbar(toolbar_container)

`toolbar_container` — QWidget, внутри которого лежит горизонтальный layout кнопок.
"""

from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import (
    QWidget, QAbstractButton, QLabel, QAbstractSpinBox, QHBoxLayout, QLayout,
)

# Базовые значения при достатке места.
_BASE_SPACING = 6
# Компактный паддинг/минимальная ширина кнопок (снимает «пустое место» внутри).
_COMPACT_QSS = (
    "QPushButton { padding: 2px 6px; min-width: 0px;"
    " border: 1px solid #9a9a9a; border-radius: 3px; background-color: #f7f7f7; }"
    "QPushButton:hover { background-color: #eaeaea; }\n"
)


class ResponsiveToolbar(QObject):
    """Следит за resize контейнера и подгоняет паддинг/промежутки/шрифт под ширину."""

    def __init__(self, container: QWidget, min_pt: float = 7.0):
        super().__init__(container)
        self._c = container
        self._min_pt = float(min_pt)
        self._base_pt: float | None = None
        self._compacted = False
        self._applying = False
        container.installEventFilter(self)

    # -- helpers --
    def _children(self):
        # PySide6 (эта версия) не принимает кортеж типов в findChildren —
        # берём все QWidget и фильтруем isinstance'ом.
        types = (QAbstractButton, QLabel, QAbstractSpinBox)
        return [
            w for w in self._c.findChildren(QWidget)
            if isinstance(w, types) and w.isVisible()
        ]

    def _box_layouts(self):
        """Главный ряд + горизонтальные layout'ы панелей состояний."""
        result = []
        lay = self._c.layout()
        if lay is not None:
            result.append(lay)
        result += self._c.findChildren(QHBoxLayout)
        return result

    def _margins_width(self) -> int:
        lay = self._c.layout()
        if lay is None:
            return 0
        m = lay.contentsMargins()
        return m.left() + m.right()

    def _compact_buttons_once(self):
        """Один раз убрать раздутый паддинг и минимальную ширину кнопок.

        Кнопки со своим паддингом (например «✅ Подтвердить») не трогаем.
        """
        if self._compacted:
            return
        for w in self._c.findChildren(QAbstractButton):
            ss = w.styleSheet() or ""
            if "padding" in ss:
                continue
            w.setStyleSheet(_COMPACT_QSS + ss)
        self._compacted = True

    def _set_spacing(self, value: int):
        for lay in self._box_layouts():
            try:
                lay.setSpacing(value)
            except Exception:
                pass

    def _apply(self):
        if self._applying:
            return
        ch = self._children()
        if not ch:
            return
        if self._base_pt is None:
            pt = self._c.font().pointSizeF()
            self._base_pt = pt if pt and pt > 0 else 9.0

        self._applying = True
        try:
            self._compact_buttons_once()

            # 1) сброс к базовым значениям — честный замер потребной ширины
            for w in ch:
                f = w.font()
                f.setPointSizeF(self._base_pt)
                w.setFont(f)
            self._set_spacing(_BASE_SPACING)

            needed = sum(max(w.sizeHint().width(), 1) for w in ch)
            needed += _BASE_SPACING * max(0, len(ch) - 1) + self._margins_width()
            avail = self._c.width()
            if needed <= 0 or avail <= 0:
                return

            ratio = min(1.0, avail / needed)

            # 2) сжать промежутки пропорционально
            self._set_spacing(max(1, int(round(_BASE_SPACING * ratio))))

            # 3) сжать шрифт (до нижнего порога)
            pt = max(self._min_pt, self._base_pt * ratio)
            for w in ch:
                f = w.font()
                f.setPointSizeF(pt)
                w.setFont(f)
        finally:
            self._applying = False

    # -- event filter --
    def eventFilter(self, obj, ev):
        if obj is self._c and ev.type() in (
            QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest,
        ):
            self._apply()
        return False


def install_responsive_toolbar(container: QWidget, min_pt: float = 7.0) -> ResponsiveToolbar:
    """Повесить авто-подгонку на контейнер тулбара. Возвращает контроллер."""
    return ResponsiveToolbar(container, min_pt=min_pt)
