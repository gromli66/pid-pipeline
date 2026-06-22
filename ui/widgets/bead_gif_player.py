"""
BeadGifPlayer — плеер анимаций (GIF) для активного этапа пайплайна.

Показывает анимацию текущего/предлагаемого этапа, масштабируя её под
доступную область с сохранением пропорций. Если для этапа гифки нет —
выводит нейтральный плейсхолдер.
"""

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QLabel, QSizePolicy
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QMovie


class BeadGifPlayer(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 120)
        self.setStyleSheet("color: #777; font-size: 14px;")
        self._movie: Optional[QMovie] = None
        self._orig_size = QSize(0, 0)
        self._current_path: Optional[str] = None
        self._placeholder = "Анимация появится на активном этапе"
        self.setText(self._placeholder)

    def set_gif(self, path: Optional[str]):
        """Показать гифку по пути (или плейсхолдер, если path пуст/не найден)."""
        norm = str(path) if path else None
        if norm == self._current_path:
            return
        self._current_path = norm

        # остановить прежнюю
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        self.setMovie(None)

        if not norm or not Path(norm).exists():
            self.setText(self._placeholder)
            return

        movie = QMovie(norm)
        if not movie.isValid():
            self.setText(self._placeholder)
            return
        movie.jumpToFrame(0)
        self._orig_size = movie.currentImage().size()
        self._movie = movie
        self.setMovie(movie)
        self._rescale()
        movie.start()

    def clear_gif(self):
        self.set_gif(None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._movie is None or self._orig_size.isEmpty():
            return
        avail = self.size()
        ow, oh = self._orig_size.width(), self._orig_size.height()
        if ow <= 0 or oh <= 0:
            return
        scale = min(avail.width() / ow, avail.height() / oh)
        scale = max(0.05, min(scale, 2.0))
        self._movie.setScaledSize(QSize(max(1, int(ow * scale)),
                                        max(1, int(oh * scale))))
