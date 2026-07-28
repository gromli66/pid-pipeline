"""Бенчмарк операции «изменить размер перекрёстков» на крупных масках (П5).

Меряет время и пик памяти на маске 4964x3509 при 50/500/5000 пятнах.

Порог тревоги (UI_TABS_BACKLOG, «Бенчмарки»): каждая запись undo — ДВЕ
полнокадровые копии QImage ≈ 139 МБ; deque(maxlen=50) теоретически до ~7 ГБ,
поэтому пик памяти обязателен к замеру, а не «на глаз».

Замер 2026-07-28 (Windows, PySide6 6.11, offscreen), маска 4964x3509:
    50 пятен   → операция 0.44–0.47 с, undo 0.18–0.21 с
    500 пятен  → операция 0.60–1.14 с, undo 0.19–0.33 с
    5000 пятен → операция 4.3–4.9 с,  undo 0.28–0.33 с
Пик по tracemalloc ~366–371 МБ (numpy-буферы). ВАЖНО: копии QImage живут на
стороне C++ и в tracemalloc НЕ попадают — оценка «две полнокадровые копии на
запись undo» остаётся в силе поверх этих чисел. Число записей то же, что у
существующих операций (flood_fill, delete_all_blobs), новых рисков нет.

Запуск (руками, до/после правки):
    QT_QPA_PLATFORM=offscreen python tools/bench/bench_mask_ops.py
"""
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np                                   # noqa: E402
from PySide6.QtWidgets import QApplication           # noqa: E402
from PySide6.QtGui import QImage, QColor             # noqa: E402

# Рабочий растр корпуса (6e7144d5): 3509x4964.
IMG_W, IMG_H = 4964, 3509
COUNTS = (50, 500, 5000)
BASE_SIZE = 15


def _write_mask(path: Path, centers, size: int):
    """Бинарный PNG с квадратами size×size вокруг центров (через numpy)."""
    arr = np.zeros((IMG_H, IMG_W, 4), dtype=np.uint8)
    arr[:, :, 3] = 255                      # непрозрачный чёрный фон
    half = size // 2
    for cx, cy in centers:
        arr[cy - half:cy - half + size, cx - half:cx - half + size, :] = 255
    img = QImage(arr.data, IMG_W, IMG_H, IMG_W * 4, QImage.Format.Format_ARGB32)
    img.copy().save(str(path))


def _centers(n: int):
    """Сетка центров, гарантированно не слипающихся при BASE_SIZE."""
    step = 40
    cols = max(1, (IMG_W - 80) // step)
    return [(40 + (i % cols) * step, 40 + (i // cols) * step) for i in range(n)]


def bench(tmp: Path, n: int, new_size: int):
    from ui.editors.square_mask_editor import SquareMaskEditor

    orig = tmp / "orig.png"
    if not orig.exists():
        img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        img.save(str(orig))
    mask = tmp / f"mask_{n}.png"
    if not mask.exists():
        _write_mask(mask, _centers(n), BASE_SIZE)

    ed = SquareMaskEditor()
    t0 = time.perf_counter()
    ed.load_images(str(orig), str(mask))
    ed.ensure_points()
    t_load = time.perf_counter() - t0

    ed.current_class = 1
    tracemalloc.start()
    t0 = time.perf_counter()
    changed = ed.apply_square_size(new_size, only_selected=False)
    t_apply = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    t0 = time.perf_counter()
    ed.undo()
    t_undo = time.perf_counter() - t0

    ed.deleteLater()
    return t_load, t_apply, t_undo, changed, peak


def main():
    QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="bench_mask_") as td:
        tmp = Path(td)
        print(f"маска {IMG_W}x{IMG_H}, базовый квадрат {BASE_SIZE}px")
        print(f"{'пятен':>6} {'новый':>6} {'загрузка':>10} {'операция':>10} "
              f"{'undo':>8} {'изменено':>9} {'пик RAM, МБ':>12}")
        for n in COUNTS:
            for new_size in (7, 25):
                tl, ta, tu, changed, peak = bench(tmp, n, new_size)
                print(f"{n:>6} {new_size:>6} {tl*1000:>10.0f} {ta*1000:>10.0f} "
                      f"{tu*1000:>8.0f} {changed:>9} {peak/2**20:>12.1f}")


if __name__ == "__main__":
    main()
