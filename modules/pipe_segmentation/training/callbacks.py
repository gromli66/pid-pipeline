"""
Callbacks для обучения: Early Stopping, CSV Logger, Checkpointing.
"""

import csv
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime


class EarlyStopping:
    """
    Early Stopping callback.
    
    Останавливает обучение если метрика не улучшается patience эпох подряд.
    
    Пример:
        early_stopping = EarlyStopping(patience=10, mode='max')
        for epoch in range(epochs):
            ...
            if early_stopping(val_dice):
                print("Early stopping!")
                break
    """
    
    def __init__(
        self,
        patience: int = 10,
        mode: str = 'max',
        min_delta: float = 0.0,
        verbose: bool = True
    ):
        """
        Args:
            patience: Сколько эпох ждать без улучшения
            mode: 'max' (dice, iou) или 'min' (loss)
            min_delta: Минимальное изменение для улучшения
            verbose: Выводить сообщения
        """
        self.patience = patience
        self.mode = mode
        self.min_delta = min_delta
        self.verbose = verbose
        
        self.counter = 0
        self.best_score = None
        self.should_stop = False
        self.best_epoch = 0
    
    def __call__(self, score: float, epoch: int = 0) -> bool:
        """
        Проверяет, нужно ли останавливать обучение.
        
        Args:
            score: Текущее значение метрики
            epoch: Номер эпохи
            
        Returns:
            True если нужно остановить обучение
        """
        if self.best_score is None:
            self.best_score = score
            self.best_epoch = epoch
            return False
        
        if self.mode == 'max':
            improved = score > self.best_score + self.min_delta
        else:
            improved = score < self.best_score - self.min_delta
        
        if improved:
            self.best_score = score
            self.best_epoch = epoch
            self.counter = 0
        else:
            self.counter += 1
            if self.verbose:
                print(f"  EarlyStopping: {self.counter}/{self.patience}")
            
            if self.counter >= self.patience:
                self.should_stop = True
                return True
        
        return False
    
    def reset(self):
        """Сброс состояния."""
        self.counter = 0
        self.best_score = None
        self.should_stop = False
        self.best_epoch = 0


class CSVLogger:
    """
    Логгер метрик в CSV файл.
    
    Пример:
        logger = CSVLogger('training_stats.csv')
        logger.log({'epoch': 1, 'loss': 0.5, 'dice': 0.8})
        logger.close()
    """
    
    def __init__(
        self,
        filepath: str,
        fieldnames: Optional[List[str]] = None,
        append: bool = False
    ):
        """
        Args:
            filepath: Путь к CSV файлу
            fieldnames: Имена колонок (определяются автоматически из первой записи)
            append: Дописывать в существующий файл
        """
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        
        self.fieldnames = fieldnames
        self.file = None
        self.writer = None
        self.append = append
        self._initialized = False
    
    def _init_writer(self, row: Dict):
        """Инициализирует writer с fieldnames из первой записи."""
        if self.fieldnames is None:
            self.fieldnames = list(row.keys())
        
        mode = 'a' if self.append and self.filepath.exists() else 'w'
        self.file = open(self.filepath, mode, newline='', encoding='utf-8')
        self.writer = csv.DictWriter(self.file, fieldnames=self.fieldnames)
        
        # Записываем заголовок если новый файл
        if mode == 'w':
            self.writer.writeheader()
        
        self._initialized = True
    
    def log(self, row: Dict[str, Any]):
        """
        Записывает строку в CSV.
        
        Args:
            row: Словарь с данными
        """
        if not self._initialized:
            self._init_writer(row)
        
        # Округляем float значения
        formatted_row = {}
        for key, value in row.items():
            if key in self.fieldnames:
                if isinstance(value, float):
                    formatted_row[key] = f"{value:.6f}"
                else:
                    formatted_row[key] = value
        
        self.writer.writerow(formatted_row)
        self.file.flush()
    
    def close(self):
        """Закрывает файл."""
        if self.file:
            self.file.close()
            self.file = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class CheckpointCallback:
    """
    Callback для сохранения чекпоинтов.
    
    Сохраняет лучшую модель и опционально последнюю.
    
    Пример:
        checkpoint = CheckpointCallback('./checkpoints', monitor='val_dice')
        checkpoint(model, optimizer, epoch, {'val_dice': 0.9})
    """
    
    def __init__(
        self,
        checkpoint_dir: str,
        monitor: str = 'val_dice',
        mode: str = 'max',
        save_last: bool = True,
        verbose: bool = True
    ):
        """
        Args:
            checkpoint_dir: Директория для чекпоинтов
            monitor: Метрика для отслеживания
            mode: 'max' или 'min'
            save_last: Сохранять последний чекпоинт
            verbose: Выводить сообщения
        """
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        
        self.monitor = monitor
        self.mode = mode
        self.save_last = save_last
        self.verbose = verbose
        
        self.best_score = None
        self.best_epoch = 0
    
    def __call__(
        self,
        model,
        optimizer,
        epoch: int,
        metrics: Dict[str, float],
        scheduler=None,
        **extra_data
    ) -> bool:
        """
        Проверяет и сохраняет чекпоинт.
        
        Args:
            model: Модель
            optimizer: Optimizer
            epoch: Номер эпохи
            metrics: Словарь с метриками
            scheduler: Learning rate scheduler (опционально)
            **extra_data: Дополнительные данные для сохранения
            
        Returns:
            True если сохранён лучший чекпоинт
        """
        import torch
        
        score = metrics.get(self.monitor)
        if score is None:
            return False
        
        is_best = False
        
        if self.best_score is None:
            is_best = True
        elif self.mode == 'max' and score > self.best_score:
            is_best = True
        elif self.mode == 'min' and score < self.best_score:
            is_best = True
        
        # Сохраняем лучший
        if is_best:
            self.best_score = score
            self.best_epoch = epoch
            
            checkpoint = {
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'metrics': metrics,
                self.monitor: score,
                **extra_data
            }
            
            if scheduler is not None:
                checkpoint['scheduler_state_dict'] = scheduler.state_dict()
            
            best_path = self.checkpoint_dir / 'best_model.pth'
            torch.save(checkpoint, best_path)
            
            if self.verbose:
                print(f"  ✓ New best {self.monitor}: {score:.4f} — Saved!")
        
        # Сохраняем последний
        if self.save_last:
            checkpoint = {
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'metrics': metrics,
                **extra_data
            }
            
            if scheduler is not None:
                checkpoint['scheduler_state_dict'] = scheduler.state_dict()
            
            last_path = self.checkpoint_dir / 'last_model.pth'
            torch.save(checkpoint, last_path)
        
        return is_best


def format_time(seconds: float) -> str:
    """Форматирует время в читаемый вид."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


def print_epoch_summary(
    epoch: int,
    total_epochs: int,
    train_metrics: Dict[str, float],
    val_metrics: Dict[str, float],
    lr: float,
    epoch_time: float
):
    """
    Выводит summary эпохи.
    
    Args:
        epoch: Номер эпохи
        total_epochs: Всего эпох
        train_metrics: Метрики на train
        val_metrics: Метрики на val
        lr: Learning rate
        epoch_time: Время эпохи в секундах
    """
    print(f"\nEpoch {epoch}/{total_epochs} ({format_time(epoch_time)})")
    print(f"  LR: {lr:.2e}")
    
    train_loss = train_metrics.get('loss', 0)
    train_dice = train_metrics.get('dice', 0)
    print(f"  Train: loss={train_loss:.4f}, dice={train_dice:.4f}")
    
    val_loss = val_metrics.get('loss', 0)
    val_dice = val_metrics.get('dice', 0)
    val_iou = val_metrics.get('iou', 0)
    print(f"  Val: loss={val_loss:.4f}, dice={val_dice:.4f}, iou={val_iou:.4f}")
