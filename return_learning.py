# python -c "from ultralytics import YOLO; m = YOLO(r'result_yolo_node\experiments\baseline_full\weights\last.pt'); m.train(resume=True)"

from ultralytics import RTDETR

from ultralytics import YOLO

if __name__ == '__main__':
    model = YOLO("yolov8m.pt")
    model.train(
        data=r"C:\project\pid\pid\app\data_yolo_node\processed\finetune\dataset\data.yaml",
        epochs=100,
        batch=4,
        imgsz=1280,
        lr0=0.001,
        patience=25,
        device=0,
        workers=2,
        project=r"result_yolo_node\experiments",
        name="yolov8m_morph_v1",
        exist_ok=True,
    )