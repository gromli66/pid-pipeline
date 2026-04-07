"""
YOLO Trainer для P&ID Node Detection.

НАЗНАЧЕНИЕ:
----------
Этот модуль обеспечивает обучение и дообучение YOLOv8 моделей
для детекции узлов на P&ID схемах.

ОСОБЕННОСТИ:
-----------
1. Использует YOLOv8m (medium) как базовую модель
2. Увеличенные loss weights для box (7.5) - важна точная локализация
3. Отключены встроенные аугментации YOLO (уже применены в пайплайне)
4. Поддержка finetune с загрузкой существующих весов

ПАРАМЕТРЫ ОБУЧЕНИЯ:
------------------
- epochs: 100 (train), 50 (finetune)
- batch_size: 4 (для RTX 4070 12GB и img_size=1280)
- img_size: 1280 (соответствует tile_size)
- optimizer: AdamW с lr0=0.001
- patience: 25 (early stopping)

ИСПОЛЬЗОВАНИЕ:
-------------
    from pid_node_detection.training import YOLOTrainer

    trainer = YOLOTrainer(
        model="yolov8m.pt",
        epochs=100,
        batch_size=4,
        img_size=1280
    )

    # Обучение с нуля
    results = trainer.train(
        data_yaml=Path("./dataset/data.yaml"),
        project=Path("./experiments"),
        name="train_v1"
    )

    # Дообучение
    results = trainer.finetune(
        weights=Path("./experiments/train_v1/weights/best.pt"),
        data_yaml=Path("./dataset_merged/data.yaml"),
        project=Path("./experiments"),
        name="finetune_v1"
    )
"""

import yaml
from pathlib import Path
from typing import Dict, Optional, Any, Union
from datetime import datetime


class YOLOTrainer:
    """
    Обертка над ultralytics YOLO для обучения и дообучения.

    Attributes:
        model: Базовая модель или путь к весам
        epochs: Количество эпох
        batch_size: Размер батча
        img_size: Размер входного изображения
        patience: Эпох без улучшения для early stopping
        device: GPU устройство
    """

    def __init__(
        self,
        config = None,
        model: str = "yolov8m.pt",
        epochs: int = 100,
        batch_size: int = 4,
        img_size: int = 1280,
        patience: int = 25,
        optimizer: str = "AdamW",
        lr0: float = 0.001,
        lrf: float = 0.01,
        weight_decay: float = 0.0005,
        loss_weights: Optional[Dict[str, float]] = None,
        device: Union[int, str] = 0,
        save_period: int = 10
    ):
        """
        Args:
            config: Config объект (если передан, другие параметры игнорируются)
            model: Базовая модель YOLO (yolov8n/s/m/l/x.pt) или путь к весам
            epochs: Количество эпох обучения
            batch_size: Размер батча
            img_size: Размер входного изображения
            patience: Эпох без улучшения для early stopping
            optimizer: Оптимизатор (SGD, Adam, AdamW)
            lr0: Начальный learning rate
            lrf: Финальный learning rate (множитель от lr0)
            weight_decay: Регуляризация L2
            loss_weights: Веса лоссов {"box": 7.5, "cls": 0.5, "dfl": 1.5}
            device: GPU устройство (0, 1, ... или "cpu")
            save_period: Сохранять чекпоинт каждые N эпох
        """
        # Если передан config, извлекаем параметры из него
        if config is not None and hasattr(config, 'training'):
            training = config.training
            self.model = getattr(training, 'model', model)
            self.epochs = getattr(training, 'epochs', epochs)
            self.batch_size = getattr(training, 'batch_size', batch_size)
            self.img_size = getattr(training, 'img_size', img_size)
            self.patience = getattr(training, 'patience', patience)
            self.optimizer = getattr(training, 'optimizer', optimizer)
            self.lr0 = getattr(training, 'lr0', lr0)
            self.lrf = getattr(training, 'lrf', lrf)
            self.weight_decay = getattr(training, 'weight_decay', weight_decay)
            self.device = getattr(training, 'device', device)
            self.save_period = getattr(training, 'save_period', save_period)

            # Loss weights
            if hasattr(training, 'loss_weights'):
                lw = training.loss_weights
                self.loss_weights = {
                    "box": getattr(lw, 'box', 7.5),
                    "cls": getattr(lw, 'cls', 0.5),
                    "dfl": getattr(lw, 'dfl', 1.5)
                }
            else:
                self.loss_weights = loss_weights or {"box": 7.5, "cls": 0.5, "dfl": 1.5}
        else:
            # Используем переданные параметры напрямую
            self.model = model
            self.epochs = epochs
            self.batch_size = batch_size
            self.img_size = img_size
            self.patience = patience
            self.optimizer = optimizer
            self.lr0 = lr0
            self.lrf = lrf
            self.weight_decay = weight_decay
            self.loss_weights = loss_weights or {"box": 7.5, "cls": 0.5, "dfl": 1.5}
            self.device = device
            self.save_period = save_period

        # Встроенные аугментации YOLO
        # По умолчанию отключены, но можно включить через конфиг
        self.augmentation_params = {
            "degrees": 0.0,
            "translate": 0.0,
            "scale": 0.0,
            "flipud": 0.0,
            "fliplr": 0.0,
            "mosaic": 0.0,
            "mixup": 0.0,
            "copy_paste": 0.0,
            "hsv_h": 0.0,
            "hsv_s": 0.0,
            "hsv_v": 0.0
        }

        # Перезаписать из конфига если builtin_augmentation включены
        if config is not None and hasattr(config, 'training'):
            ba = getattr(config.training, 'builtin_augmentation', None)
            if ba is not None and getattr(ba, 'enabled', False):
                for key in self.augmentation_params:
                    if hasattr(ba, key):
                        self.augmentation_params[key] = float(getattr(ba, key))

    def _build_train_args(
        self,
        data_yaml: Path,
        project: Path,
        name: str,
        resume: bool = False,
        pretrained_weights: Optional[Path] = None
    ) -> Dict[str, Any]:
        """
        Сформировать аргументы для YOLO train.

        Args:
            data_yaml: Путь к data.yaml
            project: Директория проекта
            name: Имя эксперимента
            resume: Продолжить прерванное обучение
            pretrained_weights: Веса для инициализации (finetune)

        Returns:
            Словарь аргументов для model.train()
        """
        args = {
            "data": str(data_yaml),
            "epochs": self.epochs,
            "batch": self.batch_size,
            "imgsz": self.img_size,
            "patience": self.patience,
            "device": self.device,
            "project": str(project),
            "name": name,
            "exist_ok": True,
            "pretrained": True,
            "optimizer": self.optimizer,
            "lr0": self.lr0,
            "lrf": self.lrf,
            "weight_decay": self.weight_decay,
            "box": self.loss_weights["box"],
            "cls": self.loss_weights["cls"],
            "dfl": self.loss_weights["dfl"],
            "save_period": self.save_period,
            "resume": resume,
            "verbose": True,
            "plots": True,
            "save": True,
            "val": True
        }

        # Добавить параметры аугментации
        args.update(self.augmentation_params)

        return args

    def train(
        self,
        data_yaml: Path,
        project: Path,
        name: Optional[str] = None,
        resume: bool = False
    ) -> Dict:
        """
        Обучение модели с нуля.

        Args:
            data_yaml: Путь к data.yaml с конфигурацией датасета
            project: Директория для сохранения результатов
            name: Имя эксперимента (по умолчанию генерируется)
            resume: Продолжить прерванное обучение

        Returns:
            Результаты обучения

        Example:
            >>> trainer = YOLOTrainer()
            >>> results = trainer.train(
            ...     data_yaml=Path("./dataset/data.yaml"),
            ...     project=Path("./experiments")
            ... )
        """
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError(
                "ultralytics не установлен. Установите: pip install ultralytics"
            )

        data_yaml = Path(data_yaml)
        project = Path(project)

        if name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"train_{timestamp}"

        print("\n" + "="*60)
        print("ОБУЧЕНИЕ YOLO")
        print("="*60)
        print(f"Модель: {self.model}")
        print(f"Data: {data_yaml}")
        print(f"Epochs: {self.epochs}")
        print(f"Batch size: {self.batch_size}")
        print(f"Image size: {self.img_size}")
        print(f"Device: {self.device}")
        print(f"Project: {project / name}")
        print()

        # Создать модель
        model = YOLO(self.model)

        # Аргументы обучения
        train_args = self._build_train_args(
            data_yaml=data_yaml,
            project=project,
            name=name,
            resume=resume
        )

        # Запустить обучение
        results = model.train(**train_args)

        # Путь к лучшим весам
        best_weights = project / name / "weights" / "best.pt"

        print(f"\n=== Обучение завершено ===")
        print(f"Лучшие веса: {best_weights}")

        return {
            "results": results,
            "best_weights": best_weights,
            "experiment_dir": project / name
        }

    def finetune(
        self,
        weights: Path,
        data_yaml: Path,
        project: Path,
        name: Optional[str] = None,
        epochs: Optional[int] = None,
        lr0: Optional[float] = None,
        patience: Optional[int] = None
    ) -> Dict:
        """
        Дообучение существующей модели на новых данных.

        Args:
            weights: Путь к весам существующей модели
            data_yaml: Путь к data.yaml нового/объединенного датасета
            project: Директория для сохранения результатов
            name: Имя эксперимента
            epochs: Количество эпох (по умолчанию из config.finetune.epochs)
            lr0: Learning rate (по умолчанию из config.finetune.lr0)
            patience: Early stopping patience (по умолчанию из config.finetune.patience)

        Returns:
            Результаты дообучения
        """
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError(
                "ultralytics не установлен. Установите: pip install ultralytics"
            )

        weights = Path(weights)
        data_yaml = Path(data_yaml)
        project = Path(project)

        if not weights.exists():
            raise FileNotFoundError(f"Веса не найдены: {weights}")

        if name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"finetune_{timestamp}"

        # Параметры для finetune
        finetune_epochs = epochs or self.epochs
        finetune_lr = lr0 or self.lr0
        finetune_patience = patience or self.patience

        print("\n" + "="*60)
        print("ДООБУЧЕНИЕ YOLO")
        print("="*60)
        print(f"Веса: {weights}")
        print(f"Data: {data_yaml}")
        print(f"Epochs: {finetune_epochs}")
        print(f"Learning rate: {finetune_lr}")
        print(f"Patience: {finetune_patience}")
        print(f"Project: {project / name}")
        print()

        # Загрузить модель с весами
        model = YOLO(str(weights))

        # Изменить параметры для finetune
        original_epochs = self.epochs
        original_lr = self.lr0
        original_patience = self.patience

        self.epochs = finetune_epochs
        self.lr0 = finetune_lr
        self.patience = finetune_patience

        # Аргументы обучения
        train_args = self._build_train_args(
            data_yaml=data_yaml,
            project=project,
            name=name
        )

        # Восстановить параметры
        self.epochs = original_epochs
        self.lr0 = original_lr
        self.patience = original_patience

        # Запустить дообучение
        results = model.train(**train_args)

        best_weights = project / name / "weights" / "best.pt"

        print(f"\n=== Дообучение завершено ===")
        print(f"Лучшие веса: {best_weights}")

        return {
            "results": results,
            "best_weights": best_weights,
            "experiment_dir": project / name,
            "original_weights": weights
        }

    def validate(
        self,
        weights: Path,
        data_yaml: Path,
        split: str = "val"
    ) -> Dict:
        """
        Валидация модели на указанном сплите.

        Args:
            weights: Путь к весам модели
            data_yaml: Путь к data.yaml
            split: Сплит для валидации ("val" или "test")

        Returns:
            Метрики валидации
        """
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError(
                "ultralytics не установлен. Установите: pip install ultralytics"
            )

        weights = Path(weights)

        print(f"\nВалидация на {split}...")

        model = YOLO(str(weights))

        results = model.val(
            data=str(data_yaml),
            split=split,
            imgsz=self.img_size,
            batch=self.batch_size,
            device=self.device,
            verbose=True
        )

        return {
            "map50": results.box.map50,
            "map50_95": results.box.map,
            "precision": results.box.mp,
            "recall": results.box.mr,
            "results": results
        }


def create_data_yaml(
    dataset_dir: Path,
    num_classes: int,
    class_names: Dict[int, str],
    train_path: str = "train/images",
    val_path: str = "val/images",
    output_path: Optional[Path] = None
) -> Path:
    """
    Создать data.yaml для обучения YOLO.

    Args:
        dataset_dir: Корневая директория датасета
        num_classes: Количество классов
        class_names: Маппинг id -> имя класса
        train_path: Относительный путь к train изображениям
        val_path: Относительный путь к val изображениям
        output_path: Путь для сохранения (по умолчанию dataset_dir/data.yaml)

    Returns:
        Путь к созданному файлу
    """
    dataset_dir = Path(dataset_dir)

    if output_path is None:
        output_path = dataset_dir / "data.yaml"

    data = {
        "path": str(dataset_dir.absolute()),
        "train": train_path,
        "val": val_path,
        "nc": num_classes,
        "names": class_names
    }

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False, allow_unicode=True)

    print(f"Создан data.yaml: {output_path}")

    return output_path