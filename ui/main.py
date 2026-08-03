"""
P&ID Pipeline - Desktop UI Application.

Точка входа для PySide6 приложения.
"""

import sys
import os
import logging
from pathlib import Path

# Windows-консоль обычно cp1251: '→'/emoji в логах роняли StreamHandler
# (UnicodeEncodeError) и строка терялась. UTF-8 + errors=replace — не падаем.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)

# Добавляем корень проекта в path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader, QFontDatabase, QFontInfo

from ui.windows.main_window import MainWindow

# Увеличить лимит загрузки изображений (по умолчанию 256 МБ, нужно для P&ID ~15000x7000)
QImageReader.setAllocationLimit(1024)  # 1 ГБ

# Дженерик-алиасы: не семейства, а «подбери сам». В примари-слоте рядом с
# эмодзи-шрифтом Qt отдаёт ему общие кодпоинты (цифры, ⚠, ⚙) — см. _load_bundled_fonts.
_GENERIC_FAMILIES = {"sans serif", "sans-serif", "serif", "monospace", "system"}


def _load_bundled_fonts(app):
    """Подключить шрифты из ui/resources/fonts как fallback шрифта приложения.

    На Astra нет системного эмодзи-шрифта — эмодзи в кнопках/статусах
    (💾 ✅ 📁 🔄 …) рисуются «тофу»-квадратами. Бандлим Noto Color Emoji (OFL)
    и добавляем зарегистрированные семейства в конец fallback-списка.
    Путь от __file__ работает и в dev, и во frozen-сборке: спека кладёт
    ui/resources в _MEIPASS с сохранением пути, а __file__ модуля ui.main
    во frozen указывает туда же (плюс client_main делает chdir в _MEIPASS,
    так что даже относительный __file__ разрешится верно).
    Нет папки/шрифтов — тихий no-op (Windows и так рендерит через Segoe UI Emoji).
    """
    fonts_dir = Path(__file__).resolve().parent / "resources" / "fonts"
    if not fonts_dir.is_dir():
        return
    f = app.font()
    # Резолвим примари ДО регистрации бандла: после addApplicationFont эмодзи-шрифт
    # тоже кандидат, и в бедной БД шрифтов QFontInfo выберет именно его (в пустой БД —
    # он единственный) → эмодзи станет примари, цифры уедут туда же.
    concrete = QFontInfo(f).family()
    families = []
    for fp in sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf")):
        font_id = QFontDatabase.addApplicationFont(str(fp))
        if font_id == -1:
            logging.getLogger(__name__).warning("Не удалось загрузить шрифт: %s", fp.name)
            continue
        for fam in QFontDatabase.applicationFontFamilies(font_id):
            if fam not in families:
                families.append(fam)
    if not families:
        return
    # В примари нужно КОНКРЕТНОЕ семейство, а не дженерик-алиас (на Linux/Astra
    # f.family() == 'Sans Serif'): при дженерик-примари Qt отдаёт общие кодпоинты
    # (цифры 0-9, ⚠, ⚙) эмодзи-шрифту → цифры «плыли». QFontInfo выше резолвит в
    # реальный шрифт (Linux→DejaVu Sans, Windows→Segoe UI), эмодзи остаются fallback.
    # Если конкретного семейства нет (пустая БД шрифтов — так ведёт себя Qt-плагин
    # offscreen на Windows: ни одного системного шрифта, QFontInfo даёт ''), то
    # примари всё равно достался бы эмодзи-шрифту — ровно тот баг, который лечим.
    # Тогда шрифт приложения не трогаем: эмодзи-фолбэка не будет, но цифры целы.
    if not concrete or concrete.lower() in _GENERIC_FAMILIES or concrete in families:
        logging.getLogger(__name__).warning(
            "Примари-шрифт не резолвится в конкретное семейство (%r) — "
            "fallback-шрифты не подключены: %s",
            concrete,
            ", ".join(families),
        )
        return
    f.setFamilies([concrete, *families])
    app.setFont(f)
    logging.getLogger(__name__).info(
        "Fallback-шрифты подключены (примари %s): %s", concrete, ", ".join(families)
    )


def main():
    """Запуск приложения."""
    # Высокое DPI
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # Общий GL-контекст для QWebEngineView (CVAT) — безопасно на всех ОС,
    # рекомендация Qt. Ставится всегда.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    # GL-бэкенд рендеринга. Дефолт gles (ANGLE): аппаратное ускорение без
    # мигания оверлея CVAT на этой машине (на desktop OpenGL боксы мерцали).
    # Переключается БЕЗ правки кода через переменную окружения PID_GL_BACKEND:
    #   gles     — ANGLE, аппаратное ускорение (дефолт, без мигания)
    #   desktop  — нативный OpenGL (быстро, но на части GPU мигает оверлей)
    #   software — софт-рендер, универсально но медленно
    #   auto     — дефолт ОС (на Windows может вернуть мигание)
    _gl = os.environ.get("PID_GL_BACKEND", "gles").lower()
    _gl_attr = {
        "desktop": Qt.ApplicationAttribute.AA_UseDesktopOpenGL,
        "gles": Qt.ApplicationAttribute.AA_UseOpenGLES,
        "software": Qt.ApplicationAttribute.AA_UseSoftwareOpenGL,
    }.get(_gl)
    if _gl_attr is not None:
        QApplication.setAttribute(_gl_attr)

    app = QApplication(sys.argv)
    app.setApplicationName("P&ID Pipeline")
    app.setOrganizationName("PID")

    # Стиль
    app.setStyle("Fusion")

    # Эмодзи-шрифт из бандла (на Astra эмодзи иначе рисуются квадратами)
    _load_bundled_fonts(app)

    # Главное окно
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
