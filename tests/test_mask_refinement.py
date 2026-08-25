"""Тесты restore_direction_box_tails — возврат хвостов труб в боксах стрелок.

Запуск:  pytest tests/test_mask_refinement.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from modules.mask_refinement import restore_direction_box_tails  # noqa: E402


def _coco():
    return {
        "categories": [{"id": 1, "name": "napravlenie"},
                       {"id": 2, "name": "annotation"}],
        "annotations": [{"category_id": 1, "bbox": [40, 8, 20, 10]},
                        {"category_id": 2, "bbox": [70, 8, 20, 10]}],
    }


def test_tails_restored_only_in_direction_boxes():
    raw = np.zeros((30, 100), np.uint8)
    raw[12:15, 5:58] = 1       # труба модели заходит в бокс стрелки (x 40..57)
    raw[12:15, 70:88] = 1      # «труба» модели в текстовом боксе — не наша зона
    mask = np.zeros((30, 100), np.uint8)
    mask[12:15, 5:38] = 255    # валидированная: хвост обрезан до x=38
    out, added = restore_direction_box_tails(mask, raw, _coco())
    assert added == 3 * 18                      # только внутри бокса стрелки
    assert (out[12:15, 40:58] == 255).all()     # хвост восстановлен
    assert (out[12:15, 70:88] == 0).all()       # текстовый бокс не тронут
    assert (out[12:15, 38:40] == 0).all()       # вне боксов ничего не добавлено
    assert mask[12:15, 40:58].sum() == 0        # вход не мутирован


def test_tails_noop_without_direction_boxes():
    raw = np.ones((10, 10), np.uint8)
    mask = np.zeros((10, 10), np.uint8)
    out, added = restore_direction_box_tails(
        mask, raw, {"categories": [], "annotations": []})
    assert added == 0 and out.sum() == 0


if __name__ == "__main__":
    test_tails_restored_only_in_direction_boxes()
    test_tails_noop_without_direction_boxes()
    print("OK")
