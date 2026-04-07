"""
Magic Wand Contour Editor v3 для unknow-узлов P&ID.

ДВА РЕЖИМА (автоматически по цвету пикселя):
  Клик на БЕЛУЮ ЛИНИЮ → connected component линии → fill holes → контур
  Клик на ЧЁРНУЮ ВНУТРЕННОСТЬ → flood fill замкнутой области → контур

Shift+click: добавить компонент (собрать из нескольких частей)
Right-click: убрать компонент

Использование:
  python magic_wand_editor.py --image <path.png> --graph <graph.json>
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
from matplotlib.widgets import Button, Slider


PAD = 40


class MagicWandEditor:
    def __init__(self, ax, image_crop, bbox_local=None, cvat_points=None,
                 close_r=2, epsilon_pct=1.2):
        self.ax = ax
        self.gray = image_crop if len(image_crop.shape) == 2 \
            else cv2.cvtColor(image_crop, cv2.COLOR_BGR2GRAY)
        self.h, self.w = self.gray.shape
        self.bbox_local = bbox_local
        self.close_r = close_r
        self.epsilon_pct = epsilon_pct

        # Бинарное: белые линии = 255
        _, self.binary = cv2.threshold(self.gray, 127, 255, cv2.THRESH_BINARY)

        # Connected components БЕЛЫХ пикселей (линии)
        self.wh_nlabels, self.wh_labels, self.wh_stats, self.wh_centroids = \
            cv2.connectedComponentsWithStats(self.binary, connectivity=8)

        # Connected components ЧЁРНЫХ пикселей (области между линиями)
        black = cv2.bitwise_not(self.binary)
        self.bk_nlabels, self.bk_labels, self.bk_stats, self.bk_centroids = \
            cv2.connectedComponentsWithStats(black, connectivity=8)

        print(f"    White components (lines): {self.wh_nlabels - 1}")
        print(f"    Black components (areas): {self.bk_nlabels - 1}")

        # Display: инвертированное (чёрные линии на белом)
        self.display_base = cv2.cvtColor(255 - self.gray, cv2.COLOR_GRAY2RGB)

        # State
        self.selected_white = set()   # selected white (line) component labels
        self.selected_black = set()   # selected black (interior) component labels
        self.contour_pts = []
        self.mask = None

        # Artists
        self.img_artist = ax.imshow(self.display_base, aspect='equal')

        if bbox_local:
            x1, y1, x2, y2 = bbox_local
            ax.add_patch(plt.Rectangle((x1, y1), x2-x1, y2-y1, fill=False,
                         edgecolor='blue', linewidth=0.8, linestyle='--', alpha=0.4))

        if cvat_points and len(cvat_points) >= 3:
            ax.add_patch(MplPolygon(np.array(cvat_points), closed=True, fill=False,
                         edgecolor='green', linewidth=1.0, linestyle='--', alpha=0.4))

        self.contour_artist = None
        self.contour_scatter = None
        self.fill_artist = None

        ax.figure.canvas.mpl_connect('button_press_event', self._on_click)

    def _on_click(self, event):
        if event.inaxes != self.ax or event.button not in (1, 3):
            return
        x, y = int(round(event.xdata)), int(round(event.ydata))
        if x < 0 or x >= self.w or y < 0 or y >= self.h:
            return

        pixel_val = self.gray[y, x]
        is_bright = pixel_val >= 128  # белый пиксель = линия

        if event.button == 1:
            if is_bright:
                # Клик на ЛИНИЮ
                lbl = self.wh_labels[y, x]
                if lbl == 0:
                    return
                area = self.wh_stats[lbl, cv2.CC_STAT_AREA]
                if area < 5:
                    return

                if event.key == 'shift':
                    self.selected_white.add(lbl)
                    print(f"    +white #{lbl} (area={area})")
                else:
                    self.selected_white = {lbl}
                    self.selected_black.clear()
                    print(f"    Selected white #{lbl} (area={area}) — line component")

            else:
                # Клик на ЧЁРНУЮ ОБЛАСТЬ
                lbl = self.bk_labels[y, x]
                if lbl == 0:
                    return
                area = self.bk_stats[lbl, cv2.CC_STAT_AREA]
                if area < 5:
                    return

                if event.key == 'shift':
                    self.selected_black.add(lbl)
                    print(f"    +black #{lbl} (area={area})")
                else:
                    self.selected_black = {lbl}
                    self.selected_white.clear()
                    print(f"    Selected black #{lbl} (area={area}) — interior region")

            self._build_mask()
            self._update_display()

        elif event.button == 3:
            # Right click: remove
            if is_bright:
                lbl = self.wh_labels[y, x]
                self.selected_white.discard(lbl)
            else:
                lbl = self.bk_labels[y, x]
                self.selected_black.discard(lbl)
            self._build_mask()
            self._update_display()

    def _build_mask(self):
        """Собрать маску и контур из выбранных компонентов."""
        mask = np.zeros((self.h, self.w), dtype=np.uint8)

        # Белые компоненты (линии)
        for lbl in self.selected_white:
            mask[self.wh_labels == lbl] = 255

        # Чёрные компоненты (внутренности)
        for lbl in self.selected_black:
            mask[self.bk_labels == lbl] = 255

        if mask.sum() == 0:
            self.mask = None
            self.contour_pts = []
            return

        # Morphological close
        if self.close_r > 0:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (self.close_r*2+1, self.close_r*2+1))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        # Fill holes
        flood = mask.copy()
        bm = np.zeros((self.h+2, self.w+2), dtype=np.uint8)
        cv2.floodFill(flood, bm, (0, 0), 128)
        holes = (flood == 0).astype(np.uint8) * 255
        mask = cv2.bitwise_or(mask, holes)

        self.mask = mask

        # Contour
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_L1)
        if not contours:
            self.contour_pts = []
            return
        largest = max(contours, key=cv2.contourArea)
        eps = self.epsilon_pct * 0.01 * cv2.arcLength(largest, True)
        simp = cv2.approxPolyDP(largest, eps, True)
        self.contour_pts = [(float(pt[0][0]), float(pt[0][1])) for pt in simp]
        print(f"    → contour: {len(self.contour_pts)} pts, "
              f"area={cv2.contourArea(largest):.0f}")

    def _update_display(self):
        display = self.display_base.copy()

        # Подсветить заполненную область
        if self.mask is not None:
            display[self.mask > 0] = [200, 225, 255]  # light blue fill

        self.img_artist.set_data(display)

        # Contour line
        if self.contour_artist:
            self.contour_artist.remove()
            self.contour_artist = None
        if self.contour_scatter:
            self.contour_scatter.remove()
            self.contour_scatter = None

        if len(self.contour_pts) >= 3:
            xy = np.array(self.contour_pts)
            xs = list(xy[:, 0]) + [xy[0, 0]]
            ys = list(xy[:, 1]) + [xy[0, 1]]
            self.contour_artist, = self.ax.plot(xs, ys, 'r-', linewidth=2.5, alpha=0.9)
            self.contour_scatter = self.ax.scatter(
                xy[:, 0], xy[:, 1], c='red', s=30, zorder=10,
                edgecolors='white', linewidths=1)

        self.ax.set_xlabel(
            f"White sel: {len(self.selected_white)} | "
            f"Black sel: {len(self.selected_black)} | "
            f"Contour: {len(self.contour_pts)} pts",
            fontsize=9, fontfamily='monospace')

        self.ax.figure.canvas.draw_idle()

    def recompute(self):
        self._build_mask()
        self._update_display()

    def get_contour(self):
        return list(self.contour_pts)


class EditorApp:
    def __init__(self, image_path, graph_path, output_path):
        self.output_path = output_path
        self.approved = {}

        self.image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if self.image is None:
            raise FileNotFoundError(f"Cannot load: {image_path}")
        print(f"Image: {self.image.shape}")

        with open(graph_path) as f:
            graph = json.load(f)

        self.nodes = []
        for node in graph.get('nodes', []):
            if node.get('class_name') != 'unknow':
                continue
            bbox = node.get('bbox')
            if not bbox or len(bbox) != 4:
                continue
            seg = node.get('segmentation', [])
            cvat = [(seg[i], seg[i+1]) for i in range(0, len(seg), 2)] \
                if seg and len(seg) >= 6 else []
            self.nodes.append({'id': node['id'], 'bbox': bbox, 'cvat': cvat})

        print(f"Found {len(self.nodes)} unknow nodes\n")
        self.current_idx = 0

    def _crop_node(self, node):
        h, w = self.image.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in node['bbox']]
        rx1, ry1 = max(0, x1-PAD), max(0, y1-PAD)
        rx2, ry2 = min(w, x2+PAD), min(h, y2+PAD)
        crop = self.image[ry1:ry2, rx1:rx2].copy()
        bbox_local = (x1-rx1, y1-ry1, x2-rx1, y2-ry1)
        cvat_local = [(px-rx1, py-ry1) for (px, py) in node['cvat']]
        return crop, bbox_local, cvat_local, (rx1, ry1)

    def _show_node(self):
        if self.current_idx >= len(self.nodes):
            self._finish()
            return

        node = self.nodes[self.current_idx]
        crop, bbox_local, cvat_local, offset = self._crop_node(node)

        plt.close('all')
        self.fig, self.ax = plt.subplots(1, 1, figsize=(14, 8))
        self.fig.subplots_adjust(bottom=0.22)

        n = len(self.approved)
        self.ax.set_title(
            f"{node['id']}  |  bbox {node['bbox']}  |  CVAT: {len(node['cvat'])} pts  |  "
            f"{self.current_idx+1}/{len(self.nodes)}  |  approved: {n}",
            fontsize=11, fontfamily='monospace')

        print(f"  [{self.current_idx+1}/{len(self.nodes)}] {node['id']}")

        self.editor = MagicWandEditor(
            self.ax, crop, bbox_local=bbox_local, cvat_points=cvat_local)
        self._offset = offset
        self._node = node

        # Buttons
        bh, y = 0.045, 0.12
        ax_back    = self.fig.add_axes([0.02, y, 0.08, bh])
        ax_approve = self.fig.add_axes([0.13, y, 0.14, bh])
        ax_skip    = self.fig.add_axes([0.30, y, 0.08, bh])
        ax_reset   = self.fig.add_axes([0.41, y, 0.08, bh])
        ax_export  = self.fig.add_axes([0.52, y, 0.13, bh])
        ax_clear   = self.fig.add_axes([0.68, y, 0.08, bh])

        Button(ax_back, '← Back').on_clicked(self._on_back)
        Button(ax_approve, '✓ Approve & Next',
               color='#ffe0e8', hovercolor='#ffb0c0').on_clicked(self._on_approve)
        Button(ax_skip, 'Skip →').on_clicked(self._on_skip)
        Button(ax_reset, 'Reset').on_clicked(self._on_reset)
        Button(ax_export, f'Export ({n})',
               color='#e0ffe0', hovercolor='#b0ffb0').on_clicked(self._on_export)
        Button(ax_clear, 'Clear').on_clicked(self._on_clear)

        # Keep references to prevent GC
        self._buttons = [ax_back, ax_approve, ax_skip, ax_reset, ax_export, ax_clear]

        # Sliders
        ax_c = self.fig.add_axes([0.15, 0.05, 0.3, 0.03])
        ax_e = self.fig.add_axes([0.55, 0.05, 0.3, 0.03])
        self.sl_c = Slider(ax_c, 'Close gaps', 0, 8, valinit=2, valstep=1)
        self.sl_e = Slider(ax_e, 'Epsilon %', 0.3, 5.0, valinit=1.2, valstep=0.1)
        self.sl_c.on_changed(self._on_param)
        self.sl_e.on_changed(self._on_param)

        # Help
        ha = self.fig.add_axes([0.15, 0.005, 0.7, 0.04])
        ha.axis('off')
        ha.text(0, 0.5,
            "Click LINE (dark on display) → line component + fill  |  "
            "Click INSIDE (light on display) → interior flood fill\n"
            "Shift+click: add more  |  Right-click: remove  |  "
            "Close gaps: merge nearby parts",
            fontsize=8, fontfamily='monospace', va='center', color='#555')

        plt.show()

    def _on_param(self, val):
        self.editor.close_r = int(self.sl_c.val)
        self.editor.epsilon_pct = self.sl_e.val
        self.editor.recompute()

    def _on_approve(self, event):
        pts = self.editor.get_contour()
        if len(pts) < 3:
            print("    ✗ Click on symbol first!")
            return
        ox, oy = self._offset
        glob = [(round(px+ox, 1), round(py+oy, 1)) for (px, py) in pts]
        flat = []
        for x, y in glob:
            flat.extend([x, y])
        self.approved[self._node['id']] = {'polygon': glob, 'segmentation': flat}
        print(f"    ✓ Approved: {len(pts)} pts")
        self.current_idx += 1
        self._show_node()

    def _on_skip(self, event):
        self.current_idx += 1
        self._show_node()

    def _on_back(self, event):
        if self.current_idx > 0:
            self.current_idx -= 1
            self._show_node()

    def _on_reset(self, event):
        self._show_node()

    def _on_clear(self, event):
        self.editor.selected_white.clear()
        self.editor.selected_black.clear()
        self.editor._build_mask()
        self.editor._update_display()

    def _on_export(self, event):
        self._save()

    def _save(self):
        with open(self.output_path, 'w') as f:
            json.dump(self.approved, f, indent=2)
        print(f"\n    Exported {len(self.approved)} → {self.output_path}")

    def _finish(self):
        print(f"\nDone! {len(self.approved)}/{len(self.nodes)} approved.")
        if self.approved:
            self._save()
        plt.close('all')

    def run(self):
        self._show_node()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--image', type=Path, required=True)
    p.add_argument('--graph', type=Path, required=True)
    p.add_argument('--output', type=Path, default=Path('approved_contours.json'))
    a = p.parse_args()
    EditorApp(a.image, a.graph, a.output).run()

if __name__ == '__main__':
    main()
