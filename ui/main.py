"""
P&ID Pipeline - Desktop UI Application.

Точка входа для PySide6 приложения.
"""

import sys
import os
import logging

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
