"""Контракт-тест фикса эмодзи/цифр (регрессия шрифта на Astra).

Проверяет РЕАЛЬНУЮ ui.main._load_bundled_fonts, но без загрузки всего UI
(тяжёлый импорт MainWindow застаблен), offscreen. Значим в Linux/CI-окружении,
где примари приложения — дженерик-алиас 'Sans Serif' (как на Astra); именно там
воспроизводится баг. На Windows-примари (Segoe UI) баг не проявляется.

Что кодирует контракт фикса (вариант E, QFontInfo):
  1) эмодзи-шрифт доступен как fallback  -> эмодзи рисуются, а не «тофу»;
  2) примари — конкретное семейство, не дженерик -> Qt не отдаёт цифры эмодзи;
  3) цифры 0-9 резолвятся базовым шрифтом, не эмодзи (жалоба пользователя).
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextLayout, QImage, QPainter, QColor, QFontDatabase

EMOJI = "Noto Color Emoji"
GENERIC = {"", "sans serif", "sans-serif", "serif", "monospace", "system"}


# застабить тяжёлый ui.windows.main_window, чтобы импортировать только функцию шрифтов
if "ui.windows.main_window" not in sys.modules:
    _stub = types.ModuleType("ui.windows.main_window")
    _stub.MainWindow = object
    sys.modules["ui.windows.main_window"] = _stub


@pytest.fixture(scope="module")
def loaded_app():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    # Плагин offscreen отдаёт системные шрифты только там, где под ним есть
    # бэкенд БД шрифтов (Linux/fontconfig). На Windows БД пуста: после загрузки
    # бандла Noto Color Emoji остаётся ЕДИНСТВЕННЫМ шрифтом процесса, и любой
    # символ — включая цифры — может отрисоваться только им. Контракт
    # «примари конкретный, цифры не у эмодзи» в таком окружении невыполним
    # ни при какой реализации, поэтому не проверяем его, а не ослабляем.
    if not QFontDatabase.families():
        pytest.skip(
            f"Qt-плагин {app.platformName()!r} не отдаёт ни одного системного "
            "шрифта (QFontDatabase.families() пуст) — контракт примари/фолбэка "
            "непроверяем. Значим на Linux/Astra/CI; на Windows запускать так: "
            "QT_QPA_PLATFORM=windows"
        )
    from ui.main import _load_bundled_fonts

    _load_bundled_fonts(app)
    yield app


def _families(app):
    return app.font().families() or [app.font().family()]


def _char_family(app, ch):
    lay = QTextLayout(ch, app.font())
    lay.beginLayout()
    lay.createLine()
    lay.endLayout()
    runs = lay.glyphRuns()
    return runs[0].rawFont().familyName() if runs else "<none>"


def test_emoji_font_available_as_fallback(loaded_app):
    fams = _families(loaded_app)
    assert EMOJI in fams, f"эмодзи-шрифт должен быть в fallback (иначе тофу): {fams!r}"


def test_primary_is_concrete_not_generic(loaded_app):
    primary = _families(loaded_app)[0]
    assert primary.lower() not in GENERIC, f"примари не должен быть дженериком: {primary!r}"
    assert primary != EMOJI


@pytest.mark.parametrize("ch", list("0123456789"))
def test_digits_not_captured_by_emoji(loaded_app, ch):
    fam = _char_family(loaded_app, ch)
    assert fam != EMOJI, f"цифра {ch!r} улетела в эмодзи-шрифт ({fam})"


def test_bundled_font_never_becomes_primary():
    """Гард: эмодзи-шрифт из бандла не должен занять примари-слот.

    Единственная проверка модуля, значимая и без системных шрифтов: там
    зарегистрированный бандл — единственный кандидат, и QFontInfo резолвит
    примари прямо в него. Контракт: либо примари — конкретное чужое семейство,
    либо шрифт приложения не тронут вовсе (лучше тофу, чем поехавшие цифры).
    """
    app = QApplication.instance() or QApplication(sys.argv)
    from ui.main import _load_bundled_fonts

    before = _families(app)
    _load_bundled_fonts(app)
    after = _families(app)
    assert after == before or (after[0] != EMOJI and after[0].lower() not in GENERIC), (
        f"примари уехал в бандл/дженерик: было {before!r}, стало {after!r}"
    )


def test_dump_pngs_for_eyeball(loaded_app):
    """Не ассерт: если задан PROBE_OUT — сохранить рендеры боевой функции для сверки."""
    out = os.environ.get("PROBE_OUT")
    if not out:
        pytest.skip("PROBE_OUT не задан")
    from pathlib import Path

    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    samples = [
        ("real_digits", "0123456789 76 px"),
        ("real_emoji", "\U0001F4BE✅\U0001F4C1\U0001F504\U0001F50D"),
    ]
    for name, text in samples:
        img = QImage(640, 90, QImage.Format_ARGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setFont(loaded_app.font())
        p.setPen(QColor("black"))
        p.drawText(20, 58, text)
        p.end()
        img.save(str(d / f"{name}.png"))
