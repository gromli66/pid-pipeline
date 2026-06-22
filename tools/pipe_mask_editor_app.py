"""
Batch Pipe Mask Editor — проход по папке изображений + масок.

Пропускает уже валидированные (есть в validated/).
Ctrl+S — сохранить и перейти к следующему.
Skip — пропустить без сохранения.

Инструменты:
  ✏️ Полилиния — рисование маски
  🧹 Ластик — стирание маски
  🔲 Удалить рамку — обвести полигоном внутреннюю часть чертежа,
     всё снаружи полигона заливается цветом фона (и на оригинале, и на маске).

Запуск:
    python pipe_mask_editor_app.py
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QToolBar, QPushButton, QLabel, QStatusBar, QSlider,
    QMessageBox, QGraphicsView,
)
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import (
    QKeySequence, QAction, QImage, QPixmap, QColor, QPen,
    QPainter, QPainterPath, QBrush,
)

from ui.editors.polyline_mask_editor import (
    PolylineMaskEditor, PolylineTool,
    _qimage_to_numpy, _numpy_to_qimage,
    _make_grayscale_darkened, MASK_TILE_SIZE,
)
from ui.editors.polygon_overlay import PolygonVertexOverlay

import numpy as np
from PySide6.QtGui import QImageReader

QImageReader.setAllocationLimit(1024)  # 1GB вместо 256MB

# ── Визуально яркая маска поверх оригинала ──

MASK_COLOR = QColor(255, 255, 0)
MASK_ALPHA = 60


def _recolor_tile_pixmap(pixmap: QPixmap) -> QPixmap:
    """Белый полупрозрачный тайл → ярко-жёлтый полупрозрачный."""
    qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.bits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, w, 4)).copy()

    visible = arr[:, :, 3] > 0
    arr[visible, 0] = MASK_COLOR.blue()
    arr[visible, 1] = MASK_COLOR.green()
    arr[visible, 2] = MASK_COLOR.red()
    arr[visible, 3] = MASK_ALPHA

    out = QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
    return QPixmap.fromImage(out)


def _detect_background_color(qimage: QImage, margin: int = 15) -> QColor:
    """Определить цвет фона по краям изображения (медиана по полосам краёв).

    Берём пиксели из полос шириной `margin` по всем 4 сторонам.
    Медиана даёт устойчивость к линиям и надписям на рамке.
    """
    arr = _qimage_to_numpy(qimage)  # (H, W, 4) BGRA
    h, w = arr.shape[:2]
    m = min(margin, h // 4, w // 4)

    strips = []
    strips.append(arr[:m, :, :3].reshape(-1, 3))       # top
    strips.append(arr[h - m:, :, :3].reshape(-1, 3))    # bottom
    strips.append(arr[:, :m, :3].reshape(-1, 3))         # left
    strips.append(arr[:, w - m:, :3].reshape(-1, 3))     # right

    all_px = np.concatenate(strips, axis=0)
    median_bgr = np.median(all_px, axis=0).astype(np.uint8)

    return QColor(int(median_bgr[2]), int(median_bgr[1]), int(median_bgr[0]))


def _build_outer_mask(
    width: int, height: int, polygon_points: list[tuple[float, float]]
) -> QImage:
    """Построить маску: снаружи полигона = белый, внутри = прозрачный.

    Используется для определения области заливки фоном.
    """
    # Рисуем полигон как белый на чёрном — это «внутренность»
    inner = QImage(width, height, QImage.Format.Format_ARGB32)
    inner.fill(QColor(0, 0, 0, 255))

    painter = QPainter(inner)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(255, 255, 255, 255)))

    path = QPainterPath()
    path.moveTo(polygon_points[0][0], polygon_points[0][1])
    for x, y in polygon_points[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
    painter.drawPath(path)
    painter.end()

    # Инвертируем: внутри → 0, снаружи → 255
    arr = _qimage_to_numpy(inner)
    # arr[:,:,2] это R канал, white=255 inside polygon
    is_inside = arr[:, :, 2] > 128

    outer = np.zeros((height, width), dtype=np.uint8)
    outer[~is_inside] = 255

    return outer  # numpy mask: 255 = outside polygon


class BrightMaskEditor(PolylineMaskEditor):
    """PolylineMaskEditor с яркой полупрозрачной маской + удаление рамки."""

    def __init__(self):
        super().__init__()

        # ── Frame removal state ──
        self._frame_mode = False
        self._frame_overlay: PolygonVertexOverlay | None = None
        self._frame_drawing = False

    # ── Zoom to cursor fix ──────────────────────────────

    def wheelEvent(self, event):
        """Zoom к позиции курсора через сдвиг scrollbar'ов."""
        # Позиция курсора в координатах сцены ДО масштабирования
        old_pos = self.mapToScene(event.position().toPoint())

        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

        # Позиция курсора в координатах сцены ПОСЛЕ масштабирования
        new_pos = self.mapToScene(event.position().toPoint())

        # Компенсировать сдвиг через scrollbar'ы
        delta = old_pos - new_pos
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() + int(delta.x() * self.transform().m11())
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() + int(delta.y() * self.transform().m22())
        )

    # ── Mask recolor overrides (без изменений) ──

    # Padding вокруг изображения (% от размера) — позволяет скроллить
    # дальше краёв для удобного редактирования.
    SCENE_PADDING_RATIO = 0.3  # 30% с каждой стороны

    def _setup_scene(self):
        super()._setup_scene()
        for row in self._mask_tiles:
            for tile in row:
                tile.setPixmap(_recolor_tile_pixmap(tile.pixmap()))
                tile.setOpacity(1.0)
        # Убрать оверлей если остался от предыдущего изображения
        self._cleanup_frame_overlay()

        # Расширить sceneRect с padding'ом для доступа к краям
        pad_x = self.img_width * self.SCENE_PADDING_RATIO
        pad_y = self.img_height * self.SCENE_PADDING_RATIO
        self.setSceneRect(QRectF(
            -pad_x, -pad_y,
            self.img_width + pad_x * 2,
            self.img_height + pad_y * 2,
        ))
        self.fitInView(
            QRectF(0, 0, self.img_width, self.img_height),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def update_mask_display(self):
        super().update_mask_display()
        for row in self._mask_tiles:
            for tile in row:
                tile.setPixmap(_recolor_tile_pixmap(tile.pixmap()))
                tile.setOpacity(1.0)

    def _update_mask_region(self, x: int, y: int, radius: int):
        super()._update_mask_region(x, y, radius)
        ts = MASK_TILE_SIZE
        col_min = max(0, (x - radius) // ts)
        col_max = min(self._mask_tile_cols - 1, (x + radius) // ts)
        row_min = max(0, (y - radius) // ts)
        row_max = min(self._mask_tile_rows - 1, (y + radius) // ts)
        for r in range(row_min, row_max + 1):
            for c in range(col_min, col_max + 1):
                tile = self._mask_tiles[r][c]
                tile.setPixmap(_recolor_tile_pixmap(tile.pixmap()))
                tile.setOpacity(1.0)

    def _update_mask_rect(self, x1: int, y1: int, x2: int, y2: int):
        super()._update_mask_rect(x1, y1, x2, y2)
        ts = MASK_TILE_SIZE
        col_min = max(0, x1 // ts)
        col_max = min(self._mask_tile_cols - 1, x2 // ts)
        row_min = max(0, y1 // ts)
        row_max = min(self._mask_tile_rows - 1, y2 // ts)
        for r in range(row_min, row_max + 1):
            for c in range(col_min, col_max + 1):
                tile = self._mask_tiles[r][c]
                tile.setPixmap(_recolor_tile_pixmap(tile.pixmap()))
                tile.setOpacity(1.0)

    def _finish_polyline(self):
        super()._finish_polyline()
        if self.polylines:
            item = self.polylines[-1]
            pen = QPen(MASK_COLOR, item.pen().width())
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            item.setPen(pen)
            item.setOpacity(MASK_ALPHA / 255.0)

    # ================================================================
    # Frame removal — public API
    # ================================================================

    def set_frame_mode(self, enabled: bool):
        """Включить/выключить режим удаления рамки."""
        if enabled:
            # Переключиться в frame mode
            self._frame_mode = True
            self._start_frame_draw()
        else:
            self._frame_mode = False
            self._cleanup_frame_overlay()

    def is_frame_mode(self) -> bool:
        return self._frame_mode

    # ── Frame draw lifecycle ──

    def _start_frame_draw(self):
        """Начать рисование полигона рамки."""
        self._cleanup_frame_overlay()
        self._frame_overlay = PolygonVertexOverlay(self.scene, "__frame__")
        self._frame_overlay.show_draw()
        self._frame_drawing = True
        # Отключить drag, поставить крестик
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._update_status(
            "Рамка: ЛКМ — точки полигона, замкните на 1-ю точку / ПКМ / Enter. "
            "Ctrl+Z — отменить точку. Escape — отмена."
        )

    def _cleanup_frame_overlay(self):
        if self._frame_overlay:
            self._frame_overlay.hide()
            self._frame_overlay = None
        self._frame_drawing = False
        # Вернуть drag mode
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _apply_frame_removal(self):
        """Применить удаление рамки: залить снаружи полигона фоном."""
        if not self._frame_overlay:
            return

        points = self._frame_overlay.get_draw_points()
        if len(points) < 3:
            self._update_status("Нужно минимум 3 точки для полигона рамки.")
            return

        # Сохранить состояние для undo (оригинал + маска)
        orig_backup = self.original_image.copy()
        mask_backup = self.mask_image.copy()

        # 1. Определить цвет фона
        bg_color = _detect_background_color(self.original_image)

        # 2. Построить маску «снаружи полигона» для оригинала
        outer_orig = _build_outer_mask(self.img_width, self.img_height, points)

        # 3. Залить оригинал фоном снаружи полигона
        orig_arr = _qimage_to_numpy(self.original_image)
        outside_orig = outer_orig == 255
        orig_arr[outside_orig, 0] = bg_color.blue()
        orig_arr[outside_orig, 1] = bg_color.green()
        orig_arr[outside_orig, 2] = bg_color.red()
        self.original_image = _numpy_to_qimage(orig_arr)

        # 4. Обнулить маску снаружи полигона
        #    Маска может быть другого размера — масштабируем точки полигона
        mask_h = self.mask_image.height()
        mask_w = self.mask_image.width()
        if mask_w != self.img_width or mask_h != self.img_height:
            scale_x = mask_w / self.img_width
            scale_y = mask_h / self.img_height
            mask_points = [(x * scale_x, y * scale_y) for x, y in points]
            outer_mask = _build_outer_mask(mask_w, mask_h, mask_points)
        else:
            outer_mask = outer_orig

        mask_arr = _qimage_to_numpy(self.mask_image)
        outside_mask = outer_mask == 255
        mask_arr[outside_mask, 3] = 0  # прозрачный
        self.mask_image = _numpy_to_qimage(mask_arr)

        # 5. Перестроить сцену
        self._rebuild_scene_after_frame()

        # 6. Добавить в undo
        self.undo_stack.append(("frame_remove", orig_backup, mask_backup))

        self._update_status(
            f"Рамка удалена. Фон: RGB({bg_color.red()},{bg_color.green()},{bg_color.blue()})"
        )

        # Убрать оверлей
        self._cleanup_frame_overlay()

    def _rebuild_scene_after_frame(self):
        """Перестроить визуальные элементы сцены после изменения оригинала/маски."""
        # Обновить подложку (grayscale darkened)
        darkened = _make_grayscale_darkened(self.original_image, brightness=0.4)
        self.original_item.setPixmap(QPixmap.fromImage(darkened))

        # Обновить тайлы маски
        ts = MASK_TILE_SIZE
        for row_idx in range(self._mask_tile_rows):
            for col_idx in range(self._mask_tile_cols):
                x0 = col_idx * ts
                y0 = row_idx * ts
                w = min(ts, self.img_width - x0)
                h = min(ts, self.img_height - y0)
                region = self.mask_image.copy(x0, y0, w, h)
                tile = self._mask_tiles[row_idx][col_idx]
                tile.setPixmap(_recolor_tile_pixmap(QPixmap.fromImage(region)))
                tile.setOpacity(1.0)

    # ── Override undo для поддержки frame_remove ──

    def undo(self):
        if not self.undo_stack:
            return

        action = self.undo_stack[-1]
        if action[0] == "frame_remove":
            self.undo_stack.pop()
            _, orig_backup, mask_backup = action
            self.original_image = orig_backup
            self.mask_image = mask_backup
            self._rebuild_scene_after_frame()
            self._update_status("Отменено удаление рамки")
        else:
            super().undo()

    # ================================================================
    # Event overrides — перехват для frame mode
    #
    # В frame mode все клики идут БЕЗ Ctrl (прямой ЛКМ ставит точку).
    # Это позволяет не конфликтовать с базовыми Ctrl+ЛКМ/ПКМ.
    # Перехватываем события ДО super() и делаем return чтобы
    # базовый класс не обрабатывал их.
    # ================================================================

    def mousePressEvent(self, event):
        if self._frame_mode and self._frame_drawing and self._frame_overlay:
            pos = self.mapToScene(event.position().toPoint())

            if event.button() == Qt.MouseButton.LeftButton:
                if 0 <= pos.x() < self.img_width and 0 <= pos.y() < self.img_height:
                    # Snap к первой точке → замкнуть и применить
                    if self._frame_overlay.is_near_first_point(pos.x(), pos.y()):
                        self._apply_frame_removal()
                    else:
                        self._frame_overlay.add_draw_point(pos.x(), pos.y())
                        n = self._frame_overlay.draw_point_count
                        self._update_status(
                            f"Рамка: {n} точек. "
                            "Замкните на 1-ю точку / ПКМ / Enter."
                        )
                return  # съедаем событие

            elif event.button() == Qt.MouseButton.RightButton:
                if self._frame_overlay.draw_point_count >= 3:
                    self._apply_frame_removal()
                return  # съедаем событие

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """В frame mode двойной клик тоже замыкает полигон."""
        if self._frame_mode and self._frame_drawing and self._frame_overlay:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._frame_overlay.draw_point_count >= 3:
                    self._apply_frame_removal()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._frame_mode and self._frame_drawing and self._frame_overlay:
            pos = self.mapToScene(event.position().toPoint())
            self._frame_overlay.update_draw_preview(pos.x(), pos.y())
            # НЕ вызываем super — иначе ScrollHandDrag будет двигать холст
            # при зажатой ЛКМ (а мы хотим просто рисовать)
            return

        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        if self._frame_mode and self._frame_drawing:
            if event.key() == Qt.Key.Key_Escape:
                self._cleanup_frame_overlay()
                self._frame_mode = False
                self._update_status("Удаление рамки отменено.")
                return
            if (
                event.key() == Qt.Key.Key_Z
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                if self._frame_overlay:
                    self._frame_overlay.undo_last_draw_point()
                    n = self._frame_overlay.draw_point_count
                    self._update_status(f"Рамка: {n} точек.")
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._frame_overlay and self._frame_overlay.draw_point_count >= 3:
                    self._apply_frame_removal()
                return
            # Ctrl/Shift в frame mode — игнорируем, не переключаем курсор
            if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift):
                return

        super().keyPressEvent(event)


# ── Paths ──

# IMAGES_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\renders_clean")
# MASKS_DIR = Path(r"C:\project\pid\pid\app\result_seg_pipe\pre_annotation_457\masks")
IMAGES_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated\renders")
MASKS_DIR = Path(r"C:\Users\Maksim\Desktop\after_autocad\masks_a\validated")
VALIDATED_DIR = MASKS_DIR / "validated"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def collect_queue() -> list[tuple[Path, Path, Path]]:
    """Собрать список (image, mask, validated_out), пропуская готовые."""
    VALIDATED_DIR.mkdir(parents=True, exist_ok=True)

    queue = []
    for img_path in sorted(IMAGES_DIR.iterdir()):
        if img_path.suffix.lower() not in IMAGE_EXTS:
            continue

        mask_name = img_path.name
        mask_path = MASKS_DIR / mask_name
        out_path = VALIDATED_DIR / mask_name

        if out_path.exists():
            continue
        if not mask_path.exists():
            continue

        queue.append((img_path, mask_path, out_path))

    return queue


class BatchMaskEditor(QMainWindow):

    def __init__(self):
        super().__init__()

        self._queue = collect_queue()
        self._index = 0

        self._editor = BrightMaskEditor()
        self._editor.status_callback = self._on_status

        self.setWindowTitle("Batch Pipe Mask Editor")
        self.setMinimumSize(1000, 700)

        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()

        if not self._queue:
            QMessageBox.information(self, "Готово", "Нет файлов для валидации.")
            sys.exit(0)

        self._load_current()
        self.showMaximized()

    # ── Toolbar ───────────────────────────────────────────

    def _setup_toolbar(self):
        tb = QToolBar("Tools")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.btn_polyline = QPushButton("✏️ Полилиния")
        self.btn_polyline.setCheckable(True)
        self.btn_polyline.setChecked(True)
        self.btn_polyline.setStyleSheet(
            "QPushButton:checked { background-color: #4CAF50; color: white; }"
        )
        self.btn_polyline.clicked.connect(lambda: self._set_tool("polyline"))
        tb.addWidget(self.btn_polyline)

        self.btn_eraser = QPushButton("🧹 Ластик")
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.setStyleSheet(
            "QPushButton:checked { background-color: #FF9800; color: white; }"
        )
        self.btn_eraser.clicked.connect(lambda: self._set_tool("eraser"))
        tb.addWidget(self.btn_eraser)

        self.btn_frame = QPushButton("🔲 Удалить рамку")
        self.btn_frame.setCheckable(True)
        self.btn_frame.setStyleSheet(
            "QPushButton:checked { background-color: #E91E63; color: white; }"
        )
        self.btn_frame.clicked.connect(lambda: self._set_tool("frame"))
        tb.addWidget(self.btn_frame)

        tb.addSeparator()
        tb.addWidget(QLabel(" Ширина: "))

        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(2, 20)
        self.width_slider.setValue(4)
        self.width_slider.setMaximumWidth(140)
        self.width_slider.valueChanged.connect(self._on_width_changed)
        tb.addWidget(self.width_slider)
        self.width_label = QLabel("4px")
        tb.addWidget(self.width_label)

        tb.addSeparator()

        btn_undo = QPushButton("↩ Undo")
        btn_undo.setShortcut(QKeySequence("Ctrl+Z"))
        btn_undo.clicked.connect(self._editor.undo)
        tb.addWidget(btn_undo)

        tb.addSeparator()

        btn_save = QPushButton("💾 Save & Next (Ctrl+S)")
        btn_save.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 4px 12px;"
        )
        btn_save.clicked.connect(self._save_and_next)
        tb.addWidget(btn_save)

        btn_skip = QPushButton("⏭ Skip")
        btn_skip.setShortcut(QKeySequence("Ctrl+D"))
        btn_skip.clicked.connect(self._skip)
        tb.addWidget(btn_skip)

        act_save = QAction(self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(self._save_and_next)
        self.addAction(act_save)

    # ── Central / Status ──────────────────────────────────

    def _setup_central(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._editor)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)

    # ── Navigation ────────────────────────────────────────

    def _load_current(self):
        img_path, mask_path, out_path = self._queue[self._index]
        ok = self._editor.load_images(
            original_path=str(img_path),
            mask_path=str(mask_path),
        )
        if not ok:
            self.statusbar.showMessage(f"Ошибка загрузки: {img_path.name}")
            return

        total = len(self._queue)
        self.setWindowTitle(
            f"[{self._index + 1}/{total}] {img_path.name} — Batch Pipe Mask Editor"
        )
        self.statusbar.showMessage(
            f"{img_path.name}  |  Ctrl+S — сохранить  |  Ctrl+D — пропустить"
        )

    def _save_and_next(self):
        if self._index >= len(self._queue):
            return

        # Сохраняем маску
        _, _, out_path = self._queue[self._index]
        self._editor.save_mask(str(out_path))

        # Сохраняем оригинал (может быть изменён frame removal)
        img_path = self._queue[self._index][0]
        out_img_dir = out_path.parent / "renders"
        out_img_dir.mkdir(parents=True, exist_ok=True)
        out_img_path = out_img_dir / img_path.name
        self._editor.original_image.save(str(out_img_path))

        self.statusbar.showMessage(
            f"Сохранено: {out_path.parent.name}/{out_path.name} + "
            f"{out_img_dir.name}/{out_img_path.name}",
            3000,
        )
        self._next()

    def _skip(self):
        if self._editor.undo_stack:
            reply = QMessageBox.question(
                self, "Пропустить",
                "Есть несохранённые изменения. Пропустить?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return
        self._next()

    def _next(self):
        self._index += 1
        if self._index >= len(self._queue):
            QMessageBox.information(self, "Готово", "Все файлы обработаны!")
            self.close()
            return
        self._load_current()

    # ── Tool switching ────────────────────────────────────

    def _set_tool(self, tool: str):
        self.btn_polyline.setChecked(tool == "polyline")
        self.btn_eraser.setChecked(tool == "eraser")
        self.btn_frame.setChecked(tool == "frame")

        if tool == "frame":
            # Выключить другие инструменты, включить frame mode
            self._editor.set_tool(PolylineTool.POLYLINE)  # базовый — неважно
            self._editor.set_frame_mode(True)
        else:
            # Выключить frame mode
            self._editor.set_frame_mode(False)
            if tool == "polyline":
                self._editor.set_tool(PolylineTool.POLYLINE)
            else:
                self._editor.set_tool(PolylineTool.ERASER)

    def _on_width_changed(self, value: int):
        self.width_label.setText(f"{value}px")
        self._editor.set_line_width(value)

    def _on_status(self, msg: str):
        self.statusbar.showMessage(msg, 5000)

    # ── Close ─────────────────────────────────────────────

    def closeEvent(self, event):
        if self._editor.undo_stack:
            reply = QMessageBox.question(
                self, "Выход",
                "Есть несохранённые изменения. Выйти?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.No:
                event.ignore()
                return
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = BatchMaskEditor()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
