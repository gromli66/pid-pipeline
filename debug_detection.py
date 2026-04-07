import sys
sys.path.insert(0, r"C:\project\pid\pid\app")

from ultralytics import YOLO
import cv2
from pid_node_detection.data.preprocessing import binarize_for_yolo

weights = r"C:\project\pid\pid\app\result_yolo_node\experiments\stage2_new\weights\best.pt"
image = r"C:\project\pid\pid\app\data\test_validation\images\03e59965-9455-4b4a-9c2f-3a4f0cab1145-05.png"



model = YOLO(weights)
img = binarize_for_yolo(image, method="full")
h, w = img.shape[:2]
print(f"Размер: {w}x{h}")

tile_size = 1280
overlap = 0.25
step = int(tile_size * (1 - overlap))

datchik_total = 0
all_classes = {}

for y in range(0, h - tile_size + 1, step):
    for x in range(0, w - tile_size + 1, step):
        tile = img[y:y+tile_size, x:x+tile_size]
        results = model(tile, conf=0.3, imgsz=1280, verbose=False)
        boxes = results[0].boxes

        for i in range(len(boxes)):
            cls = int(boxes.cls[i])
            conf = float(boxes.conf[i])
            name = results[0].names[cls]
            all_classes[name] = all_classes.get(name, 0) + 1
            if cls == 33:
                datchik_total += 1
                print(f"  datchik @ tile({x},{y}) conf={conf:.3f}")

print(f"\nИтого datchik: {datchik_total}")
print(f"\nВсе классы (до NMS):")
for name, cnt in sorted(all_classes.items(), key=lambda x: -x[1]):
    print(f"  {name}: {cnt}")