"""T8/T9 (П5) — реальный размер перекрёстков и мостов.

T8: список центров не изменился (координаты), маска после операции = квадраты
    size×size вокруг этих центров. Формулировка «число пятен то же» неверна:
    у корректной реализации число компонент легально меняется — расширение
    сливает соседние, сжатие разъединяет слипшиеся.
T9: undo возвращает обе маски побайтово И геометрию свежепоставленных
    QGraphicsRectItem (они живут отдельно от масок, снимка масок мало).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("cv2")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

IMG_W, IMG_H = 200, 200


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _mask_png(tmp_path, name, squares, size=15):
    """Бинарный PNG с белыми квадратами size×size вокруг заданных центров."""
    arr = np.zeros((IMG_H, IMG_W), dtype=np.uint8)
    half = size // 2
    for cx, cy in squares:
        arr[cy - half:cy - half + size, cx - half:cx - half + size] = 255
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0))
    for y in range(IMG_H):
        for x in range(IMG_W):
            if arr[y, x]:
                img.setPixelColor(x, y, QColor(255, 255, 255))
    p = tmp_path / name
    img.save(str(p))
    return p


@pytest.fixture
def editor(qapp, tmp_path):
    from ui.editors.square_mask_editor import SquareMaskEditor

    orig = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    orig.fill(QColor("white"))
    orig_path = tmp_path / "original.png"
    orig.save(str(orig_path))

    # Три перекрёстка: два рядом (при 15 — раздельные, при 25 сольются)
    # и один в стороне.
    j = _mask_png(tmp_path, "junction.png", [(40, 40), (58, 40), (150, 150)])
    b = _mask_png(tmp_path, "bridge.png", [(40, 150)])

    ed = SquareMaskEditor()
    assert ed.load_images(str(orig_path), str(j), str(b))
    ed.ensure_points()
    return ed


def _alpha(mask: QImage) -> np.ndarray:
    from ui.editors.square_mask_editor import _qimage_to_numpy
    return _qimage_to_numpy(mask)[:, :, 3] > 128


def _expected(centers, size) -> np.ndarray:
    out = np.zeros((IMG_H, IMG_W), dtype=bool)
    half = size // 2
    for cx, cy in centers:
        y1, y2 = max(0, cy - half), min(IMG_H, cy - half + size)
        x1, x2 = max(0, cx - half), min(IMG_W, cx - half + size)
        out[y1:y2, x1:x2] = True
    return out


# ── T8 ───────────────────────────────────────────────────────────────────

def test_t8_centers_survive_shrink(editor):
    """Сжатие: центры те же, маска = квадраты нового размера вокруг них."""
    before = sorted(editor._points[1])
    assert len(before) == 3

    editor.current_class = 1
    editor.apply_square_size(7, only_selected=False)

    assert sorted(editor._points[1]) == before, "центры сдвинулись"
    assert np.array_equal(_alpha(editor.mask1_image), _expected(before, 7))


def test_t8_centers_survive_grow(editor):
    """Расширение: центры те же; число КОМПОНЕНТ легально меняется (слипание)."""
    before = sorted(editor._points[1])
    n_blobs_before = len([b for b in editor._blobs if b["cls"] == 1])

    editor.current_class = 1
    editor.apply_square_size(25, only_selected=False)

    assert sorted(editor._points[1]) == before
    assert np.array_equal(_alpha(editor.mask1_image), _expected(before, 25))
    n_after = len([b for b in editor._blobs if b["cls"] == 1])
    assert n_after != n_blobs_before, "два соседних квадрата обязаны слиться"


def test_t8_shrink_splits_merged_back(editor):
    """Слипшиеся при расширении разъединяются обратно при сжатии — это и есть
    смысл работы по центрам, а не по морфологии."""
    centers = sorted(editor._points[1])
    editor.current_class = 1
    editor.apply_square_size(25, only_selected=False)
    editor.apply_square_size(7, only_selected=False)
    assert sorted(editor._points[1]) == centers
    assert np.array_equal(_alpha(editor.mask1_image), _expected(centers, 7))


def test_t8_other_class_untouched(editor):
    """Операция по текущему классу не трогает вторую маску."""
    before = _alpha(editor.mask2_image).copy()
    editor.current_class = 1
    editor.apply_square_size(9, only_selected=False)
    assert np.array_equal(_alpha(editor.mask2_image), before)


def test_t8_selection_limits_the_operation(editor):
    """Есть выделение — меняются только выделенные пятна."""
    target = [b for b in editor._blobs if b["cls"] == 1 and b["x1"] > 100][0]
    target["confirmed"] = True
    others = sorted(c for c in editor._points[1] if c[0] < 100)

    editor.apply_square_size(5)

    mask = _alpha(editor.mask1_image)
    # нетронутые остались 15×15
    assert np.array_equal(mask & _expected(others, 15), _expected(others, 15))
    # выделенное стало 5×5
    picked = [c for c in editor._points[1] if c[0] > 100]
    assert picked and np.array_equal(mask & _expected(picked, 15),
                                     _expected(picked, 5))


def test_t8_erase_is_by_component_not_bbox(editor, tmp_path):
    """Стирание идёт по связной компоненте: L-образная клякса и её сосед имеют
    перекрывающиеся bbox'ы, и стирание по bbox съело бы чужие пиксели."""
    from ui.editors.square_mask_editor import SquareMaskEditor, _qimage_to_numpy

    arr = np.zeros((IMG_H, IMG_W), dtype=np.uint8)
    arr[20:40, 20:40] = 255          # блок A
    arr[38:44, 38:70] = 255          # хвост, уходящий вправо (общий bbox с B)
    arr[20:35, 60:75] = 255          # блок B — внутри bbox первого
    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0))
    for y in range(IMG_H):
        for x in range(IMG_W):
            if arr[y, x]:
                img.setPixelColor(x, y, QColor(255, 255, 255))
    mp = tmp_path / "m.png"
    img.save(str(mp))
    orig = tmp_path / "o.png"
    o = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    o.fill(QColor("white"))
    o.save(str(orig))

    ed = SquareMaskEditor()
    assert ed.load_images(str(orig), str(mp))
    ed.ensure_points()
    # Выделяем ТОЛЬКО L-образную компоненту (ту, что содержит точку 25,25)
    lshape = [b for b in ed._blobs
              if b["cls"] == 1 and b["x1"] <= 25 < b["x2"] and b["y1"] <= 25 < b["y2"]]
    assert lshape, "L-образная компонента не найдена"
    for b in lshape:
        b["confirmed"] = True
    b_pixels_before = _alpha(ed.mask1_image)[20:35, 60:75].sum()

    ed.apply_square_size(9)

    # Пиксели соседнего блока B, попавшие в bbox L-кляксы, обязаны уцелеть
    assert _alpha(ed.mask1_image)[20:35, 60:75].sum() == b_pixels_before


# ── T9 ───────────────────────────────────────────────────────────────────

def test_t9_undo_restores_both_masks_bytewise(editor):
    m1 = _alpha(editor.mask1_image).copy()
    m2 = _alpha(editor.mask2_image).copy()

    editor.current_class = 1
    editor.apply_square_size(31, only_selected=False)
    assert not np.array_equal(_alpha(editor.mask1_image), m1)

    editor.undo()
    assert np.array_equal(_alpha(editor.mask1_image), m1)
    assert np.array_equal(_alpha(editor.mask2_image), m2)


def test_t9_undo_restores_fresh_square_geometry(editor):
    """Свежепоставленные квадраты (QGraphicsRectItem) живут отдельно от масок —
    undo обязан вернуть и их геометрию."""
    editor.current_class = 1
    editor.add_square(100, 100)
    rect = editor.squares_white[-1]
    before = rect.rect()

    editor.apply_square_size(31, only_selected=False)
    assert rect.rect() != before, "свежий квадрат должен был поменять размер"

    editor.undo()
    assert rect.rect() == before


def test_t9_undo_restores_centers_and_applied_size(editor):
    centers = {c: list(v) for c, v in editor._points.items()}
    sizes = dict(editor._applied_size)

    editor.current_class = 1
    editor.apply_square_size(31, only_selected=False)
    editor.undo()

    assert editor._points == centers
    assert editor._applied_size == sizes


def test_t9_operation_is_one_undo_step(editor):
    depth = len(editor.undo_stack)
    editor.current_class = 1
    editor.apply_square_size(9, only_selected=False)
    assert len(editor.undo_stack) == depth + 1


# ── персистентность центров ──────────────────────────────────────────────

def test_points_roundtrip_keeps_applied_size(editor):
    """export → load возвращает и центры, и последний применённый размер:
    без него экстрактор с дефолтной 15 не разберёт ужатые квадраты."""
    from ui.editors.square_mask_editor import SquareMaskEditor

    editor.current_class = 1
    editor.apply_square_size(7, only_selected=False)
    data = editor.export_points()
    assert data["junctions"] and data["junctions"][0]["size"] == 7

    fresh = SquareMaskEditor()
    fresh.load_points(data)
    assert fresh._applied_size[1] == 7
    assert sorted(fresh._points[1]) == sorted(editor._points[1])


def test_load_points_accepts_worker_format():
    """Формат воркера — списки [x, y] без size (points.json)."""
    from ui.editors.square_mask_editor import SquareMaskEditor

    ed = SquareMaskEditor()
    ed.load_points({"junctions": [[10, 20], [30, 40]], "bridges": [[50, 60]]})
    assert ed._points[1] == [(10, 20), (30, 40)]
    assert ed._points[2] == [(50, 60)]
