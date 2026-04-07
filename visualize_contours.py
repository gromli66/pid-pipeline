"""
Visualize SAM2 contour polygons on original P&ID image.

Usage:
    python visualize_contours.py
    python visualize_contours.py --contours path/to/contours_auto.json --image path/to/image.png
    python visualize_contours.py --output result.png
"""

import json
import argparse
import cv2
import numpy as np
from pathlib import Path


# Colors by status
COLOR_AUTO = (0, 200, 0)        # green
COLOR_REVIEW = (0, 140, 255)    # orange
COLOR_EMPTY = (0, 0, 200)       # red (no polygon)

# Vertex dot
VERTEX_RADIUS = 3
VERTEX_COLOR = (255, 0, 200)    # magenta


def draw_contours(image, contours_data, show_bbox=True, show_vertices=True, show_labels=True):
    vis = image.copy()
    nodes = contours_data.get("nodes", [])

    stats = {"auto": 0, "review": 0, "empty": 0}

    for node in nodes:
        poly_flat = node.get("polygon_auto", [])
        status = node.get("status", "manual_review")
        confidence = node.get("confidence", 0.0)
        class_name = node.get("class_name", "?")
        bbox = node.get("bbox", [])
        n_points = node.get("n_points", 0)

        if status == "auto":
            color = COLOR_AUTO
        else:
            color = COLOR_REVIEW

        # Draw polygon
        if poly_flat and len(poly_flat) >= 6:
            pts = []
            for i in range(0, len(poly_flat), 2):
                pts.append([int(poly_flat[i]), int(poly_flat[i + 1])])
            pts_np = np.array(pts, dtype=np.int32)

            # Fill with transparency
            overlay = vis.copy()
            cv2.fillPoly(overlay, [pts_np], color)
            cv2.addWeighted(overlay, 0.25, vis, 0.75, 0, vis)

            # Contour outline
            cv2.polylines(vis, [pts_np], True, color, 2, cv2.LINE_AA)

            # Vertices
            if show_vertices:
                for pt in pts:
                    cv2.circle(vis, (pt[0], pt[1]), VERTEX_RADIUS, VERTEX_COLOR, -1)

            if status == "auto":
                stats["auto"] += 1
            else:
                stats["review"] += 1
        else:
            stats["empty"] += 1
            color = COLOR_EMPTY

        # Draw bbox
        if show_bbox and bbox and len(bbox) == 4:
            bx, by, bw, bh = [int(v) for v in bbox]
            cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), color, 1)

        # Label
        if show_labels and bbox and len(bbox) == 4:
            bx, by = int(bbox[0]), int(bbox[1])
            label = f"{class_name} {confidence:.2f} {n_points}pts"
            # Background for text
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.35, 1)
            cv2.rectangle(vis, (bx, by - th - 4), (bx + tw, by), (0, 0, 0), -1)
            cv2.putText(vis, label, (bx, by - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1, cv2.LINE_AA)

    return vis, stats


def main():
    default_base = Path(r"C:\project\pid_pipeline\storage\diagrams\500143fe-47a7-4ea6-a4b5-d0d517af681b")

    parser = argparse.ArgumentParser(description="Visualize SAM2 contours on P&ID image")
    parser.add_argument("--contours", type=Path,
                        default=default_base / "contours" / "contours_auto.json")
    parser.add_argument("--image", type=Path,
                        default=default_base / "original" / "image.png")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output path (default: contours_vis.png next to contours JSON)")
    parser.add_argument("--no-bbox", action="store_true", help="Hide bounding boxes")
    parser.add_argument("--no-vertices", action="store_true", help="Hide vertex dots")
    parser.add_argument("--no-labels", action="store_true", help="Hide class/confidence labels")
    parser.add_argument("--scale", type=float, default=1.0, help="Scale output image")
    args = parser.parse_args()

    # Load
    print(f"Image:    {args.image}")
    print(f"Contours: {args.contours}")

    image = cv2.imread(str(args.image))
    if image is None:
        raise FileNotFoundError(f"Cannot load image: {args.image}")

    with open(args.contours, "r", encoding="utf-8") as f:
        contours_data = json.load(f)

    print(f"Nodes:    {len(contours_data.get('nodes', []))}")
    print(f"Stats:    {contours_data.get('stats', {})}")

    # Draw
    vis, draw_stats = draw_contours(
        image, contours_data,
        show_bbox=not args.no_bbox,
        show_vertices=not args.no_vertices,
        show_labels=not args.no_labels,
    )

    # Scale
    if args.scale != 1.0:
        h, w = vis.shape[:2]
        vis = cv2.resize(vis, (int(w * args.scale), int(h * args.scale)))

    # Save
    output = args.output or (args.contours.parent / "contours_vis.png")
    cv2.imwrite(str(output), vis)

    print(f"\nResult:")
    print(f"  Auto (green):    {draw_stats['auto']}")
    print(f"  Review (orange): {draw_stats['review']}")
    print(f"  Empty (red):     {draw_stats['empty']}")
    print(f"  Saved: {output}")


if __name__ == "__main__":
    main()

