"""Бенчмарк сборки сцены редактора графа: цена подсветки сторон (П8).

Меряет время setup_scene и число QGraphicsItem на синтетических графах
100/500/1000 узлов — с подсветкой сторон и без неё.

Порог тревоги (UI_TABS_BACKLOG, «Бенчмарки»): участков — ПО СТЕПЕНИ УЗЛА
(у коллекторов их больше четырёх), т.е. на листе порядка +2×рёбер item'ов.
Если просадка заметна — один QGraphicsPathItem на слой вместо отдельных.

Замер 2026-07-28 (Windows, PySide6 6.11, offscreen), 1000 узлов / 999 рёбер:
    без подсветки  31 мс, 2500 item'ов
    с подсветкой   54 мс, 3499 item'ов (+23 мс, +999, +0.2 МБ)
Вариант «один item на узел» дал 46.6 против 48.3 мс — экономия в пределах
шума, поэтому оставлен item на участок (нужен для side_items[node_id] и
поштучной чистки в remove_node_items). Просадки, требующей слоя, нет.

Запуск (руками, до/после правки):
    QT_QPA_PLATFORM=offscreen python tools/bench/bench_scene.py
"""
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QImage, QColor            # noqa: E402

SIZES = (100, 500, 1000)
IMG_W, IMG_H = 1920, 1080
# Первый прогон меряет прогрев (импорты, шрифты, QImage), а не сцену:
# на 1000 узлах он давал 150 мс против 29 мс установившихся. Берём лучшее из N.
REPEATS = 4


def make_graph(n_nodes: int) -> dict:
    """Цепочка «бокс — коннектор — бокс — ...»: рёбер ≈ узлов, степень 2.

    Каждый второй узел — equipment с bbox 42x38 (габарит фикс-размеров холста),
    остальные — коннекторы. Раскладка сеткой, чтобы боксы не слипались.
    """
    nodes, links = [], []
    per_row = 40
    for i in range(n_nodes):
        col, row = i % per_row, i // per_row
        cx, cy = 24.0 + col * 46.0, 24.0 + row * 44.0
        if i % 2 == 0:
            nodes.append({
                "id": f"n{i}", "type": "equipment", "centroid": [cy, cx],
                "bbox": [cx - 21.0, cy - 19.0, cx + 21.0, cy + 19.0],
                "segmentation": None, "class_id": 1, "class_name": "nasos",
            })
        else:
            nodes.append({
                "id": f"n{i}", "type": "connector", "centroid": [cy, cx],
                "bbox": None, "segmentation": None,
                "class_id": -1, "class_name": "connector",
            })
        if i:
            links.append({
                "id": f"e{i}", "source": f"n{i-1}", "target": f"n{i}",
                "source_point": [cy, cx - 23.0], "target_point": [cy, cx - 21.0],
                "waypoints": [],
            })
    return {"directed": False, "multigraph": False,
            "graph": {"image_size": [IMG_H, IMG_W]},
            "nodes": nodes, "links": links, "text_blocks": [], "bindings": []}


def bench(app, tmp: Path, n_nodes: int, side_marks: bool):
    from ui.editors.simple_graph_editor import SimpleGraphEditor

    img_path = tmp / "bg.png"
    if not img_path.exists():
        img = QImage(IMG_W, IMG_H, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        img.save(str(img_path))
    graph_path = tmp / f"g{n_nodes}.json"
    graph_path.write_text(json.dumps(make_graph(n_nodes)), encoding="utf-8")

    best, items, marks, peak = None, 0, 0, 0
    for run in range(REPEATS):
        ed = SimpleGraphEditor()
        ed.show_side_marks = side_marks

        if run == REPEATS - 1:
            tracemalloc.start()
        t0 = time.perf_counter()
        ed.load_data(str(img_path), str(graph_path))
        dt = time.perf_counter() - t0
        if run == REPEATS - 1:
            _cur, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        best = dt if best is None else min(best, dt)
        items = len(ed.scene.items())
        marks = sum(len(v) for v in ed.side_items.values())
        ed.deleteLater()
    return best, items, marks, peak


def main():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="bench_scene_") as td:
        tmp = Path(td)
        print(f"{'узлов':>6} {'подсветка':>10} {'время, мс':>10} "
              f"{'items':>7} {'участков':>9} {'пик RAM, МБ':>12}")
        for n in SIZES:
            base = None
            for on in (False, True):
                dt, items, marks, peak = bench(app, tmp, n, on)
                print(f"{n:>6} {str(on):>10} {dt*1000:>10.1f} {items:>7} "
                      f"{marks:>9} {peak/2**20:>12.1f}")
                if on is False:
                    base = (dt, items)
                else:
                    print(f"{'':>6} {'дельта':>10} "
                          f"{(dt-base[0])*1000:>+10.1f} {items-base[1]:>+7}")


if __name__ == "__main__":
    main()
