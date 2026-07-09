"""
P&ID Pipeline - Desktop UI Application.

Точка входа для PySide6 приложения.
"""

import sys
import os
import logging

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
from PySide6.QtGui import QImageReader

from ui.windows.main_window import MainWindow

# Увеличить лимит загрузки изображений (по умолчанию 256 МБ, нужно для P&ID ~15000x7000)
QImageReader.setAllocationLimit(1024)  # 1 ГБ


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

    # Главное окно
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
