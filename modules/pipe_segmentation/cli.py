"""
CLI интерфейс для pipe_segmentation.

Основной способ работы — через YAML конфиг:
    python -m pipe_segmentation prepare --config my_config.yaml
    python -m pipe_segmentation train --config my_config.yaml

CLI параметры переопределяют значения из конфига.

Важно:
- test/infer работают на ОРИГИНАЛЬНЫХ изображениях (тайлинг внутри)
- finetune автоматически делает prepare под капотом
"""

import click
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from pipe_segmentation.config.config_loader import Config, load_config


def apply_cli_overrides(config: Config, **kwargs) -> Config:
    """Применяет CLI параметры поверх конфига."""
    
    # Маппинг CLI -> config path
    mapping = {
        # paths
        'images': 'paths.raw_images',
        'coco': 'paths.coco_annotations',
        'pipe_masks': 'paths.masks.pipes',
        'node_masks': 'paths.masks.nodes',
        'output': 'paths.dataset_dir',
        'data': 'paths.dataset_dir',
        'checkpoint': 'inference.checkpoint',
        
        # preprocessing
        'binarize': 'preprocessing.binarization.enabled',
        'binarize_method': 'preprocessing.binarization.method',
        
        # tiling
        'tile_size': 'tiling.tile_size',
        'overlap': 'tiling.overlap',
        'min_pipe_pixels': 'tiling.filtering.min_pipe_pixels',
        
        # splitting
        'test_files': 'splitting.test_files',
        'seed': 'splitting.random_seed',
        
        # training
        'epochs': 'training.epochs',
        'batch_size': 'training.batch_size',
        'encoder_lr': 'training.learning_rate.encoder',
        'decoder_lr': 'training.learning_rate.decoder',
        'accumulation_steps': 'training.accumulation_steps',
        'patience': 'training.patience',
        'device': 'training.device',
        
        # finetune specific
        'lr': 'finetune.learning_rate',
        'freeze_encoder': 'finetune.freeze_encoder_epochs',
        
        # inference
        'threshold': 'inference.threshold',
        'tta': 'inference.tta.enabled',
        'postprocess': 'inference.postprocessing.enabled',
        'save_overlay': 'inference.save_overlay',
    }
    
    for cli_key, config_path in mapping.items():
        if cli_key in kwargs and kwargs[cli_key] is not None:
            config.set(config_path, kwargs[cli_key])
    
    # Обработка split (специальный случай)
    if 'split' in kwargs and kwargs['split']:
        parts = [float(x) for x in kwargs['split'].split('/')]
        if len(parts) == 3:
            config.set('splitting.ratios.train', parts[0])
            config.set('splitting.ratios.val', parts[1])
            config.set('splitting.ratios.test', parts[2])
        elif len(parts) == 2:
            config.set('splitting.ratios_finetune.train', parts[0])
            config.set('splitting.ratios_finetune.val', parts[1])
    
    return config


def run_prepare_internal(cfg: Config, output_dir: Path, verbose: bool = True):
    """
    Внутренняя функция prepare (используется в finetune).
    
    Returns:
        Path к подготовленному dataset
    """
    from pipe_segmentation.data.coco_parser import COCOParser
    from pipe_segmentation.data.tiling import ImageTiler
    from pipe_segmentation.utils.io import load_file_list, ensure_dir, save_json
    from pipe_segmentation.config.defaults import MASKS_PIPES_DIR, MASKS_NODES_DIR
    
    images_dir = cfg.get('paths.raw_images')
    coco_path = cfg.get('paths.coco_annotations')
    pipe_masks_dir = cfg.get('paths.masks.pipes')
    node_masks_dir = cfg.get('paths.masks.nodes')
    
    tile_size = cfg.get('tiling.tile_size')
    overlap = cfg.get('tiling.overlap')
    min_pipe_pixels = cfg.get('tiling.filtering.min_pipe_pixels')
    
    binarize = cfg.get('preprocessing.binarization.enabled')
    binarize_method = cfg.get('preprocessing.binarization.method')
    
    train_ratio = cfg.get('splitting.ratios_finetune.train', 0.8)
    val_ratio = cfg.get('splitting.ratios_finetune.val', 0.2)
    seed = cfg.get('splitting.random_seed')
    
    if verbose:
        print(f"\nImages: {images_dir}")
        print(f"Output: {output_dir}")
        print(f"Tile size: {tile_size}, Overlap: {overlap}")
        print(f"Split: {train_ratio}/{val_ratio}")
    
    # Определяем источник масок
    if coco_path:
        if verbose:
            print(f"COCO: {coco_path}")
        parser = COCOParser(coco_path)
        masks_dir = ensure_dir(output_dir / 'masks')
        parser.extract_masks(masks_dir, verbose=verbose)
        actual_pipe_masks = masks_dir / MASKS_PIPES_DIR
        actual_node_masks = masks_dir / MASKS_NODES_DIR
    elif pipe_masks_dir:
        if verbose:
            print(f"Pipe masks: {pipe_masks_dir}")
        actual_pipe_masks = Path(pipe_masks_dir)
        actual_node_masks = Path(node_masks_dir) if node_masks_dir else None
        if actual_node_masks is None:
            actual_node_masks = ensure_dir(output_dir / 'masks' / MASKS_NODES_DIR)
    else:
        raise ValueError("Укажите paths.coco_annotations или paths.masks.pipes")
    
    # Тайлинг (только train/val, без test)
    split_ratios = (train_ratio, val_ratio, 0.0)
    
    tiler = ImageTiler(
        images_dir=images_dir,
        pipe_masks_dir=actual_pipe_masks,
        node_masks_dir=actual_node_masks,
        output_dir=output_dir,
        tile_size=tile_size,
        overlap=overlap,
        min_pipe_pixels=min_pipe_pixels,
        binarize=binarize,
        binarize_method=binarize_method
    )
    
    results = tiler.process(
        split_ratios=split_ratios,
        test_files=None,
        seed=seed,
        verbose=verbose
    )
    
    save_json(cfg.to_dict(), output_dir / 'prepare_config.json')
    
    return output_dir


@click.group()
@click.version_option(version='1.1.0')
def cli():
    """
    Pipe Segmentation — сегментация труб на P&ID схемах.
    
    \b
    Использование с конфигом (рекомендуется):
      python -m pipe_segmentation prepare --config my_config.yaml
      python -m pipe_segmentation train --config my_config.yaml
    
    \b
    CLI параметры переопределяют значения из конфига:
      python -m pipe_segmentation train --config my_config.yaml --epochs 100
    
    \b
    Команды:
      prepare   Подготовка данных (маски → split → тайлы)
      train     Обучение модели с нуля
      finetune  Fine-tuning на новых данных (prepare автоматически)
      test      Тестирование на ОРИГИНАЛЬНЫХ изображениях
      infer     Инференс на ОРИГИНАЛЬНЫХ изображениях
    """
    pass


# =============================================================================
# PREPARE COMMAND
# =============================================================================

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None,
              help='YAML конфиг (если не указан — используется default.yaml)')
@click.option('--images', type=click.Path(exists=True), default=None,
              help='Папка с изображениями (переопределяет paths.raw_images)')
@click.option('--coco', type=click.Path(exists=True), default=None,
              help='COCO JSON (переопределяет paths.coco_annotations)')
@click.option('--pipe-masks', type=click.Path(exists=True), default=None,
              help='Папка с масками труб (переопределяет paths.masks.pipes)')
@click.option('--node-masks', type=click.Path(exists=True), default=None,
              help='Папка с масками узлов (переопределяет paths.masks.nodes)')
@click.option('--output', type=click.Path(), default=None,
              help='Выходная папка (переопределяет paths.dataset_dir)')
@click.option('--tile-size', type=int, default=None,
              help='Размер тайла (переопределяет tiling.tile_size)')
@click.option('--overlap', type=int, default=None,
              help='Перекрытие (переопределяет tiling.overlap)')
@click.option('--split', type=str, default=None,
              help='Пропорции split, например "0.7/0.15/0.15"')
@click.option('--test-files', type=click.Path(exists=True), default=None,
              help='Файл со списком тестовых изображений')
@click.option('--binarize/--no-binarize', default=None,
              help='Бинаризовать изображения')
@click.option('--binarize-method', type=click.Choice(['adaptive', 'otsu', 'fixed']), default=None,
              help='Метод бинаризации')
@click.option('--seed', type=int, default=None,
              help='Random seed')
def prepare(config, **kwargs):
    """
    Подготовка данных: маски → split → тайлы.
    
    \b
    Примеры:
      # С конфигом (все пути в конфиге)
      python -m pipe_segmentation prepare --config my_config.yaml
      
      # С переопределением путей
      python -m pipe_segmentation prepare --config my_config.yaml \\
        --images ./other/images --output ./other/dataset
    """
    from pipe_segmentation.data.coco_parser import COCOParser
    from pipe_segmentation.data.tiling import ImageTiler
    from pipe_segmentation.utils.io import load_file_list, ensure_dir, save_json
    from pipe_segmentation.config.defaults import MASKS_PIPES_DIR, MASKS_NODES_DIR
    
    # Загружаем конфиг
    cfg = load_config(config)
    cfg = apply_cli_overrides(cfg, **kwargs)
    
    print("=" * 70)
    print("PIPE SEGMENTATION — PREPARE")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Config: {config or 'default'}")
    
    # Получаем параметры из конфига
    images_dir = cfg.get('paths.raw_images')
    coco_path = cfg.get('paths.coco_annotations')
    pipe_masks_dir = cfg.get('paths.masks.pipes')
    node_masks_dir = cfg.get('paths.masks.nodes')
    output_dir = Path(cfg.get('paths.dataset_dir'))
    
    tile_size = cfg.get('tiling.tile_size')
    overlap = cfg.get('tiling.overlap')
    min_pipe_pixels = cfg.get('tiling.filtering.min_pipe_pixels')
    
    binarize = cfg.get('preprocessing.binarization.enabled')
    binarize_method = cfg.get('preprocessing.binarization.method')
    
    train_ratio = cfg.get('splitting.ratios.train')
    val_ratio = cfg.get('splitting.ratios.val')
    test_ratio = cfg.get('splitting.ratios.test')
    test_files_path = cfg.get('splitting.test_files')
    seed = cfg.get('splitting.random_seed')
    
    print(f"\nImages: {images_dir}")
    print(f"Output: {output_dir}")
    print(f"Tile size: {tile_size}, Overlap: {overlap}")
    print(f"Binarize: {binarize} ({binarize_method})")
    print(f"Split: {train_ratio}/{val_ratio}/{test_ratio}")
    
    # Определяем источник масок
    if coco_path:
        print(f"\nMode: COCO → masks → tiles")
        print(f"COCO: {coco_path}")
        
        print("\n" + "=" * 70)
        print("STEP 1: EXTRACTING MASKS FROM COCO")
        print("=" * 70)
        
        parser = COCOParser(coco_path)
        masks_dir = ensure_dir(output_dir / 'masks')
        parser.extract_masks(masks_dir, verbose=True)
        
        actual_pipe_masks = masks_dir / MASKS_PIPES_DIR
        actual_node_masks = masks_dir / MASKS_NODES_DIR
    elif pipe_masks_dir:
        print(f"\nMode: Ready masks → tiles")
        print(f"Pipe masks: {pipe_masks_dir}")
        print(f"Node masks: {node_masks_dir or 'None (zeros)'}")
        
        actual_pipe_masks = Path(pipe_masks_dir)
        actual_node_masks = Path(node_masks_dir) if node_masks_dir else None
        
        if actual_node_masks is None:
            actual_node_masks = ensure_dir(output_dir / 'masks' / MASKS_NODES_DIR)
    else:
        raise click.UsageError(
            "Укажите paths.coco_annotations или paths.masks.pipes в конфиге"
        )
    
    # Тайлинг
    print("\n" + "=" * 70)
    print("STEP 2: TILING IMAGES")
    print("=" * 70)
    
    test_file_list = None
    if test_files_path:
        test_file_list = load_file_list(test_files_path)
        print(f"Loaded {len(test_file_list)} test files")
    
    split_ratios = (train_ratio, val_ratio, test_ratio)
    
    tiler = ImageTiler(
        images_dir=images_dir,
        pipe_masks_dir=actual_pipe_masks,
        node_masks_dir=actual_node_masks,
        output_dir=output_dir,
        tile_size=tile_size,
        overlap=overlap,
        min_pipe_pixels=min_pipe_pixels,
        binarize=binarize,
        binarize_method=binarize_method
    )
    
    results = tiler.process(
        split_ratios=split_ratios,
        test_files=test_file_list,
        seed=seed,
        verbose=True
    )
    
    # Сохраняем конфигурацию
    save_json(cfg.to_dict(), output_dir / 'prepare_config.json')
    
    print("\n" + "=" * 70)
    print("✓ PREPARE COMPLETE")
    print("=" * 70)
    print(f"Output: {output_dir}")


# =============================================================================
# TRAIN COMMAND
# =============================================================================

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None,
              help='YAML конфиг')
@click.option('--data', type=click.Path(exists=True), default=None,
              help='Папка с dataset (переопределяет paths.dataset_dir)')
@click.option('--output', type=click.Path(), default=None,
              help='Папка для результатов (переопределяет paths.experiments_dir)')
@click.option('--epochs', type=int, default=None,
              help='Количество эпох')
@click.option('--batch-size', type=int, default=None,
              help='Размер батча')
@click.option('--encoder-lr', type=float, default=None,
              help='Learning rate для encoder')
@click.option('--decoder-lr', type=float, default=None,
              help='Learning rate для decoder')
@click.option('--patience', type=int, default=None,
              help='Early stopping patience')
@click.option('--device', type=str, default=None,
              help='Устройство (cuda/cpu)')
@click.option('--seed', type=int, default=None,
              help='Random seed')
def train(config, **kwargs):
    """
    Обучение модели с нуля.
    
    \b
    Примеры:
      python -m pipe_segmentation train --config my_config.yaml
      python -m pipe_segmentation train --config my_config.yaml --epochs 100
    """
    import torch
    import random
    import numpy as np
    
    from pipe_segmentation.model.architecture import create_model
    from pipe_segmentation.data.dataset import create_dataloaders
    from pipe_segmentation.training.trainer import Trainer
    from pipe_segmentation.utils.io import save_json, ensure_dir
    
    # Загружаем конфиг
    cfg = load_config(config)
    cfg = apply_cli_overrides(cfg, **kwargs)
    
    # Параметры
    data_dir = kwargs.get('data') or cfg.get('paths.dataset_dir')
    output_dir = kwargs.get('output') or cfg.get('paths.experiments_dir')
    epochs = cfg.get('training.epochs')
    batch_size = cfg.get('training.batch_size')
    accumulation_steps = cfg.get('training.accumulation_steps')
    encoder_lr = cfg.get('training.learning_rate.encoder')
    decoder_lr = cfg.get('training.learning_rate.decoder')
    weight_decay = cfg.get('training.weight_decay')
    patience = cfg.get('training.patience')
    device = cfg.get('training.device')
    seed = cfg.get('training.random_seed')
    
    # Set seed
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    
    print("=" * 70)
    print("PIPE SEGMENTATION — TRAIN")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Config: {config or 'default'}")
    print(f"Device: {device}")
    if device == 'cuda' and torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    
    output_dir = ensure_dir(output_dir)
    
    # DataLoaders
    num_workers = cfg.get('training.num_workers', 0)
    print(f"\nLoading data... (num_workers={num_workers})")
    train_loader, val_loader, _ = create_dataloaders(
        data_dir, batch_size=batch_size, num_workers=num_workers, use_augmentations=True
    )
    print(f"Train: {len(train_loader.dataset)} samples")
    print(f"Val: {len(val_loader.dataset)} samples")
    
    # Model — читаем параметры из конфига
    print("\nCreating model...")
    dual_head = cfg.get('model.dual_head', False)
    decoder_attention = cfg.get('model.decoder_attention_type', None)
    model = create_model(
        decoder_attention_type=decoder_attention,
        dual_head=dual_head,
        verbose=True,
    )
    
    # Loss — читаем параметры из конфига
    from pipe_segmentation.model.losses import UltimateLoss, MultiTaskLoss
    
    use_clce = cfg.get('loss.use_clce', True)
    cldice_iters = cfg.get('loss.cldice.iterations', 10)
    
    base_loss = UltimateLoss(
        dice_weight=cfg.get('loss.weights.dice', 1.0),
        focal_weight=cfg.get('loss.weights.focal', 1.0),
        cldice_weight=cfg.get('loss.weights.cldice', 0.5),
        clce_weight=cfg.get('loss.clce_weight', 0.7),
        focal_alpha=cfg.get('loss.focal.alpha', 0.25),
        focal_gamma=cfg.get('loss.focal.gamma', 2.0),
        focal_pos_weight=None,  # auto-calculated by trainer
        cldice_iters=cldice_iters,
        ohem_enabled=cfg.get('loss.ohem.enabled', True),
        ohem_top_k_ratio=cfg.get('loss.ohem.top_k_ratio', 0.5),
        use_clce=use_clce,
    )
    
    if dual_head:
        criterion = MultiTaskLoss(
            mask_loss=base_loss,
            skeleton_weight=cfg.get('loss.skeleton_weight', 0.5),
            skeleton_pos_weight=cfg.get('loss.skeleton_pos_weight', 20.0),
        )
        print(f"\nLoss: MultiTask (mask: Dice+Focal+{'clCE' if use_clce else 'clDice'}"
              f"({cldice_iters}it) + skeleton BCE)")
    else:
        criterion = base_loss
        print(f"\nLoss: Dice+Focal+{'clCE' if use_clce else 'clDice'}({cldice_iters}it)")
    
    # Trainer
    trainer = Trainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        output_dir=output_dir,
        epochs=epochs,
        batch_size=batch_size,
        accumulation_steps=accumulation_steps,
        encoder_lr=encoder_lr,
        decoder_lr=decoder_lr,
        weight_decay=weight_decay,
        patience=patience,
        device=device,
        criterion=criterion,
    )
    
    # Сохраняем конфиг
    save_json(cfg.to_dict(), output_dir / 'training_config.json')
    
    # Train
    history = trainer.train()


# =============================================================================
# FINETUNE COMMAND
# =============================================================================

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None,
              help='YAML конфиг')
@click.option('--images', type=click.Path(exists=True), default=None,
              help='Папка с ОРИГИНАЛЬНЫМИ изображениями для finetune')
@click.option('--pipe-masks', type=click.Path(exists=True), default=None,
              help='Папка с масками труб')
@click.option('--node-masks', type=click.Path(exists=True), default=None,
              help='Папка с масками узлов')
@click.option('--coco', type=click.Path(exists=True), default=None,
              help='COCO JSON с разметкой')
@click.option('--checkpoint', type=click.Path(exists=True), default=None,
              help='Путь к модели для дообучения')
@click.option('--output', type=click.Path(), default=None,
              help='Папка для результатов')
@click.option('--epochs', type=int, default=None,
              help='Количество эпох')
@click.option('--lr', type=float, default=None,
              help='Learning rate')
@click.option('--freeze-encoder', type=int, default=None,
              help='Заморозить encoder на N эпох')
@click.option('--patience', type=int, default=None,
              help='Early stopping patience')
@click.option('--split', type=str, default=None,
              help='Пропорции train/val, например "0.8/0.2"')
@click.option('--device', type=str, default=None,
              help='Устройство')
def finetune(config, **kwargs):
    """
    Fine-tuning модели на новых данных.
    
    Автоматически выполняет prepare (тайлинг) под капотом.
    Принимает ОРИГИНАЛЬНЫЕ изображения и маски.
    
    \b
    Примеры:
      # С готовыми масками
      python -m pipe_segmentation finetune --config my_config.yaml \\
        --images ./new_data/images \\
        --pipe-masks ./new_data/masks/pipes \\
        --checkpoint ./best_model.pth
      
      # С COCO разметкой
      python -m pipe_segmentation finetune --config my_config.yaml \\
        --images ./new_data/images \\
        --coco ./new_data/annotations.json \\
        --checkpoint ./best_model.pth
    """
    import torch
    import random
    import numpy as np
    import tempfile
    import shutil
    
    from pipe_segmentation.model.architecture import create_model, load_checkpoint
    from pipe_segmentation.data.dataset import create_dataloaders
    from pipe_segmentation.training.trainer import Trainer
    from pipe_segmentation.utils.io import save_json, ensure_dir
    
    # Загружаем конфиг
    cfg = load_config(config)
    cfg = apply_cli_overrides(cfg, **kwargs)
    
    # Параметры
    images_dir = kwargs.get('images') or cfg.get('paths.raw_images')
    pipe_masks_dir = kwargs.get('pipe_masks') or cfg.get('paths.masks.pipes')
    node_masks_dir = kwargs.get('node_masks') or cfg.get('paths.masks.nodes')
    coco_path = kwargs.get('coco') or cfg.get('paths.coco_annotations')
    checkpoint = kwargs.get('checkpoint') or cfg.get('finetune.checkpoint')
    output_dir = kwargs.get('output') or cfg.get('paths.experiments_dir')
    
    epochs = kwargs.get('epochs') or cfg.get('finetune.epochs')
    lr = kwargs.get('lr') or cfg.get('finetune.learning_rate')
    freeze_encoder = kwargs.get('freeze_encoder') or cfg.get('finetune.freeze_encoder_epochs')
    patience = kwargs.get('patience') or cfg.get('finetune.patience')
    device = cfg.get('training.device')
    seed = cfg.get('training.random_seed')
    batch_size = cfg.get('training.batch_size')
    
    if not checkpoint:
        raise click.UsageError("Укажите checkpoint в конфиге или через --checkpoint")
    
    if not images_dir:
        raise click.UsageError("Укажите --images или paths.raw_images в конфиге")
    
    if not pipe_masks_dir and not coco_path:
        raise click.UsageError("Укажите --pipe-masks или --coco")
    
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    
    print("=" * 70)
    print("PIPE SEGMENTATION — FINETUNE")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Checkpoint: {checkpoint}")
    print(f"Device: {device}")
    
    output_dir = Path(output_dir)
    ensure_dir(output_dir)
    
    # =========================================================================
    # STEP 1: Автоматический prepare
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 1: PREPARING DATA (auto)")
    print("=" * 70)
    
    # Временная папка для подготовленных данных
    dataset_dir = output_dir / 'dataset'
    
    # Обновляем конфиг для prepare
    cfg.set('paths.raw_images', str(images_dir))
    if coco_path:
        cfg.set('paths.coco_annotations', str(coco_path))
    if pipe_masks_dir:
        cfg.set('paths.masks.pipes', str(pipe_masks_dir))
    if node_masks_dir:
        cfg.set('paths.masks.nodes', str(node_masks_dir))
    
    # Запускаем prepare
    run_prepare_internal(cfg, dataset_dir, verbose=True)
    
    # =========================================================================
    # STEP 2: Fine-tuning
    # =========================================================================
    print("\n" + "=" * 70)
    print("STEP 2: FINE-TUNING MODEL")
    print("=" * 70)
    
    # Model
    print("\nLoading model...")
    dual_head = cfg.get('model.dual_head', False)
    model = create_model(dual_head=dual_head, verbose=False)
    load_checkpoint(checkpoint, model, device=device, verbose=True)
    
    # DataLoaders
    num_workers = cfg.get('training.num_workers', 0)
    print(f"\nLoading data... (num_workers={num_workers})")
    train_loader, val_loader, _ = create_dataloaders(
        dataset_dir, batch_size=batch_size, num_workers=num_workers, use_augmentations=True
    )
    print(f"Train: {len(train_loader.dataset)} samples")
    print(f"Val: {len(val_loader.dataset)} samples")
    
    # Trainer
    trainer = Trainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        output_dir=output_dir,
        epochs=epochs,
        batch_size=batch_size,
        encoder_lr=lr,
        decoder_lr=lr,
        patience=patience,
        device=device,
        freeze_encoder_epochs=freeze_encoder
    )
    
    save_json(cfg.to_dict(), output_dir / 'finetune_config.json')
    history = trainer.train()
    
    print("\n" + "=" * 70)
    print("✓ FINETUNE COMPLETE")
    print("=" * 70)
    print(f"Output: {output_dir}")


# =============================================================================
# TEST COMMAND
# =============================================================================

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None,
              help='YAML конфиг')
@click.option('--images', type=click.Path(exists=True), default=None,
              help='Папка с ОРИГИНАЛЬНЫМИ тестовыми изображениями')
@click.option('--checkpoint', type=click.Path(exists=True), default=None,
              help='Путь к модели')
@click.option('--output', type=click.Path(), default=None,
              help='Папка для результатов')
@click.option('--coco', type=click.Path(exists=True), default=None,
              help='COCO JSON с GT разметкой')
@click.option('--pipe-masks', type=click.Path(exists=True), default=None,
              help='Папка с GT масками труб')
@click.option('--node-masks', type=click.Path(exists=True), default=None,
              help='Папка с масками узлов (для 4-го канала)')
@click.option('--threshold', type=float, default=None,
              help='Порог бинаризации')
@click.option('--tta/--no-tta', default=None,
              help='Test-Time Augmentation')
@click.option('--postprocess/--no-postprocess', default=None,
              help='Постобработка маски')
@click.option('--device', type=str, default=None,
              help='Устройство')
def test(config, **kwargs):
    """
    Тестирование модели на ОРИГИНАЛЬНЫХ изображениях.
    
    Тайлинг выполняется автоматически внутри (как SAHI).
    
    \b
    Примеры:
      # С готовыми GT масками
      python -m pipe_segmentation test --config my_config.yaml \\
        --images ./test/images \\
        --pipe-masks ./test/masks/pipes \\
        --checkpoint ./best_model.pth
      
      # С COCO GT
      python -m pipe_segmentation test --config my_config.yaml \\
        --images ./test/images \\
        --coco ./test/annotations.json \\
        --checkpoint ./best_model.pth
    """
    import torch
    from tqdm import tqdm
    
    from pipe_segmentation.model.architecture import create_model, load_checkpoint
    from pipe_segmentation.data.coco_parser import COCOParser
    from pipe_segmentation.inference.engine import TiledInference
    from pipe_segmentation.model.metrics import calculate_metrics, compute_summary_stats
    from pipe_segmentation.utils.io import (
        load_image, save_image, save_json, save_csv,
        get_image_files, ensure_dir, find_matching_file
    )
    from pipe_segmentation.utils.visualization import create_overlay
    
    # Загружаем конфиг
    cfg = load_config(config)
    cfg = apply_cli_overrides(cfg, **kwargs)
    
    # Параметры
    images_dir = kwargs.get('images') or cfg.get('paths.raw_images')
    checkpoint = kwargs.get('checkpoint') or cfg.get('inference.checkpoint')
    output_dir = kwargs.get('output') or cfg.get('paths.inference_output')
    coco_path = kwargs.get('coco') or cfg.get('paths.coco_annotations')
    pipe_masks_dir = kwargs.get('pipe_masks') or cfg.get('paths.masks.pipes')
    node_masks_dir = kwargs.get('node_masks') or cfg.get('paths.masks.nodes')
    
    tile_size = cfg.get('inference.tile_size')
    overlap = cfg.get('inference.overlap')
    batch_size = cfg.get('inference.batch_size')
    threshold = cfg.get('inference.threshold')
    use_tta = cfg.get('inference.tta.enabled')
    postprocess = cfg.get('inference.postprocessing.enabled')
    binarize = cfg.get('inference.binarization.enabled')
    binarize_method = cfg.get('inference.binarization.method')
    device = cfg.get('training.device')
    
    if not checkpoint:
        raise click.UsageError("Укажите checkpoint в конфиге или через --checkpoint")
    
    if not images_dir:
        raise click.UsageError("Укажите --images или paths.raw_images в конфиге")
    
    print("=" * 70)
    print("PIPE SEGMENTATION — TEST")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Images: {images_dir}")
    print(f"Checkpoint: {checkpoint}")
    print(f"Device: {device}")
    print(f"Tile size: {tile_size}, Overlap: {overlap}")
    print(f"TTA: {use_tta}, Postprocess: {postprocess}")
    
    output_dir = Path(output_dir)
    masks_out_dir = ensure_dir(output_dir / 'masks')
    overlays_dir = ensure_dir(output_dir / 'overlays')
    
    # Model
    print("\nLoading model...")
    dual_head = cfg.get('model.dual_head', False)
    model = create_model(dual_head=dual_head, verbose=False)
    load_checkpoint(checkpoint, model, device=device, verbose=True)
    
    # Inference engine (тайлинг внутри)
    inference = TiledInference(
        model=model,
        device=device,
        tile_size=tile_size,
        overlap=overlap,
        batch_size=batch_size,
        threshold=threshold,
        use_tta=use_tta,
        binarize=binarize,
        binarize_method=binarize_method
    )
    
    # GT source
    parser = None
    gt_pipe_masks_dir = None
    
    if coco_path:
        print(f"GT source: COCO ({coco_path})")
        parser = COCOParser(coco_path)
    elif pipe_masks_dir:
        print(f"GT source: masks ({pipe_masks_dir})")
        gt_pipe_masks_dir = Path(pipe_masks_dir)
    else:
        print("GT source: None (metrics will not be calculated)")
    
    # Process ORIGINAL images
    image_files = get_image_files(images_dir)
    print(f"\nImages to test: {len(image_files)}")
    
    results = []
    for img_path in tqdm(image_files, desc="Testing"):
        filename = img_path.name
        
        # Load ORIGINAL image
        image = load_image(img_path, rgb=True)
        if image is None:
            continue
        
        # Get GT masks
        pipe_gt = None
        node_mask = None
        
        if parser:
            pipe_gt, node_mask = parser.get_masks_for_file(filename)
        elif gt_pipe_masks_dir:
            gt_path = find_matching_file(filename, gt_pipe_masks_dir, ['.png'])
            if gt_path:
                pipe_gt = load_image(gt_path, grayscale=True)
            if node_masks_dir:
                node_path = find_matching_file(filename, Path(node_masks_dir), ['.png'])
                if node_path:
                    node_mask = load_image(node_path, grayscale=True)
        
        # Inference (тайлинг внутри)
        result = inference.predict(image, node_mask, postprocess=postprocess)
        pred_mask = result['mask']
        
        # Save results
        save_image(pred_mask, masks_out_dir / filename)
        overlay = create_overlay(image, pred_mask)
        save_image(overlay, overlays_dir / filename, is_rgb=True)
        
        # Metrics
        if pipe_gt is not None:
            metrics = calculate_metrics(pred_mask, pipe_gt, threshold)
        else:
            metrics = {'dice': None, 'iou': None}
        
        metrics['filename'] = filename
        metrics['width'] = image.shape[1]
        metrics['height'] = image.shape[0]
        metrics['n_tiles'] = result['n_tiles']
        metrics['inference_time_sec'] = result['time_sec']
        results.append(metrics)
    
    # Save results
    save_csv(results, output_dir / 'test_results.csv')
    
    results_with_gt = [r for r in results if r.get('dice') is not None]
    if results_with_gt:
        summary = compute_summary_stats(results_with_gt)
        save_json(summary, output_dir / 'test_summary.json')
        
        print("\n" + "=" * 70)
        print("TEST RESULTS")
        print("=" * 70)
        print(f"Images: {len(results)}")
        for metric in ['dice', 'iou', 'precision', 'recall']:
            if metric in summary:
                s = summary[metric]
                print(f"  {metric.capitalize():12s}: {s['mean']:.4f} ± {s['std']:.4f}")
    else:
        print("\n[No GT provided — metrics not calculated]")
    
    save_json(cfg.to_dict(), output_dir / 'test_config.json')
    
    print("\n" + "=" * 70)
    print("✓ TEST COMPLETE")
    print("=" * 70)
    print(f"Output: {output_dir}")


# =============================================================================
# INFER COMMAND
# =============================================================================

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None,
              help='YAML конфиг')
@click.option('--images', type=click.Path(exists=True), default=None,
              help='Папка с ОРИГИНАЛЬНЫМИ изображениями (или один файл)')
@click.option('--checkpoint', type=click.Path(exists=True), default=None,
              help='Путь к модели')
@click.option('--output', type=click.Path(), default=None,
              help='Папка для результатов')
@click.option('--coco', type=click.Path(exists=True), default=None,
              help='COCO JSON с разметкой узлов (извлекает node_mask автоматически)')
@click.option('--node-masks', type=click.Path(exists=True), default=None,
              help='Папка с масками узлов (для 4-го канала, альтернатива --coco)')
@click.option('--save-node-masks/--no-save-node-masks', default=True,
              help='Сохранять маски узлов из COCO (по умолчанию: да)')
@click.option('--threshold', type=float, default=None,
              help='Порог бинаризации')
@click.option('--tta/--no-tta', default=None,
              help='Test-Time Augmentation')
@click.option('--postprocess/--no-postprocess', default=None,
              help='Постобработка маски')
@click.option('--save-overlay/--no-save-overlay', default=None,
              help='Сохранять overlay визуализации')
@click.option('--device', type=str, default=None,
              help='Устройство')
def infer(config, **kwargs):
    """
    Инференс на ОРИГИНАЛЬНЫХ изображениях (без GT).
    
    Тайлинг выполняется автоматически внутри (как SAHI).
    
    \b
    Источники масок узлов (приоритет):
      1. --coco: извлекает маски из COCO JSON (рекомендуется)
      2. --node-masks: готовые маски из папки
    
    \b
    Примеры:
      # С COCO разметкой (извлечёт и сохранит маски узлов)
      python -m pipe_segmentation infer --config my_config.yaml \\
        --images ./new_schemes \\
        --coco ./annotations.json \\
        --checkpoint ./best_model.pth \\
        --save-overlay
      
      # С готовыми масками узлов
      python -m pipe_segmentation infer --config my_config.yaml \\
        --images ./new_schemes \\
        --node-masks ./masks/nodes \\
        --checkpoint ./best_model.pth
    """
    import torch
    from tqdm import tqdm
    
    from pipe_segmentation.model.architecture import create_model, load_checkpoint
    from pipe_segmentation.inference.engine import TiledInference
    from pipe_segmentation.data.coco_parser import COCOParser
    from pipe_segmentation.utils.io import (
        load_image, save_image, save_json, save_csv,
        get_image_files, ensure_dir, find_matching_file
    )
    from pipe_segmentation.utils.visualization import create_overlay
    
    # Загружаем конфиг
    cfg = load_config(config)
    cfg = apply_cli_overrides(cfg, **kwargs)
    
    # Параметры
    images_path = kwargs.get('images') or cfg.get('paths.raw_images')
    checkpoint = kwargs.get('checkpoint') or cfg.get('inference.checkpoint')
    output_dir = kwargs.get('output') or cfg.get('paths.inference_output')
    coco_path = kwargs.get('coco') or cfg.get('paths.coco_annotations')
    node_masks_dir = kwargs.get('node_masks') or cfg.get('paths.masks.nodes')
    save_node_masks = kwargs.get('save_node_masks', True)
    
    tile_size = cfg.get('inference.tile_size')
    overlap = cfg.get('inference.overlap')
    batch_size = cfg.get('inference.batch_size')
    threshold = cfg.get('inference.threshold')
    use_tta = cfg.get('inference.tta.enabled')
    postprocess = cfg.get('inference.postprocessing.enabled')
    binarize = cfg.get('inference.binarization.enabled')
    binarize_method = cfg.get('inference.binarization.method')
    save_overlay = cfg.get('inference.save_overlay')
    device = cfg.get('training.device')
    
    if not checkpoint:
        raise click.UsageError("Укажите checkpoint в конфиге или через --checkpoint")
    
    if not images_path:
        raise click.UsageError("Укажите --images или paths.raw_images в конфиге")
    
    # Определяем источник масок узлов
    coco_parser = None
    node_mask_source = "none"
    
    if coco_path:
        coco_parser = COCOParser(coco_path)
        node_mask_source = "coco"
    elif node_masks_dir:
        node_mask_source = "directory"
    
    print("=" * 70)
    print("PIPE SEGMENTATION — INFER")
    print("=" * 70)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Images: {images_path}")
    print(f"Checkpoint: {checkpoint}")
    print(f"Device: {device}")
    print(f"Tile size: {tile_size}, Overlap: {overlap}")
    print(f"TTA: {use_tta}, Postprocess: {postprocess}")
    print(f"Node masks: {node_mask_source}" + (f" ({coco_path})" if coco_path else ""))
    
    output_dir = Path(output_dir)
    masks_out_dir = ensure_dir(output_dir / 'masks')
    
    # Директория для масок узлов (из COCO)
    node_masks_out_dir = None
    if coco_parser and save_node_masks:
        node_masks_out_dir = ensure_dir(output_dir / 'node_masks')
        print(f"Node masks will be saved to: {node_masks_out_dir}")
    
    if save_overlay:
        overlays_dir = ensure_dir(output_dir / 'overlays')
    
    # Model
    print("\nLoading model...")
    dual_head = cfg.get('model.dual_head', False)
    model = create_model(dual_head=dual_head, verbose=False)
    load_checkpoint(checkpoint, model, device=device, verbose=True)
    
    # Inference engine (тайлинг внутри)
    inference = TiledInference(
        model=model,
        device=device,
        tile_size=tile_size,
        overlap=overlap,
        batch_size=batch_size,
        threshold=threshold,
        use_tta=use_tta,
        binarize=binarize,
        binarize_method=binarize_method
    )
    
    # Get images
    images_path = Path(images_path)
    if images_path.is_file():
        image_files = [images_path]
    else:
        image_files = get_image_files(images_path)
    
    print(f"\nImages to process: {len(image_files)}")
    
    # Process
    results = []
    total_time = 0
    node_masks_saved = 0
    
    for img_path in tqdm(image_files, desc="Inference"):
        filename = img_path.name
        
        # Load ORIGINAL image
        image = load_image(img_path, rgb=True)
        if image is None:
            continue
        
        # Node mask - приоритет: COCO > directory
        node_mask = None
        
        if coco_parser:
            # Получаем маску из COCO
            _, node_mask = coco_parser.get_masks_for_file(filename)
            
            # Сохраняем маску узлов если есть и включено сохранение
            if node_mask is not None and node_masks_out_dir:
                base_name = Path(filename).stem
                save_image(node_mask, node_masks_out_dir / f"{base_name}.png")
                node_masks_saved += 1
        
        elif node_masks_dir:
            # Загружаем готовую маску из директории
            node_path = find_matching_file(filename, Path(node_masks_dir), ['.png'])
            if node_path:
                node_mask = load_image(node_path, grayscale=True)
        
        # Inference (тайлинг внутри)
        result = inference.predict(image, node_mask, postprocess=postprocess)
        pred_mask = result['mask']
        total_time += result['time_sec']
        
        # Save
        save_image(pred_mask, masks_out_dir / filename)
        
        if save_overlay:
            overlay = create_overlay(image, pred_mask)
            save_image(overlay, overlays_dir / filename, is_rgb=True)
        
        results.append({
            'filename': filename,
            'width': image.shape[1],
            'height': image.shape[0],
            'n_tiles': result['n_tiles'],
            'coverage_pct': result['coverage_pct'],
            'time_sec': result['time_sec'],
            'has_node_mask': node_mask is not None
        })
    
    save_csv(results, output_dir / 'inference_log.csv')
    save_json(cfg.to_dict(), output_dir / 'inference_config.json')
    
    print("\n" + "=" * 70)
    print("✓ INFERENCE COMPLETE")
    print("=" * 70)
    print(f"Images: {len(results)}")
    print(f"Total time: {total_time:.1f}s ({total_time/max(len(results),1):.2f}s/image)")
    if coco_parser:
        print(f"Node masks extracted: {node_masks_saved}")
    print(f"Output: {output_dir}")


# =============================================================================
# ENSEMBLE COMMAND
# =============================================================================

@cli.command()
@click.option('--config', type=click.Path(exists=True), default=None,
              help='YAML конфиг')
@click.option('--images', type=click.Path(exists=True), required=True,
              help='Папка с ОРИГИНАЛЬНЫМИ изображениями')
@click.option('--checkpoint-a', type=click.Path(exists=True), required=True,
              help='Чекпоинт модели A (plain UNet++)')
@click.option('--checkpoint-b', type=click.Path(exists=True), required=True,
              help='Чекпоинт модели B (DualHead)')
@click.option('--dual-head-a/--no-dual-head-a', default=False,
              help='Модель A — DualHeadModel?')
@click.option('--dual-head-b/--no-dual-head-b', default=True,
              help='Модель B — DualHeadModel?')
@click.option('--strategy', type=click.Choice(['or', 'and', 'mean', 'weighted_mean']),
              default='or', help='Стратегия ансамбля')
@click.option('--output', type=click.Path(), default=None,
              help='Папка для результатов')
@click.option('--pipe-masks', type=click.Path(exists=True), default=None,
              help='GT маски для метрик (опционально)')
@click.option('--node-masks', type=click.Path(exists=True), default=None,
              help='Маски узлов (4-й канал)')
@click.option('--coco', type=click.Path(exists=True), default=None,
              help='COCO JSON (GT + node masks)')
@click.option('--postprocess/--no-postprocess', default=True,
              help='Постобработка')
@click.option('--save-overlay/--no-save-overlay', default=True,
              help='Сохранять overlay')
def ensemble(config, **kwargs):
    """
    Ансамблевый инференс двух моделей.
    
    Загружает два чекпоинта, прогоняет оба, комбинирует (OR по умолчанию).
    OR даёт +4% Recall при том же Precision.
    
    \b
    Примеры:
      python -m pipe_segmentation ensemble \\
        --images ./test/images \\
        --checkpoint-a ./model_v2.pth \\
        --checkpoint-b ./model_v3.pth --dual-head-b \\
        --pipe-masks ./test/pipe_masks \\
        --strategy or
    """
    import torch
    from tqdm import tqdm
    
    from pipe_segmentation.inference.ensemble import EnsembleInference
    from pipe_segmentation.data.coco_parser import COCOParser
    from pipe_segmentation.model.metrics import calculate_metrics, compute_summary_stats
    from pipe_segmentation.utils.io import (
        load_image, save_image, save_json, save_csv,
        get_image_files, ensure_dir, find_matching_file
    )
    from pipe_segmentation.utils.visualization import create_overlay
    
    cfg = load_config(config)
    cfg = apply_cli_overrides(cfg, **kwargs)
    
    images_dir = kwargs['images']
    checkpoint_a = kwargs['checkpoint_a']
    checkpoint_b = kwargs['checkpoint_b']
    dual_head_a = kwargs.get('dual_head_a', False)
    dual_head_b = kwargs.get('dual_head_b', True)
    strategy = kwargs.get('strategy', 'or')
    output_dir = Path(kwargs.get('output') or cfg.get('paths.inference_output'))
    pipe_masks_dir = kwargs.get('pipe_masks')
    node_masks_dir = kwargs.get('node_masks')
    coco_path = kwargs.get('coco') or cfg.get('paths.coco_annotations')
    postprocess = kwargs.get('postprocess', True)
    save_overlay_flag = kwargs.get('save_overlay', True)
    
    device = cfg.get('training.device', 'cuda')
    tile_size = cfg.get('inference.tile_size', 1024)
    overlap = cfg.get('inference.overlap', 128)
    batch_size = cfg.get('inference.batch_size', 4)
    threshold = cfg.get('inference.threshold', 0.5)
    use_tta = cfg.get('inference.tta.enabled', True)
    binarize = cfg.get('inference.binarization.enabled', True)
    binarize_method = cfg.get('inference.binarization.method', 'adaptive')
    
    # Output dirs
    masks_out = ensure_dir(output_dir / 'masks')
    overlays_out = ensure_dir(output_dir / 'overlays') if save_overlay_flag else None
    masks_a_out = ensure_dir(output_dir / 'masks_a')
    masks_b_out = ensure_dir(output_dir / 'masks_b')
    
    # Ensemble
    ens = EnsembleInference(
        checkpoint_a=checkpoint_a,
        checkpoint_b=checkpoint_b,
        dual_head_a=dual_head_a,
        dual_head_b=dual_head_b,
        device=device,
        tile_size=tile_size,
        overlap=overlap,
        batch_size=batch_size,
        threshold=threshold,
        use_tta=use_tta,
        binarize=binarize,
        binarize_method=binarize_method,
        strategy=strategy,
    )
    
    # GT source
    parser = None
    gt_masks_dir = None
    if coco_path:
        parser = COCOParser(coco_path)
        print(f"GT source: COCO ({coco_path})")
    elif pipe_masks_dir:
        gt_masks_dir = Path(pipe_masks_dir)
        print(f"GT source: masks ({pipe_masks_dir})")
    
    # Process
    image_files = get_image_files(images_dir)
    print(f"\nImages: {len(image_files)}")
    
    results = []
    for img_path in tqdm(image_files, desc="Ensemble"):
        filename = img_path.name
        image = load_image(img_path, rgb=True)
        if image is None:
            continue
        
        # Node mask
        node_mask = None
        if parser:
            _, node_mask = parser.get_masks_for_file(filename)
        elif node_masks_dir:
            np_path = find_matching_file(filename, Path(node_masks_dir), ['.png'])
            if np_path:
                node_mask = load_image(np_path, grayscale=True)
        
        # Ensemble predict
        result = ens.predict(image, node_mask, postprocess=postprocess)
        
        # Save
        save_image(result['mask'], masks_out / filename)
        save_image(result['mask_a'], masks_a_out / filename)
        save_image(result['mask_b'], masks_b_out / filename)
        
        if overlays_out:
            overlay = create_overlay(image, result['mask'])
            save_image(overlay, overlays_out / filename, is_rgb=True)
        
        # GT metrics
        pipe_gt = None
        if parser:
            pipe_gt, _ = parser.get_masks_for_file(filename)
        elif gt_masks_dir:
            gt_path = find_matching_file(filename, gt_masks_dir, ['.png'])
            if gt_path:
                pipe_gt = load_image(gt_path, grayscale=True)
        
        entry = {
            'filename': filename,
            'time_sec': result['time_sec'],
            'coverage_pct': result['coverage_pct'],
            'strategy': strategy,
        }
        
        if pipe_gt is not None:
            met = calculate_metrics(result['mask'], pipe_gt, threshold)
            entry.update(met)
            # Also per-model metrics
            met_a = calculate_metrics(result['mask_a'], pipe_gt, threshold)
            met_b = calculate_metrics(result['mask_b'], pipe_gt, threshold)
            entry['dice_a'] = met_a.get('dice')
            entry['dice_b'] = met_b.get('dice')
        
        results.append(entry)
    
    save_csv(results, output_dir / 'ensemble_results.csv')
    
    results_gt = [r for r in results if r.get('dice') is not None]
    if results_gt:
        summary = compute_summary_stats(results_gt)
        save_json(summary, output_dir / 'ensemble_summary.json')
        
        print("\n" + "=" * 70)
        print(f"ENSEMBLE RESULTS (strategy={strategy})")
        print("=" * 70)
        print(f"Images: {len(results)}")
        for metric in ['dice', 'iou', 'precision', 'recall', 'dice_a', 'dice_b']:
            if metric in summary:
                s = summary[metric]
                label = {'dice_a': 'Dice (model A)', 'dice_b': 'Dice (model B)'}.get(metric, metric.capitalize())
                print(f"  {label:16s}: {s['mean']:.4f} ± {s['std']:.4f}")
    
    ens.free_memory()
    
    print("\n" + "=" * 70)
    print("✓ ENSEMBLE COMPLETE")
    print("=" * 70)
    print(f"Output: {output_dir}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    cli()


if __name__ == '__main__':
    main()
