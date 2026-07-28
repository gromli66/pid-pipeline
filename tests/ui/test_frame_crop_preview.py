"""T7 (П3) — предпросмотр обрезки листа в «Очистке рамки».

Раньше отпускание кнопки сразу резало лист. Теперь протяжка ПАРКУЕТ рамку,
её можно поправить (любая точка стороны, углы, перенос целиком) и применить по
Enter либо отменить по Esc.

Методы дёргаются напрямую (_start_crop / _update_crop_preview / _finish_crop /
keyPressEvent) — мышь через QTest не нужна, а состояние проверяется точнее.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor, QKeyEvent  # noqa: E402
from PySide6.QtCore import Qt, QPointF, QEvent       # noqa: E402

IMG_W, IMG_H = 300, 200


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(qapp, tmp_path):
    from ui.editors.frame_editor import FrameRemoverView

    img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    path = tmp_path / "sheet.png"
    img.save(str(path))

    ed = FrameRemoverView()
    assert ed.load_image(str(path))
    ed.set_tool("crop")
    return ed


def _key(k, mods=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, k, mods)


def _drag_out(ed, x1, y1, x2, y2):
    """Протяжка: press → move → release (превью паркуется)."""
    ed._start_crop(QPointF(x1, y1))
    ed._update_crop_preview(QPointF(x2, y2))
    ed._finish_crop(QPointF(x2, y2))


# ── парковка превью ──────────────────────────────────────────────────────

def test_t7_release_parks_preview_and_does_not_crop(editor):
    """Отпускание кнопки больше НЕ применяет обрезку."""
    _drag_out(editor, 50, 40, 200, 150)
    assert editor._crop_rect == (50, 40, 200, 150)
    assert editor._crop_preview is not None
    assert (editor.img_w, editor.img_h) == (IMG_W, IMG_H), "лист порезался сразу"
    assert not editor.has_edits


def test_t7_degenerate_drag_is_rejected_at_finish(editor):
    """Вырожденная рамка отсекается при завершении протяжки, а не молча по Enter."""
    _drag_out(editor, 50, 40, 52, 42)
    assert editor._crop_rect is None
    assert editor._crop_preview is None


# ── правка рамки ─────────────────────────────────────────────────────────

def test_t7_drag_side_then_enter_gives_expected_size(editor):
    """Протяжка → тяга за правую сторону → Enter даёт ожидаемый размер."""
    _drag_out(editor, 50, 40, 200, 150)
    assert editor._crop_zone_at(200, 100) == 'r'
    editor._start_crop_drag('r', 200, 100)
    editor._update_crop_drag(240, 100)
    editor._end_crop_drag()
    assert editor._crop_rect == (50, 40, 240, 150)

    editor.keyPressEvent(_key(Qt.Key.Key_Return))
    assert (editor.img_w, editor.img_h) == (190, 110)
    assert editor.has_edits


def test_t7_corner_zone_moves_both_sides(editor):
    _drag_out(editor, 50, 40, 200, 150)
    assert editor._crop_zone_at(50, 40) == 'tl'
    editor._start_crop_drag('tl', 50, 40)
    editor._update_crop_drag(70, 60)
    assert editor._crop_rect == (70, 60, 200, 150)


def test_t7_inside_zone_moves_whole_rect(editor):
    _drag_out(editor, 50, 40, 200, 150)
    assert editor._crop_zone_at(120, 100) == 'move'
    editor._start_crop_drag('move', 120, 100)
    editor._update_crop_drag(130, 110)
    assert editor._crop_rect == (60, 50, 210, 160)


def test_t7_move_is_clamped_to_sheet(editor):
    _drag_out(editor, 50, 40, 200, 150)
    editor._start_crop_drag('move', 120, 100)
    editor._update_crop_drag(1000, 1000)
    l, t, r, b = editor._crop_rect
    assert (r - l, b - t) == (150, 110), "перенос не должен менять размер"
    assert (r, b) == (IMG_W, IMG_H)


def test_t7_side_drag_keeps_min_size(editor):
    """Сторону нельзя протащить за противоположную."""
    _drag_out(editor, 50, 40, 200, 150)
    editor._start_crop_drag('l', 50, 100)
    editor._update_crop_drag(1000, 100)
    l, _t, r, _b = editor._crop_rect
    assert r - l == editor.MIN_CROP


def test_t7_zone_is_none_outside_preview(editor):
    _drag_out(editor, 50, 40, 200, 150)
    assert editor._crop_zone_at(10, 10) is None


# ── Enter / Esc / Ctrl+Z ─────────────────────────────────────────────────

def test_t7_escape_leaves_image_untouched(editor):
    _drag_out(editor, 50, 40, 200, 150)
    editor.keyPressEvent(_key(Qt.Key.Key_Escape))
    assert editor._crop_rect is None and editor._crop_preview is None
    assert (editor.img_w, editor.img_h) == (IMG_W, IMG_H)
    assert not editor.has_edits


def test_t7_undo_after_enter_restores_original(editor):
    _drag_out(editor, 50, 40, 200, 150)
    editor.keyPressEvent(_key(Qt.Key.Key_Return))
    assert (editor.img_w, editor.img_h) == (150, 110)

    editor.keyPressEvent(_key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert (editor.img_w, editor.img_h) == (IMG_W, IMG_H)


def test_t7_ctrl_z_with_live_preview_cancels_preview_only(editor):
    """Ctrl+Z при живом превью гасит превью, а не правку изображения:
    undo пересобрал бы сцену и убил превью-item посреди жеста."""
    _drag_out(editor, 50, 40, 200, 150)
    editor.keyPressEvent(_key(Qt.Key.Key_Return))          # правка №1
    assert editor.has_edits
    _drag_out(editor, 10, 10, 100, 90)                     # новое превью

    editor.keyPressEvent(_key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert editor._crop_rect is None
    assert (editor.img_w, editor.img_h) == (150, 110), "первая правка не должна откатиться"

    editor.keyPressEvent(_key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert (editor.img_w, editor.img_h) == (IMG_W, IMG_H)


def test_t7_tool_switch_drops_parked_preview(editor):
    """Смена инструмента = отказ от обрезки (принято как норма)."""
    _drag_out(editor, 50, 40, 200, 150)
    editor.set_tool("box")
    assert editor._crop_rect is None and editor._crop_preview is None
    assert (editor.img_w, editor.img_h) == (IMG_W, IMG_H)


def test_t7_new_drag_replaces_parked_preview(editor):
    """Клик мимо превью начинает новую протяжку, старая рамка заменяется."""
    _drag_out(editor, 50, 40, 200, 150)
    _drag_out(editor, 20, 20, 120, 100)
    assert editor._crop_rect == (20, 20, 120, 100)


def test_t7_escape_is_not_stolen_when_no_crop(editor):
    """Правило «Esc только при активной обрезке» сохранено."""
    editor.set_tool("crop")
    assert editor._crop_rect is None
    # не должно упасть и не должно ничего сделать
    editor.keyPressEvent(_key(Qt.Key.Key_Escape))
    assert (editor.img_w, editor.img_h) == (IMG_W, IMG_H)
