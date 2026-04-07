"""Hyperparameters for junction segmentation training."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    # ── Data ─────────────────────────────────────────────────────────
    data_dir: str = "./data/junction_seg"
    meta_json: str = "dataset_meta.json"

    # ── Tiling ───────────────────────────────────────────────────────
    tile_size: int = 512
    sigma: float = 4.0
    positive_ratio: float = 0.6
    epoch_tiles: int = 2000
    jitter_px: int = 100

    # ── Model ────────────────────────────────────────────────────────
    encoder_name: str = "efficientnet-b2"
    encoder_weights: str = "imagenet"
    in_channels: int = 5
    classes: int = 2
    skeleton_attention: bool = True
    skeleton_dilation_px: int = 20
    aux_head: bool = True

    # ── Loss ─────────────────────────────────────────────────────────
    focal_alpha: float = 2.0
    focal_beta: float = 4.0
    aux_loss_weight: float = 0.1

    # ── Training ─────────────────────────────────────────────────────
    batch_size: int = 16
    learning_rate: float = 1e-4
    weight_decay: float = 1e-4
    epochs: int = 120
    warmup_epochs: int = 3
    amp: bool = True

    # ── Augmentations ────────────────────────────────────────────────
    aug_hflip: float = 0.5
    aug_vflip: float = 0.5
    aug_rotate90: float = 0.5
    aug_brightness: float = 0.3
    aug_brightness_limit: float = 0.2
    aug_noise: float = 0.2
    aug_noise_var: float = 10.0

    # ── Validation / metrics ─────────────────────────────────────────
    val_tile_overlap: int = 128
    val_threshold_junction: float = 0.4
    val_threshold_bridge: float = 0.5
    match_radius: int = 15
    nms_kernel: int = 3

    # ── Early stopping ───────────────────────────────────────────────
    patience: int = 40
    monitor: str = "val_f1_combined"

    # ── Infrastructure ───────────────────────────────────────────────
    num_workers: int = 2
    device: str = "cuda"
    output_dir: str = "./runs/junction_seg"
    save_every: int = 10
    log_every: int = 50

    pipe_seg_weights: Optional[str] = None


def load_config(yaml_path: Optional[str] = None) -> Config:
    cfg = Config()
    if yaml_path is None:
        return cfg
    import yaml
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    for k, v in data.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg
