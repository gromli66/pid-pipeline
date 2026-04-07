"""
Polygon Editor для unknow-узлов P&ID.

Показывает инвертированное изображение (чёрные линии на белом фоне).
CVAT-полигон загружается как начальная форма.
Можно перетаскивать вершины, добавлять/удалять точки.

Управление:
  - Левая кнопка: drag вершины
  - Double-click на ребро: добавить вершину
  - Right-click на вершину: удалить
  - Кнопки: Approve, Skip, Reset, Export

Зависимости: opencv-python, matplotlib, numpy

Использование:
  python polygon_editor.py --image <path.png> --graph <graph.json> --output approved_contours.json
"""

import cv2
import numpy as np
import json
import argparse
from pathlib import Path
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.lines import Line2D
from matplotlib.widgets import Button


PAD = 40  # отступ вокруг bbox


class PolygonEditor:
    """Интерактивный редактор полигонов на matplotlib."""

    def __init__(self, ax, image_crop, init_points=None, cvat_points=None, bbox_local=None):
        self.ax = ax
        self.image_crop = image_crop
        self.bbox_local = bbox_local

        # Показываем инвертированное изображение: чёрные линии на белом
        inverted = 255 - image_crop
        # Конвертируем grayscale в RGB для отображения
        if len(inverted.shape) == 2:
            display = cv2.cvtColor(inverted, cv2.COLOR_GRAY2RGB)
        else:
            display = cv2.cvtColor(inverted, cv2.COLOR_BGR2RGB)

        self.img_artist = ax.imshow(display, aspect='equal')

        # Bbox rectangle
        if bbox_local:
            x1, y1, x2, y2 = bbox_local
            rect = plt.Rectangle((x1, y1), x2 - x1, y2 - y1,
                                  fill=False, edgecolor='blue', linewidth=0.8,
                                  linestyle='--', alpha=0.4)
            ax.add_patch(rect)

        # CVAT reference polygon (non-editable, green dashed)
        self.cvat_points = cvat_points
        if cvat_points and len(cvat_points) >= 3:
            cvat_xy = np.array(cvat_points)
            cvat_poly = MplPolygon(cvat_xy, closed=True, fill=False,
                                    edgecolor='green', linewidth=1.0,
                                    linestyle='--', alpha=0.5)
            ax.add_patch(cvat_poly)

        # Editable polygon
        self.points = list(init_points) if init_points else []
        self.poly_patch = None
        self.scatter = None
        self.line = None
        self._update_polygon()

        # Interaction state
        self.dragging = -1
        self.press_xy = None

        # Connect events
        self.cid_press = ax.figure.canvas.mpl_connect('button_press_event', self._on_press)
        self.cid_release = ax.figure.canvas.mpl_connect('button_release_event', self._on_release)
        self.cid_motion = ax.figure.canvas.mpl_connect('motion_notify_event', self._on_motion)

    def _update_polygon(self):
        """Перерисовать полигон и точки."""
        # Remove old artists
        if self.poly_patch:
            self.poly_patch.remove()
            self.poly_patch = None
        if self.scatter:
            self.scatter.remove()
            self.scatter = None
        if self.line:
            self.line.remove()
            self.line = None

        if len(self.points) >= 3:
            xy = np.array(self.points)
            self.poly_patch = MplPolygon(xy, closed=True,
                                          facecolor=(1, 0.2, 0.4, 0.1),
                                          edgecolor='red', linewidth=1.5)
            self.ax.add_patch(self.poly_patch)

            # Closed line
            xs = list(xy[:, 0]) + [xy[0, 0]]
            ys = list(xy[:, 1]) + [xy[0, 1]]
            self.line, = self.ax.plot(xs, ys, 'r-', linewidth=1.5, alpha=0.8)

        if self.points:
            xy = np.array(self.points)
            self.scatter = self.ax.scatter(xy[:, 0], xy[:, 1],
                                            c='red', s=40, zorder=10,
                                            edgecolors='white', linewidths=1)

        self.ax.figure.canvas.draw_idle()

    def _nearest_point(self, mx, my, threshold=8):
        """Найти ближайшую вершину."""
        if not self.points:
            return -1
        # threshold в пикселях дисплея → в координатах данных
        xlim = self.ax.get_xlim()
        bbox = self.ax.get_window_extent()
        scale = (xlim[1] - xlim[0]) / bbox.width
        thr = threshold * scale

        best_i, best_d = -1, thr
        for i, (px, py) in enumerate(self.points):
            d = np.hypot(px - mx, py - my)
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _nearest_edge(self, mx, my, threshold=6):
        """Найти ближайшее ребро (для вставки точки)."""
        if len(self.points) < 2:
            return -1
        xlim = self.ax.get_xlim()
        bbox = self.ax.get_window_extent()
        scale = (xlim[1] - xlim[0]) / bbox.width
        thr = threshold * scale

        best_i, best_d = -1, thr
        n = len(self.points)
        for i in range(n):
            j = (i + 1) % n
            ax, ay = self.points[i]
            bx, by = self.points[j]
            dx, dy = bx - ax, by - ay
            len2 = dx * dx + dy * dy
            if len2 == 0:
                continue
            t = max(0, min(1, ((mx - ax) * dx + (my - ay) * dy) / len2))
            d = np.hypot(mx - (ax + t * dx), my - (ay + t * dy))
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _on_press(self, event):
        if event.inaxes != self.ax:
            return
        mx, my = event.xdata, event.ydata

        # Right click → delete vertex
        if event.button == 3:
            idx = self._nearest_point(mx, my)
            if idx >= 0 and len(self.points) > 3:
                self.points.pop(idx)
                self._update_polygon()
            return

        # Double click → insert on edge
        if event.dblclick and event.button == 1:
            edge = self._nearest_edge(mx, my)
            if edge >= 0:
                self.points.insert(edge + 1, (round(mx, 1), round(my, 1)))
                self._update_polygon()
            elif len(self.points) < 3:
                # Draw mode: add point
                self.points.append((round(mx, 1), round(my, 1)))
                self._update_polygon()
            return

        # Single click → start drag
        if event.button == 1:
            idx = self._nearest_point(mx, my)
            if idx >= 0:
                self.dragging = idx
                self.press_xy = (mx, my)

    def _on_release(self, event):
        self.dragging = -1
        self.press_xy = None

    def _on_motion(self, event):
        if self.dragging < 0 or event.inaxes != self.ax:
            return
        mx, my = event.xdata, event.ydata
        self.points[self.dragging] = (round(mx, 1), round(my, 1))
        self._update_polygon()

    def get_points(self):
        return list(self.points)


class EditorApp:
    """Приложение: проход по всем unknow-узлам."""

    def __init__(self, image_path, graph_path, output_path):
        self.output_path = output_path
        self.approved = {}

        # Загрузить изображение
        self.image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if self.image is None:
            raise FileNotFoundError(f"Cannot load image: {image_path}")
        print(f"Image: {self.image.shape}")

        # Загрузить граф
        with open(graph_path) as f:
            graph = json.load(f)

        # Найти unknow-узлы
        self.nodes = []
        for node in graph.get('nodes', []):
            if node.get('class_name') != 'unknow':
                continue
            bbox = node.get('bbox')
            if not bbox or len(bbox) != 4:
                continue

            seg = node.get('segmentation', [])
            cvat_pts = []
            if seg and len(seg) >= 6:
                cvat_pts = [(seg[i], seg[i + 1]) for i in range(0, len(seg), 2)]

            self.nodes.append({
                'id': node['id'],
                'bbox': bbox,
                'cvat': cvat_pts,
            })

        print(f"Found {len(self.nodes)} unknow nodes")
        self.current_idx = 0
        self.editor = None

    def _crop_node(self, node):
        """Получить кроп и локальные координаты."""
        h, w = self.image.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in node['bbox']]
        rx1 = max(0, x1 - PAD)
        ry1 = max(0, y1 - PAD)
        rx2 = min(w, x2 + PAD)
        ry2 = min(h, y2 + PAD)

        crop = self.image[ry1:ry2, rx1:rx2].copy()
        bbox_local = (x1 - rx1, y1 - ry1, x2 - rx1, y2 - ry1)

        # CVAT points → crop coords
        cvat_local = [(px - rx1, py - ry1) for (px, py) in node['cvat']]

        return crop, bbox_local, cvat_local, (rx1, ry1)

    def _show_node(self):
        """Показать текущий узел."""
        if self.current_idx >= len(self.nodes):
            self._finish()
            return

        node = self.nodes[self.current_idx]
        crop, bbox_local, cvat_local, offset = self._crop_node(node)

        plt.close('all')
        self.fig, self.ax = plt.subplots(1, 1, figsize=(14, 8))
        self.fig.subplots_adjust(bottom=0.15)

        approved_count = len(self.approved)
        title = (f"{node['id']}  |  "
                 f"bbox {node['bbox']}  |  "
                 f"CVAT: {len(node['cvat'])} pts  |  "
                 f"{self.current_idx + 1}/{len(self.nodes)}  |  "
                 f"approved: {approved_count}")
        self.ax.set_title(title, fontsize=11, fontfamily='monospace')

        # Если уже approved — загрузить
        init_pts = cvat_local
        if node['id'] in self.approved:
            glob = self.approved[node['id']]['polygon']
            ox, oy = offset
            init_pts = [(px - ox, py - oy) for (px, py) in glob]

        self.editor = PolygonEditor(
            self.ax, crop,
            init_points=init_pts,
            cvat_points=cvat_local,
            bbox_local=bbox_local,
        )
        self._offset = offset
        self._node = node

        # Buttons
        ax_approve = self.fig.add_axes([0.15, 0.03, 0.15, 0.05])
        ax_skip = self.fig.add_axes([0.35, 0.03, 0.1, 0.05])
        ax_reset = self.fig.add_axes([0.50, 0.03, 0.1, 0.05])
        ax_export = self.fig.add_axes([0.65, 0.03, 0.15, 0.05])
        ax_back = self.fig.add_axes([0.02, 0.03, 0.1, 0.05])

        self.btn_approve = Button(ax_approve, '✓ Approve & Next', color='#ffe0e8', hovercolor='#ffb0c0')
        self.btn_skip = Button(ax_skip, 'Skip →', color='#e0e0e0', hovercolor='#c0c0c0')
        self.btn_reset = Button(ax_reset, 'Reset', color='#e0e0e0', hovercolor='#c0c0c0')
        self.btn_export = Button(ax_export, f'Export ({approved_count})', color='#e0ffe0', hovercolor='#b0ffb0')
        self.btn_back = Button(ax_back, '← Back', color='#e0e0e0', hovercolor='#c0c0c0')

        self.btn_approve.on_clicked(self._on_approve)
        self.btn_skip.on_clicked(self._on_skip)
        self.btn_reset.on_clicked(self._on_reset)
        self.btn_export.on_clicked(self._on_export)
        self.btn_back.on_clicked(self._on_back)

        plt.show()

    def _on_approve(self, event):
        pts = self.editor.get_points()
        if len(pts) < 3:
            print(f"  Need at least 3 points!")
            return

        ox, oy = self._offset
        glob = [(round(px + ox, 1), round(py + oy, 1)) for (px, py) in pts]
        flat = []
        for (x, y) in glob:
            flat.extend([x, y])

        self.approved[self._node['id']] = {
            'polygon': glob,
            'segmentation': flat,
        }
        print(f"  ✓ Approved {self._node['id']}: {len(pts)} pts")

        self.current_idx += 1
        self._show_node()

    def _on_skip(self, event):
        print(f"  Skipped {self._node['id']}")
        self.current_idx += 1
        self._show_node()

    def _on_back(self, event):
        if self.current_idx > 0:
            self.current_idx -= 1
            self._show_node()

    def _on_reset(self, event):
        """Reset to CVAT polygon."""
        self._show_node()

    def _on_export(self, event):
        self._save()

    def _save(self):
        with open(self.output_path, 'w') as f:
            json.dump(self.approved, f, indent=2)
        print(f"\n  Exported {len(self.approved)} contours → {self.output_path}")

    def _finish(self):
        print(f"\nDone! Approved {len(self.approved)}/{len(self.nodes)} nodes.")
        if self.approved:
            self._save()
        plt.close('all')

    def run(self):
        self._show_node()


def main():
    parser = argparse.ArgumentParser(description='Polygon editor for unknow P&ID nodes')
    parser.add_argument('--image', type=Path, required=True, help='P&ID image (PNG)')
    parser.add_argument('--graph', type=Path, required=True, help='graph.json')
    parser.add_argument('--output', type=Path, default=Path('approved_contours.json'),
                        help='Output JSON (default: approved_contours.json)')
    args = parser.parse_args()

    app = EditorApp(args.image, args.graph, args.output)
    app.run()


if __name__ == '__main__':
    main()
