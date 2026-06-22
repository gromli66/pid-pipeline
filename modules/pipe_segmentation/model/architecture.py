"""
Архитектура модели: U-Net++ с 4-канальным входом.

Использует EfficientNet-B3 как encoder с pretrained весами ImageNet.
4-й канал (node_mask) инициализируется нулями + малый шум.

[FIX 5.1] 4-й канал: нули + шум вместо mean(RGB)
[NEW] DualHeadModel — mask + skeleton для multi-task learning
"""

import torch
import torch.nn as nn
from pathlib import Path
from typing import Optional, Dict, List

import segmentation_models_pytorch as smp

from pipe_segmentation.config.defaults import DEFAULT_MODEL_CONFIG


class DualHeadModel(nn.Module):
    """
    Обёртка над UNet++ с двумя головами: mask + skeleton.

    Использует общий encoder + decoder, добавляет вторую
    segmentation head (1×1 conv) для предсказания скелета.

    При инференсе возвращает только маску (backward compatible).
    При обучении возвращает (mask_logits, skeleton_logits).
    """

    def __init__(self, base_model: nn.Module):
        super().__init__()
        self.base_model = base_model

        # Skeleton head: копируем структуру основной segmentation head
        # В SMP модель имеет self.segmentation_head — это Sequential
        seg_head = base_model.segmentation_head
        # Обычно: Conv2d(decoder_channels, classes, 1) + Identity + Activation
        # Создаём аналогичную голову для скелета
        in_channels = seg_head[0].in_channels
        self.skeleton_head = nn.Sequential(
            nn.Conv2d(in_channels, 1, kernel_size=1),
        )

        # Инициализация Xavier
        nn.init.xavier_uniform_(self.skeleton_head[0].weight)
        nn.init.zeros_(self.skeleton_head[0].bias)

    def forward(self, x: torch.Tensor):
        """
        Forward pass.

        При training: возвращает (mask_logits, skeleton_logits)
        При eval: возвращает только mask_logits (backward compatible)
        """
        # Encoder
        features = self.base_model.encoder(x)

        # Decoder — expects single argument (list of feature maps)
        decoder_output = self.base_model.decoder(features)

        # Mask head (основная)
        mask_logits = self.base_model.segmentation_head(decoder_output)

        if self.training:
            # Skeleton head (вторая)
            skeleton_logits = self.skeleton_head(decoder_output)
            return mask_logits, skeleton_logits
        else:
            return mask_logits


def create_model(
    architecture: str = 'UnetPlusPlus',
    encoder_name: str = 'efficientnet-b3',
    encoder_weights: Optional[str] = 'imagenet',
    in_channels: int = 4,
    classes: int = 1,
    activation: Optional[str] = None,
    decoder_attention_type: Optional[str] = None,
    dual_head: bool = False,
    verbose: bool = True
) -> nn.Module:
    """
    Создаёт модель U-Net++ с 4-канальным входом.

    Args:
        dual_head: Если True, создаёт DualHeadModel (mask + skeleton).
                   При training возвращает (mask, skeleton), при eval — только mask.
    
    Алгоритм для 4 каналов:
    1. Создаём модель с 3 каналами (загружаем ImageNet веса)
    2. Создаём модель с 4 каналами (random init)
    3. Копируем все веса кроме первого conv слоя
    4. Расширяем первый conv слой: 4-й канал = mean(RGB weights)
    
    Args:
        architecture: Архитектура модели ('UnetPlusPlus', 'Unet', 'FPN', etc.)
        encoder_name: Название encoder backbone
        encoder_weights: Pretrained веса ('imagenet' или None)
        in_channels: Количество входных каналов (3 или 4)
        classes: Количество выходных классов
        activation: Активация на выходе (None = logits)
        decoder_attention_type: Тип attention в decoder
        verbose: Выводить информацию
        
    Returns:
        PyTorch модель
    """
    if verbose:
        print("\n" + "=" * 60)
        print("CREATING MODEL")
        print("=" * 60)
        print(f"Architecture: {architecture}")
        print(f"Encoder: {encoder_name}")
        print(f"Encoder weights: {encoder_weights}")
        print(f"Input channels: {in_channels}")
        print(f"Output classes: {classes}")
    
    # Стандартная модель с 3 каналами
    if in_channels == 3:
        model = _create_base_model(
            architecture, encoder_name, encoder_weights,
            in_channels, classes, activation, decoder_attention_type
        )
        _print_model_summary(model, verbose)
        
        if dual_head:
            if verbose:
                print("\n[Wrapping in DualHeadModel: mask + skeleton heads]")
            model = DualHeadModel(model)
            if verbose:
                skel_params = sum(p.numel() for p in model.skeleton_head.parameters())
                print(f"  Skeleton head params: {skel_params:,}")
        
        return model
    
    # 4-канальная модель
    if in_channels == 4:
        if verbose:
            print("\n[Creating 4-channel model]")
        
        # Шаг 1: Модель с 3 каналами (pretrained)
        if verbose:
            print("  [1/4] Creating 3-channel model with pretrained weights...")
        model_3ch = _create_base_model(
            architecture, encoder_name, encoder_weights,
            3, classes, activation, decoder_attention_type
        )
        
        # Шаг 2: Модель с 4 каналами (random init)
        if verbose:
            print("  [2/4] Creating 4-channel model (random init)...")
        model_4ch = _create_base_model(
            architecture, encoder_name, None,  # No pretrained
            4, classes, activation, decoder_attention_type
        )
        
        # Шаг 3: Копируем веса (кроме первого conv)
        if verbose:
            print("  [3/4] Copying pretrained encoder weights...")
        _copy_encoder_weights(model_4ch, model_3ch)
        
        # Шаг 4: Расширяем первый conv слой
        if verbose:
            print("  [4/4] Extending first conv layer to 4 channels...")
        _extend_first_conv(model_4ch, model_3ch, verbose)
        
        _print_model_summary(model_4ch, verbose)
        
        if dual_head:
            if verbose:
                print("\n[Wrapping in DualHeadModel: mask + skeleton heads]")
            model_4ch = DualHeadModel(model_4ch)
            if verbose:
                skel_params = sum(p.numel() for p in model_4ch.skeleton_head.parameters())
                print(f"  Skeleton head params: {skel_params:,}")
        
        return model_4ch
    
    raise ValueError(f"Unsupported in_channels: {in_channels}. Use 3 or 4.")


def _create_base_model(
    architecture: str,
    encoder_name: str,
    encoder_weights: Optional[str],
    in_channels: int,
    classes: int,
    activation: Optional[str],
    decoder_attention_type: Optional[str]
) -> nn.Module:
    """Создаёт базовую SMP модель."""
    model_class = getattr(smp, architecture)
    
    return model_class(
        encoder_name=encoder_name,
        encoder_weights=encoder_weights,
        in_channels=in_channels,
        classes=classes,
        activation=activation,
        decoder_attention_type=decoder_attention_type
    )


def _copy_encoder_weights(model_4ch: nn.Module, model_3ch: nn.Module):
    """Копирует веса encoder'а (кроме первого conv слоя)."""
    pretrained_dict = model_3ch.state_dict()
    model_dict = model_4ch.state_dict()
    
    for name, param in pretrained_dict.items():
        if name in model_dict:
            # Пропускаем первый conv (разные размеры)
            if 'encoder' in name and 'conv' in name and param.shape[1] == 3:
                if model_dict[name].shape[1] == 4:
                    continue
            model_dict[name] = param
    
    model_4ch.load_state_dict(model_dict)


def _extend_first_conv(model_4ch: nn.Module, model_3ch: nn.Module, verbose: bool = True):
    """Расширяет первый conv слой до 4 каналов."""
    # Находим первый conv слой в encoder'е
    first_conv_name = None
    first_conv_layer = None
    
    for name, module in model_3ch.encoder.named_modules():
        if isinstance(module, nn.Conv2d) and module.in_channels == 3:
            first_conv_name = name
            first_conv_layer = module
            break
    
    if first_conv_layer is None:
        if verbose:
            print("  Warning: Could not find first conv layer")
        return
    
    if verbose:
        print(f"    Found: encoder.{first_conv_name}")
        print(f"    Original shape: {first_conv_layer.weight.shape}")
    
    # Получаем pretrained веса [out_channels, 3, kernel_h, kernel_w]
    pretrained_weight = first_conv_layer.weight.data
    out_channels = pretrained_weight.shape[0]
    kernel_size = pretrained_weight.shape[2:]
    
    # [FIX 5.1] 4-й канал: нули + малый шум
    # mean(RGB) даёт ненулевой отклик на пустую node-маску (все нули),
    # что создаёт фантомный сигнал когда маска узлов отсутствует.
    # Нули + шум позволяют каналу корректно стартовать в обоих сценариях.
    fourth_channel = torch.zeros_like(pretrained_weight.mean(dim=1, keepdim=True))
    fourth_channel += torch.randn_like(fourth_channel) * 0.01
    
    # Конкатенируем [out_channels, 4, kernel_h, kernel_w]
    new_weight = torch.cat([pretrained_weight, fourth_channel], dim=1)
    
    # Находим и заменяем в 4-канальной модели
    for name, module in model_4ch.encoder.named_modules():
        if isinstance(module, nn.Conv2d) and module.in_channels == 4:
            if name == first_conv_name:
                module.weight.data = new_weight
                if first_conv_layer.bias is not None and module.bias is not None:
                    module.bias.data = first_conv_layer.bias.data
                
                if verbose:
                    print(f"    Extended to: {module.weight.shape}")
                    print("    ✓ First conv extended successfully!")
                return
    
    if verbose:
        print("  Warning: Could not find matching conv in 4-channel model")


def _print_model_summary(model: nn.Module, verbose: bool):
    """Выводит информацию о модели."""
    if not verbose:
        return
    
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    
    print("\n" + "=" * 60)
    print("MODEL SUMMARY")
    print("=" * 60)
    print(f"Total parameters: {total_params:,}")
    print(f"Trainable parameters: {trainable_params:,}")
    print(f"Model size: {total_params * 4 / 1024 / 1024:.2f} MB (float32)")
    print("=" * 60)


def freeze_encoder(model: nn.Module, verbose: bool = True):
    """Замораживает веса encoder'а."""
    for name, param in model.named_parameters():
        if 'encoder' in name:
            param.requires_grad = False
    
    if verbose:
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print(f"Encoder frozen. Trainable parameters: {trainable:,}")


def unfreeze_encoder(model: nn.Module, verbose: bool = True):
    """Размораживает веса encoder'а."""
    for param in model.parameters():
        param.requires_grad = True
    
    if verbose:
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print(f"Encoder unfrozen. Trainable parameters: {trainable:,}")


def get_parameter_groups(
    model: nn.Module,
    encoder_lr: float,
    decoder_lr: float
) -> List[Dict]:
    """
    Создаёт группы параметров с разными learning rate.
    
    Args:
        model: Модель
        encoder_lr: LR для encoder (меньше, т.к. pretrained)
        decoder_lr: LR для decoder (больше, т.к. random init)
        
    Returns:
        Список групп для optimizer
    """
    encoder_params = []
    decoder_params = []
    
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        
        if 'encoder' in name:
            encoder_params.append(param)
        else:
            # decoder + segmentation_head + skeleton_head
            decoder_params.append(param)
    
    return [
        {'params': encoder_params, 'lr': encoder_lr, 'name': 'encoder'},
        {'params': decoder_params, 'lr': decoder_lr, 'name': 'decoder'}
    ]


def save_checkpoint(
    model: nn.Module,
    optimizer,
    epoch: int,
    metrics: Dict,
    path: str,
    **kwargs
):
    """
    Сохраняет чекпоинт.
    
    Args:
        model: Модель
        optimizer: Optimizer
        epoch: Номер эпохи
        metrics: Словарь с метриками
        path: Путь для сохранения
        **kwargs: Дополнительные данные
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    checkpoint = {
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict() if optimizer else None,
        'metrics': metrics,
        **kwargs
    }
    
    torch.save(checkpoint, path)


def load_checkpoint(
    path: str,
    model: nn.Module,
    optimizer=None,
    device: str = 'cuda',
    verbose: bool = True
) -> Dict:
    """
    Загружает чекпоинт.
    
    Args:
        path: Путь к чекпоинту
        model: Модель для загрузки весов
        optimizer: Optimizer (опционально)
        device: Устройство
        verbose: Выводить информацию
        
    Returns:
        Словарь с данными чекпоинта
    """
    path = Path(path)
    
    if not path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {path}")
    
    checkpoint = torch.load(path, map_location=device, weights_only=False)
    
    # Загружаем веса модели (с поддержкой single→dual head миграции)
    try:
        model.load_state_dict(checkpoint['model_state_dict'])
    except RuntimeError:
        # Single-head checkpoint → dual-head model: load partial
        model_dict = model.state_dict()
        pretrained = checkpoint['model_state_dict']
        # Загружаем только совпадающие ключи
        matched = {k: v for k, v in pretrained.items()
                   if k in model_dict and v.shape == model_dict[k].shape}
        model_dict.update(matched)
        model.load_state_dict(model_dict)
        if verbose:
            missing = set(model_dict.keys()) - set(matched.keys())
            if missing:
                print(f"  Initialized from scratch: {len(missing)} layers "
                      f"(skeleton_head, etc.)")
    model.to(device)
    model.eval()
    
    # Загружаем optimizer если указан
    if optimizer and checkpoint.get('optimizer_state_dict'):
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    
    if verbose:
        print(f"\nCheckpoint loaded: {path}")
        if 'epoch' in checkpoint:
            print(f"  Epoch: {checkpoint['epoch']}")
        if 'metrics' in checkpoint:
            metrics = checkpoint['metrics']
            if isinstance(metrics, dict):
                for key, val in metrics.items():
                    if isinstance(val, (int, float)):
                        print(f"  {key}: {val:.4f}")
    
    return checkpoint
