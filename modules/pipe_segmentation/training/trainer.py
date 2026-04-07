"""
Trainer для обучения модели сегментации труб.

Поддерживает:
- Gradient accumulation
- Mixed precision (AMP)
- Learning rate scheduling
- Early stopping
- Checkpointing
- CSV logging
"""

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR, ReduceLROnPlateau
import numpy as np
from pathlib import Path
from typing import Dict, Optional, Tuple, Callable
import time
import gc
from tqdm import tqdm

from pipe_segmentation.model.metrics import MetricsTracker
from pipe_segmentation.model.losses import UltimateLoss
from pipe_segmentation.model.architecture import (
    get_parameter_groups,
    freeze_encoder,
    unfreeze_encoder,
    save_checkpoint,
)
from pipe_segmentation.training.callbacks import (
    EarlyStopping,
    CSVLogger,
    CheckpointCallback,
    print_epoch_summary,
)
from pipe_segmentation.utils.io import save_json, ensure_dir
from pipe_segmentation.utils.visualization import plot_training_curves


def train_one_epoch(
    model: nn.Module,
    dataloader: DataLoader,
    criterion: nn.Module,
    optimizer: optim.Optimizer,
    device: str,
    epoch: int,
    accumulation_steps: int = 1,
    gradient_clip: float = 0.0,
    use_amp: bool = True
) -> Tuple[float, Dict[str, float]]:
    """
    Обучение одной эпохи.
    
    Args:
        model: Модель
        dataloader: Train DataLoader
        criterion: Loss функция
        optimizer: Optimizer
        device: Устройство
        epoch: Номер эпохи
        accumulation_steps: Шаги аккумуляции градиента
        gradient_clip: Клиппинг градиентов (0 = отключен)
        use_amp: Использовать AMP
        
    Returns:
        (avg_loss, metrics_dict)
    """
    model.train()
    running_loss = 0.0
    metrics_tracker = MetricsTracker()
    
    scaler = torch.amp.GradScaler('cuda') if use_amp and device == 'cuda' else None
    
    pbar = tqdm(dataloader, desc=f"Epoch {epoch} [Train]", leave=False)
    optimizer.zero_grad()
    
    for batch_idx, batch in enumerate(pbar):
        images = batch['image'].to(device)
        masks = batch['mask'].to(device)
        
        # Forward pass
        with torch.amp.autocast('cuda', enabled=(use_amp and device == 'cuda')):
            outputs = model(images)
            
            # Dual-head: outputs = (mask_logits, skeleton_logits)
            if isinstance(outputs, tuple):
                mask_outputs, skel_outputs = outputs
                skeletons = batch['skeleton'].to(device)
                loss = criterion(mask_outputs, skel_outputs, masks, skeletons)
                outputs = mask_outputs  # для метрик
            else:
                loss = criterion(outputs, masks)
            
            loss = loss / accumulation_steps
        
        # Backward pass
        if scaler:
            scaler.scale(loss).backward()
        else:
            loss.backward()
        
        # Optimizer step (с аккумуляцией)
        if (batch_idx + 1) % accumulation_steps == 0 or (batch_idx + 1) == len(dataloader):
            if gradient_clip > 0:
                if scaler:
                    scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clip)
            
            if scaler:
                scaler.step(optimizer)
                scaler.update()
            else:
                optimizer.step()
            
            optimizer.zero_grad()
        
        # Накапливаем метрики
        running_loss += loss.item() * accumulation_steps
        metrics_tracker.update(outputs, masks)
        
        pbar.set_postfix({'loss': f'{loss.item() * accumulation_steps:.4f}'})
    
    avg_loss = running_loss / len(dataloader)
    metrics = metrics_tracker.compute()
    metrics['loss'] = avg_loss
    
    return avg_loss, metrics


@torch.no_grad()
def validate_one_epoch(
    model: nn.Module,
    dataloader: DataLoader,
    criterion: nn.Module,
    device: str,
    epoch: int,
    use_amp: bool = True
) -> Tuple[float, Dict[str, float]]:
    """
    Валидация одной эпохи.
    
    Args:
        model: Модель
        dataloader: Val DataLoader
        criterion: Loss функция
        device: Устройство
        epoch: Номер эпохи
        use_amp: Использовать AMP
        
    Returns:
        (avg_loss, metrics_dict)
    """
    model.eval()
    running_loss = 0.0
    metrics_tracker = MetricsTracker()
    
    pbar = tqdm(dataloader, desc=f"Epoch {epoch} [Val]", leave=False)
    
    for batch in pbar:
        images = batch['image'].to(device)
        masks = batch['mask'].to(device)
        
        with torch.amp.autocast('cuda', enabled=(use_amp and device == 'cuda')):
            outputs = model(images)
            
            # Dual-head: model.eval() returns only mask_logits
            # But criterion may be MultiTaskLoss — handle both cases
            if isinstance(outputs, tuple):
                mask_outputs, skel_outputs = outputs
                skeletons = batch['skeleton'].to(device)
                loss = criterion(mask_outputs, skel_outputs, masks, skeletons)
                outputs = mask_outputs
            else:
                # Single-head or eval mode of DualHeadModel
                try:
                    loss = criterion(outputs, masks)
                except TypeError:
                    # MultiTaskLoss but single output — use mask_loss only
                    loss = criterion.mask_loss(outputs, masks)
        
        running_loss += loss.item()
        metrics_tracker.update(outputs, masks)
        
        pbar.set_postfix({'loss': f'{loss.item():.4f}'})
    
    avg_loss = running_loss / len(dataloader)
    metrics = metrics_tracker.compute()
    metrics['loss'] = avg_loss
    
    return avg_loss, metrics


class Trainer:
    """
    Trainer для обучения модели сегментации.
    
    Пример:
        trainer = Trainer(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            output_dir='./output',
            epochs=50
        )
        history = trainer.train()
    """
    
    def __init__(
        self,
        model: nn.Module,
        train_loader: DataLoader,
        val_loader: DataLoader,
        output_dir: str,
        epochs: int = 50,
        batch_size: int = 2,
        accumulation_steps: int = 4,
        encoder_lr: float = 1e-4,
        decoder_lr: float = 1e-3,
        weight_decay: float = 1e-4,
        patience: int = 15,
        gradient_clip: float = 1.0,
        device: str = 'cuda',
        use_amp: bool = True,
        criterion: Optional[nn.Module] = None,
        freeze_encoder_epochs: int = 0,
        scheduler_type: str = 'cosine',
        scheduler_factor: float = 0.5,
        scheduler_patience: int = 5,
        scheduler_min_lr: float = 1e-6,
    ):
        """
        Args:
            model: Модель для обучения
            train_loader: Train DataLoader
            val_loader: Val DataLoader
            output_dir: Директория для результатов
            epochs: Количество эпох
            batch_size: Размер батча
            accumulation_steps: Шаги аккумуляции градиента
            encoder_lr: LR для encoder
            decoder_lr: LR для decoder
            weight_decay: Weight decay
            patience: Patience для early stopping
            gradient_clip: Gradient clipping
            device: Устройство
            use_amp: Использовать AMP
            criterion: Loss функция (если None — используется UltimateLoss)
            freeze_encoder_epochs: Заморозить encoder на N первых эпох
            scheduler_type: Тип scheduler ('cosine' или 'plateau')
            scheduler_factor: Factor для ReduceLROnPlateau
            scheduler_patience: Patience для ReduceLROnPlateau
            scheduler_min_lr: Минимальный LR
        """
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.output_dir = Path(output_dir)
        self.epochs = epochs
        self.batch_size = batch_size
        self.accumulation_steps = accumulation_steps
        self.encoder_lr = encoder_lr
        self.decoder_lr = decoder_lr
        self.weight_decay = weight_decay
        self.patience = patience
        self.gradient_clip = gradient_clip
        self.device = device
        self.use_amp = use_amp and device == 'cuda'
        self.freeze_encoder_epochs = freeze_encoder_epochs
        self.scheduler_type = scheduler_type
        
        # Создаём директории
        self.checkpoint_dir = ensure_dir(self.output_dir / 'checkpoints')
        self.plots_dir = ensure_dir(self.output_dir / 'plots')
        
        # Модель на устройство
        self.model.to(device)
        
        # Loss
        self.criterion = criterion or self._create_default_criterion()
        
        # Optimizer
        self.optimizer = self._create_optimizer()
        
        # [FIX 9.1] Scheduler из конфига вместо hardcoded CosineAnnealing
        if scheduler_type == 'plateau':
            self.scheduler = ReduceLROnPlateau(
                self.optimizer,
                mode='max',
                factor=scheduler_factor,
                patience=scheduler_patience,
                min_lr=scheduler_min_lr,
                verbose=True
            )
        else:  # 'cosine'
            self.scheduler = CosineAnnealingLR(
                self.optimizer, T_max=epochs, eta_min=scheduler_min_lr
            )
        
        # Callbacks
        self.early_stopping = EarlyStopping(patience=patience, mode='max')
        self.checkpoint_callback = CheckpointCallback(
            self.checkpoint_dir,
            monitor='val_dice',
            mode='max'
        )
        
        # CSV Logger
        self.csv_logger = CSVLogger(
            self.output_dir / 'training_stats.csv',
            fieldnames=[
                'epoch', 'train_loss', 'val_loss',
                'train_dice', 'val_dice', 'val_iou',
                'val_precision', 'val_recall', 'val_f1',
                'lr_encoder', 'lr_decoder', 'epoch_time_sec'
            ]
        )
        
        # История
        self.history = {
            'train_loss': [], 'val_loss': [],
            'train_dice': [], 'val_dice': [],
            'train_iou': [], 'val_iou': [],
            'val_precision': [], 'val_recall': [], 'val_f1': []
        }
    
    def _create_default_criterion(self) -> nn.Module:
        """
        Создаёт Loss v3:
        - UltimateLoss с clCE (MICCAI 2024) вместо clDice
        - 10 итераций скелетонизации
        - Если dual_head: оборачивает в MultiTaskLoss
        """
        from pipe_segmentation.model.losses import UltimateLoss, MultiTaskLoss

        pos_weight = self._calculate_pos_weight()

        base_loss = UltimateLoss(
            dice_weight=1.0,
            focal_weight=1.0,
            clce_weight=0.7,        # [v3] увеличен для топологии
            cldice_weight=0.5,
            focal_pos_weight=pos_weight,
            cldice_iters=10,         # [v3] 5→10
            ohem_enabled=True,
            ohem_top_k_ratio=0.5,
            use_clce=True,           # [v3] clCE вместо clDice
        )

        # Dual-head: добавляем skeleton loss
        is_dual = hasattr(self.model, 'skeleton_head')
        if is_dual:
            print("  [DualHead] Using MultiTaskLoss (mask + skeleton)")
            return MultiTaskLoss(
                mask_loss=base_loss,
                skeleton_weight=0.5,
                skeleton_pos_weight=20.0,
            )

        return base_loss
    
    def _calculate_pos_weight(self, max_samples: int = 100) -> float:
        """Вычисляет pos_weight из данных."""
        print("\nCalculating pos_weight...")
        
        pos_pixels = 0
        total_pixels = 0
        
        dataset = self.train_loader.dataset
        n_samples = min(max_samples, len(dataset))
        
        for i in range(n_samples):
            sample = dataset[i]
            mask = sample['mask']
            pos_pixels += mask.sum().item()
            total_pixels += mask.numel()
        
        neg_pixels = total_pixels - pos_pixels
        pos_weight = neg_pixels / pos_pixels if pos_pixels > 0 else 1.0
        # [FIX 4.2] Увеличен clamp 20→50
        from pipe_segmentation.config.defaults import POS_WEIGHT_MAX_CLAMP
        pos_weight = max(min(pos_weight, POS_WEIGHT_MAX_CLAMP), 1.0)
        
        print(f"  Positive pixels: {100 * pos_pixels / total_pixels:.2f}%")
        print(f"  pos_weight: {pos_weight:.2f}")
        
        return pos_weight
    
    def _create_optimizer(self) -> optim.Optimizer:
        """Создаёт optimizer с разными LR для encoder/decoder."""
        param_groups = get_parameter_groups(
            self.model,
            self.encoder_lr,
            self.decoder_lr
        )
        
        for pg in param_groups:
            pg['weight_decay'] = self.weight_decay
        
        return optim.AdamW(param_groups)
    
    def train(self) -> Dict:
        """
        Запускает обучение.
        
        Returns:
            История обучения
        """
        print("\n" + "=" * 70)
        print("TRAINING START")
        print("=" * 70)
        print(f"Epochs: {self.epochs}")
        print(f"Batch size: {self.batch_size} (effective: {self.batch_size * self.accumulation_steps})")
        print(f"Encoder LR: {self.encoder_lr}, Decoder LR: {self.decoder_lr}")
        print(f"Patience: {self.patience}")
        print(f"Device: {self.device}")
        print(f"AMP: {self.use_amp}")
        # [FIX 9.2] Логирование типа scheduler
        print(f"Scheduler: {self.scheduler_type} ({type(self.scheduler).__name__})")
        if self.freeze_encoder_epochs > 0:
            print(f"Freeze encoder: {self.freeze_encoder_epochs} epochs")
        print("=" * 70)
        
        total_time = 0
        
        # Замораживаем encoder если нужно
        if self.freeze_encoder_epochs > 0:
            freeze_encoder(self.model)
        
        for epoch in range(1, self.epochs + 1):
            epoch_start = time.time()
            
            # Размораживаем encoder после freeze_encoder_epochs
            if self.freeze_encoder_epochs > 0 and epoch == self.freeze_encoder_epochs + 1:
                print(f"\n[Epoch {epoch}] Unfreezing encoder...")
                unfreeze_encoder(self.model)
            
            # Train
            train_loss, train_metrics = train_one_epoch(
                self.model, self.train_loader, self.criterion,
                self.optimizer, self.device, epoch,
                self.accumulation_steps, self.gradient_clip, self.use_amp
            )
            
            # Validate
            val_loss, val_metrics = validate_one_epoch(
                self.model, self.val_loader, self.criterion,
                self.device, epoch, self.use_amp
            )
            
            # Scheduler step
            # [FIX 9.1] ReduceLROnPlateau needs metric, CosineAnnealing does not
            if self.scheduler_type == 'plateau':
                self.scheduler.step(val_metrics['dice'])
            else:
                self.scheduler.step()
            
            epoch_time = time.time() - epoch_start
            total_time += epoch_time
            
            # Получаем текущий LR
            lr_encoder = self.optimizer.param_groups[0]['lr']
            lr_decoder = self.optimizer.param_groups[1]['lr'] if len(self.optimizer.param_groups) > 1 else lr_encoder
            
            # Логируем в историю
            self.history['train_loss'].append(train_loss)
            self.history['val_loss'].append(val_loss)
            self.history['train_dice'].append(train_metrics['dice'])
            self.history['val_dice'].append(val_metrics['dice'])
            self.history['train_iou'].append(train_metrics['iou'])
            self.history['val_iou'].append(val_metrics['iou'])
            self.history['val_precision'].append(val_metrics['precision'])
            self.history['val_recall'].append(val_metrics['recall'])
            self.history['val_f1'].append(val_metrics['f1'])
            
            # CSV log
            self.csv_logger.log({
                'epoch': epoch,
                'train_loss': train_loss,
                'val_loss': val_loss,
                'train_dice': train_metrics['dice'],
                'val_dice': val_metrics['dice'],
                'val_iou': val_metrics['iou'],
                'val_precision': val_metrics['precision'],
                'val_recall': val_metrics['recall'],
                'val_f1': val_metrics['f1'],
                'lr_encoder': lr_encoder,
                'lr_decoder': lr_decoder,
                'epoch_time_sec': epoch_time
            })
            
            # Print summary
            print_epoch_summary(
                epoch, self.epochs,
                train_metrics, val_metrics,
                lr_encoder, epoch_time
            )
            
            # Checkpoint
            val_metrics['val_dice'] = val_metrics['dice']
            self.checkpoint_callback(
                self.model, self.optimizer, epoch, val_metrics,
                self.scheduler
            )
            
            # Early stopping
            if self.early_stopping(val_metrics['dice'], epoch):
                print(f"\n⚠ Early stopping at epoch {epoch}")
                break
            
            # Очистка памяти
            gc.collect()
            if self.device == 'cuda':
                torch.cuda.empty_cache()
        
        # Закрываем CSV
        self.csv_logger.close()
        
        # Сохраняем графики
        plot_training_curves(
            self.history,
            self.plots_dir / 'training_curves.png'
        )
        
        # Сохраняем историю
        save_json(self.history, self.output_dir / 'history.json')
        
        # Финальное summary
        print("\n" + "=" * 70)
        print("TRAINING COMPLETE")
        print("=" * 70)
        print(f"Total time: {total_time / 60:.1f} minutes")
        print(f"Best dice: {self.early_stopping.best_score:.4f} (epoch {self.early_stopping.best_epoch})")
        print(f"\nSaved:")
        print(f"  Checkpoints: {self.checkpoint_dir}")
        print(f"  Stats: {self.output_dir / 'training_stats.csv'}")
        print(f"  Plots: {self.plots_dir / 'training_curves.png'}")
        print("=" * 70)
        
        return self.history
