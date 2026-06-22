"""
run_junction_editor.py — автономный запуск редактора перекрёстков и мостов.

Открывает папку с изображениями, запускает SquareMaskEditor для каждого
изображения по очереди. Уже отвалидированные пропускаются при повторном запуске.

ОДНО ОКНО на весь сеанс — при переходе к следующему изображению окно НЕ
пересоздаётся, только подгружается новое содержимое. Это устраняет прыжки
окна между мониторами.

Горячие клавиши:
    Ctrl+S  — сохранить и перейти к следующему
    Ctrl+Z  — отменить последнее действие (undo)
    Esc     — пропустить без сохранения

Использование:
    python run_junction_editor.py

Настройка путей — только здесь, менять ниже в разделе «ПУТИ И НАСТРОЙКИ».
"""

import sys
import json
import logging
import tempfile
from pathlib import Path

# ===========================================================================
# ПУТИ И НАСТРОЙКИ — редактировать здесь
# ===========================================================================

# Папка с исходными изображениями (original PNG/JPG)
IMAGES_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\renders")

# Папка с junction масками (имя файла должно совпадать с изображением)
# Если маски нет — редактор откроется с пустой маской
JUNCTION_MASKS_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\junction")

# Папка с bridge масками (опционально, может быть пустой или несуществующей)
BRIDGE_MASKS_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\bridge")

# Папка с skeleton масками (опционально)
SKELETON_MASKS_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\skeletons")

# Куда сохранять отвалидированные junction маски (создастся автоматически)
OUTPUT_JUNCTION_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\junction_validated")

# Куда сохранять отвалидированные bridge маски (создастся автоматически)
OUTPUT_BRIDGE_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\bridge_validated")

# JSON-файл с прогрессом (какие изображения уже отвалидированы)
PROGRESS_FILE = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\jb\progress.json")

# Яркость фона (0.0 = чёрный, 1.0 = оригинал). 0.7 = менее тёмно чем дефолт 0.4
BACKGROUND_BRIGHTNESS = 0.5

# Поддерживаемые расширения изображений
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# ===========================================================================

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _find_mask(masks_dir: Path, stem: str) -> str:
    """Найти маску по stem имени файла в папке. Вернуть строку-путь или ''."""
    if not masks_dir.exists():
        return ""
    for ext in IMAGE_EXTENSIONS:
        candidate = masks_dir / (stem + ext)
        if candidate.exists():
            return str(candidate)
    return ""


def _load_progress() -> set:
    """Загрузить список уже отвалидированных файлов (stem-имена)."""
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            return set(data.get("validated", []))
        except Exception as exc:
            logger.warning("Не удалось прочитать progress.json: %s", exc)
    return set()


def _save_progress(validated: set):
    """Сохранить прогресс в JSON."""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps({"validated": sorted(validated)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _patch_brightness(brightness: float):
    """
    Monkey-patch _setup_scene в SquareMaskEditor:
    заменяет grayscale+затемнение на цветное затемнение с нашим brightness.
    brightness=0.4 захардкожен в вызове внутри _setup_scene, поэтому патчим
    сам метод, а не функцию _make_grayscale_darkened.
    Вызывать ДО импорта SquareMaskEditor.
    """
    import numpy as np
    import ui.editors.square_mask_editor as sme
    from PySide6.QtGui import QImage, QPixmap

    original_setup = sme.SquareMaskEditor._setup_scene

    def _patched_setup(self):
        # Повторяем логику _setup_scene, но с цветным затемнением
        self.scene.clear()
        self.squares_white.clear()
        self.squares_red.clear()
        self.undo_stack.clear()

        # Цветное затемнение вместо grayscale
        arr = sme._qimage_to_numpy(self.original_image).astype(np.float32)
        arr[:, :, :3] = (arr[:, :, :3] * brightness).clip(0, 255)
        arr[:, :, 3] = 255
        darkened = sme._numpy_to_qimage(arr.astype(np.uint8))

        from PySide6.QtWidgets import QGraphicsPixmapItem
        from PySide6.QtCore import QRectF, Qt

        bg_item = QGraphicsPixmapItem(QPixmap.fromImage(darkened))
        bg_item.setZValue(0)
        self.scene.addItem(bg_item)

        # Skeleton
        if self.skeleton_image:
            self.skeleton_item = QGraphicsPixmapItem(QPixmap.fromImage(self.skeleton_image))
            self.skeleton_item.setZValue(1)
            self.skeleton_item.setOpacity(0.5)
            self.scene.addItem(self.skeleton_item)

        # Mask1 (white/junction)
        self.mask1_item = QGraphicsPixmapItem(QPixmap.fromImage(self.mask1_image))
        self.mask1_item.setZValue(2)
        self.mask1_item.setOpacity(0.5)
        self.scene.addItem(self.mask1_item)

        # Mask2 (red/bridge)
        self.mask2_item = QGraphicsPixmapItem(QPixmap.fromImage(self.mask2_image))
        self.mask2_item.setZValue(3)
        self.mask2_item.setOpacity(0.5)
        self.scene.addItem(self.mask2_item)

        self.setSceneRect(QRectF(0, 0, self.img_width, self.img_height))
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

        self._detect_blobs()

    sme.SquareMaskEditor._setup_scene = _patched_setup


def main():
    # --- Проверка папки с изображениями ---
    if not IMAGES_DIR.exists():
        logger.error("Папка с изображениями не найдена: %s", IMAGES_DIR)
        sys.exit(1)

    images = sorted(
        p for p in IMAGES_DIR.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not images:
        logger.error("В папке %s нет изображений", IMAGES_DIR)
        sys.exit(1)

    # --- Создать выходные папки ---
    OUTPUT_JUNCTION_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

    # --- Загрузить прогресс ---
    validated = _load_progress()
    pending = [img for img in images if img.stem not in validated]

    logger.info(
        "Всего изображений: %d | Уже отвалидировано: %d | Осталось: %d",
        len(images), len(validated), len(pending),
    )

    if not pending:
        logger.info("Все изображения уже отвалидированы. Выход.")
        return

    # --- Qt приложение ---
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
    from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton
    from PySide6.QtWidgets import QLabel, QSlider, QMessageBox
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImageReader, QShortcut, QKeySequence, QImage

    # Снять лимит 256 МБ на загрузку изображений (P&ID могут весить 500+ МБ)
    QImageReader.setAllocationLimit(0)

    _patch_brightness(BACKGROUND_BRIGHTNESS)

    from ui.editors.square_mask_editor import SquareMaskEditor

    app = QApplication.instance() or QApplication(sys.argv)

    # =====================================================================
    # ОДНО окно, ОДИН редактор — на весь сеанс
    # =====================================================================
    window = QMainWindow()
    window.resize(1400, 900)

    central = QWidget()
    window.setCentralWidget(central)
    layout = QVBoxLayout(central)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    # --- Toolbar ---
    toolbar = QHBoxLayout()
    toolbar.setContentsMargins(8, 4, 8, 4)
    toolbar.setSpacing(8)

    btn_class1 = QPushButton("⬜ Junction (1)")
    btn_class1.setCheckable(True)
    btn_class1.setChecked(True)
    btn_class1.setStyleSheet(
        "QPushButton:checked { background-color: #4CAF50; color: white; }"
    )
    toolbar.addWidget(btn_class1)

    btn_class2 = QPushButton("🟥 Bridge (2)")
    btn_class2.setCheckable(True)
    btn_class2.setStyleSheet(
        "QPushButton:checked { background-color: #F44336; color: white; }"
    )
    toolbar.addWidget(btn_class2)

    toolbar.addWidget(QLabel(" Размер:"))
    slider = QSlider(Qt.Horizontal)
    slider.setRange(3, 30)
    slider.setValue(15)
    slider.setMaximumWidth(140)
    lbl_size = QLabel("15px")
    toolbar.addWidget(slider)
    toolbar.addWidget(lbl_size)

    toolbar.addWidget(
        QLabel("  Ctrl+S: сохранить | Ctrl+Z: undo | Esc: пропустить | "
               "Ctrl+ЛКМ: квадрат | Shift+drag: обводка | Ctrl+ПКМ: удалить | 1/2: класс")
    )
    toolbar.addStretch()

    btn_undo = QPushButton("↩ Undo")
    toolbar.addWidget(btn_undo)

    btn_skip = QPushButton("⏭ Пропустить")
    btn_skip.setToolTip("Не сохранять, перейти к следующему (Esc)")
    toolbar.addWidget(btn_skip)

    btn_save_next = QPushButton("💾 Сохранить и продолжить")
    btn_save_next.setToolTip("Ctrl+S")
    btn_save_next.setStyleSheet("""
        QPushButton {
            background-color: #2196F3; color: white;
            font-weight: bold; padding: 6px 14px; border-radius: 4px;
        }
        QPushButton:hover { background-color: #1976D2; }
    """)
    toolbar.addWidget(btn_save_next)

    btn_save_exit = QPushButton("✅ Сохранить и выйти")
    btn_save_exit.setStyleSheet("""
        QPushButton {
            background-color: #4CAF50; color: white;
            font-weight: bold; padding: 6px 14px; border-radius: 4px;
        }
        QPushButton:hover { background-color: #388E3C; }
    """)
    toolbar.addWidget(btn_save_exit)

    layout.addLayout(toolbar)

    # --- Editor (создаём один раз) ---
    editor = SquareMaskEditor()
    layout.addWidget(editor, stretch=1)

    # --- Status (создаём один раз) ---
    status_label = QLabel("")
    status_label.setStyleSheet("color: #888; font-size: 11px; padding: 2px 8px;")
    layout.addWidget(status_label)
    editor.status_callback = lambda msg: status_label.setText(msg)

    # =====================================================================
    # Состояние сеанса (мутабельное — меняется при переходах между файлами)
    # =====================================================================
    session = {
        "iterator": iter(pending),
        "current_image": None,
        "current_stem": None,
        "tmp_files": [],  # временные файлы текущего изображения
    }

    # ── Обработчики переключений класса/размера ──
    # Класс/размер настраиваем после load_images, поэтому связываем кнопки
    # через editor (он создаётся один раз).

    def set_class(cls):
        btn_class1.setChecked(cls == 1)
        btn_class2.setChecked(cls == 2)
        editor.current_class = cls

    btn_class1.clicked.connect(lambda: set_class(1))
    btn_class2.clicked.connect(lambda: set_class(2))

    slider.valueChanged.connect(lambda v: (
        lbl_size.setText(f"{v}px"),
        editor.set_square_size(v),
    ))

    btn_undo.clicked.connect(editor.undo)

    # ── Очистка временных файлов ──

    def _cleanup_tmp():
        for f in session["tmp_files"]:
            try:
                Path(f).unlink()
            except Exception:
                pass
        session["tmp_files"] = []

    # ── Загрузка следующего изображения В ТО ЖЕ ОКНО ──

    def load_next() -> bool:
        """Подгрузить следующее изображение в существующий editor.

        Возвращает True если что-то загружено, False если очередь кончилась.
        """
        _cleanup_tmp()

        try:
            img_path = next(session["iterator"])
        except StopIteration:
            logger.info("Все изображения обработаны.")
            return False

        session["current_image"] = img_path
        stem = img_path.stem
        session["current_stem"] = stem
        logger.info("Открываю: %s", img_path.name)

        # Найти маски
        junction_mask = _find_mask(JUNCTION_MASKS_DIR, stem)
        bridge_mask = _find_mask(BRIDGE_MASKS_DIR, stem)
        skeleton_mask = _find_mask(SKELETON_MASKS_DIR, stem)

        # Если junction маски нет — создать пустую временную
        if not junction_mask:
            orig = QImage(str(img_path))
            if orig.isNull():
                logger.warning("Не удалось открыть %s, пропускаю", img_path)
                return load_next()  # рекурсивно к следующему
            empty = QImage(orig.width(), orig.height(), QImage.Format.Format_Grayscale8)
            empty.fill(0)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            empty.save(tmp.name)
            junction_mask = tmp.name
            session["tmp_files"].append(tmp.name)

        # === Подгружаем в существующий редактор ===
        ok = editor.load_images(
            original_path=str(img_path),
            mask1_path=junction_mask,
            mask2_path=bridge_mask,
            skeleton_path=skeleton_mask,
        )
        if not ok:
            logger.warning("Не удалось загрузить изображение %s", img_path)
            return load_next()  # к следующему

        # Сбросить состояние UI на дефолты
        set_class(1)  # начинаем с junction по умолчанию
        slider.setValue(15)

        # Обновить заголовок и статус
        window.setWindowTitle(
            f"Junction/Bridge Editor — {img_path.name} "
            f"({len(validated)}/{len(images)} отвалидировано)"
        )
        status_label.setText(f"Изображение: {img_path.name}")
        return True

    # ── Действия по кнопкам/шорткатам ──

    def _save_and_proceed(close_after: bool):
        stem = session["current_stem"]
        if stem is None:
            return
        j_out = OUTPUT_JUNCTION_DIR / (stem + ".png")
        b_out = OUTPUT_BRIDGE_DIR / (stem + ".png")
        try:
            editor.save_masks(str(j_out), str(b_out))
        except Exception as exc:
            QMessageBox.warning(window, "Ошибка сохранения", str(exc))
            return
        validated.add(stem)
        _save_progress(validated)
        status_label.setText(f"✅ Сохранено → {j_out.name}")
        logger.info("Сохранено: %s", stem)

        if close_after:
            _cleanup_tmp()
            window.close()
            app.quit()
        else:
            if not load_next():
                # Очередь кончилась
                _cleanup_tmp()
                QMessageBox.information(window, "Готово", "Все изображения обработаны!")
                window.close()
                app.quit()

    def _skip():
        stem = session["current_stem"]
        if stem is not None:
            logger.info("Пропущено: %s", stem)
        if not load_next():
            _cleanup_tmp()
            QMessageBox.information(window, "Готово", "Все изображения обработаны!")
            window.close()
            app.quit()

    btn_skip.clicked.connect(_skip)
    btn_save_next.clicked.connect(lambda: _save_and_proceed(close_after=False))
    btn_save_exit.clicked.connect(lambda: _save_and_proceed(close_after=True))

    # --- Горячие клавиши (привязаны к окну, живут весь сеанс) ---
    QShortcut(QKeySequence("Ctrl+S"), window).activated.connect(
        lambda: _save_and_proceed(close_after=False)
    )
    QShortcut(QKeySequence("Ctrl+Z"), window).activated.connect(editor.undo)
    QShortcut(QKeySequence("Esc"), window).activated.connect(_skip)

    # --- Загрузить первое изображение ---
    if not load_next():
        logger.info("Нечего обрабатывать.")
        return

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
