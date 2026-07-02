"""
Единые кнопки тулбара для всех вкладок-этапов («бусин»).

Один источник текста, формы и размера для Undo/Redo/Save/Confirm — любую
правку внешнего вида делаем здесь, и она применяется на всех вкладках.

Договорённость по расположению: ↶/↷ — слева (у шестерёнки), 💾 + ✅ Подтвердить —
справа. ↷ добавляем только там, где редактор реально поддерживает redo.
"""

from PySide6.QtWidgets import QPushButton

# Компактный вид иконочных кнопок (без раздутого паддинга/минимальной ширины).
_ICON_QSS = (
    "QPushButton {"
    " padding: 2px 6px; min-width: 0px;"
    " border: 1px solid #9a9a9a; border-radius: 4px; background-color: #f7f7f7; }"
    "QPushButton:hover { background-color: #eaeaea; }"
    "QPushButton:pressed { background-color: #d8d8d8; }"
    "QPushButton:disabled { color: #aaa; border-color: #cfcfcf; background-color: #f2f2f2; }"
)

# Единый вид кнопки подтверждения (зелёная, скруглённая).
_CONFIRM_QSS = (
    "QPushButton {"
    " background-color: #4CAF50; color: white; font-weight: bold;"
    " padding: 4px 10px; border-radius: 4px; }"
    "QPushButton:hover { background-color: #45a049; }"
    "QPushButton:disabled { background-color: #9E9E9E; }"
)

_UNDO_SIZE = (30, 26)
_REDO_SIZE = (30, 26)
_SAVE_SIZE = (34, 28)


def make_undo_button(on_click=None, tooltip="Отменить (Ctrl+Z)") -> QPushButton:
    b = QPushButton("↶")
    b.setToolTip(tooltip)
    b.setStyleSheet(_ICON_QSS)
    if on_click is not None:
        b.clicked.connect(on_click)
    return b


def make_redo_button(on_click=None, tooltip="Повторить (Ctrl+Shift+Z)") -> QPushButton:
    b = QPushButton("↷")
    b.setToolTip(tooltip)
    b.setStyleSheet(_ICON_QSS)
    if on_click is not None:
        b.clicked.connect(on_click)
    return b


def make_save_button(on_click=None, tooltip="Сохранить (Ctrl+S)") -> QPushButton:
    b = QPushButton("💾")
    b.setToolTip(tooltip)
    b.setStyleSheet(_ICON_QSS)
    if on_click is not None:
        b.clicked.connect(on_click)
    return b


def make_confirm_button(on_click=None, text="✅ Подтвердить",
                        tooltip=None) -> QPushButton:
    b = QPushButton(text)
    if tooltip:
        b.setToolTip(tooltip)
    b.setStyleSheet(_CONFIRM_QSS)
    if on_click is not None:
        b.clicked.connect(on_click)
    return b
