"""
Training script for junction/bridge segmentation.

Usage:
  python -m junction_segmentation.train --data-dir ./data/junction_seg
  python -m junction_segmentation.train --resume ./runs/exp1/best.pth --epochs 100
"""

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from .config import Config, load_config
from .dataset import JunctionDataset, TiledInferenceDataset
from .loss import JunctionLoss
from .metrics import evaluate_heatmaps
from .model import create_model, count_parameters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def gaussian_blend_mask(size: int, sigma_ratio: float = 0.25) -> np.ndarray:
    sigma = size * sigma_ratio
    ax = np.arange(size, dtype=np.float32) - size / 2
    xx, yy = np.meshgrid(ax, ax)
    return np.exp(-(xx ** 2 + yy ** 2) / (2 * sigma ** 2))


# ═════════════════════════════════════════════════════════════════════════
# Training
# ═════════════════════════════════════════════════════════════════════════

def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    criterion: JunctionLoss,
    optimizer: torch.optim.Optimizer,
    scaler: torch.amp.GradScaler,
    device: torch.device,
    cfg: Config,
    epoch: int,
    writer: Optional[SummaryWriter] = None,
) -> Dict[str, float]:
    model.train()
    running = {"total_loss": 0, "junction_loss": 0, "bridge_loss": 0, "aux_loss": 0}
    n_batches = 0
    global_step = epoch * len(loader)

    pbar = tqdm(loader, desc=f"  Train ep.{epoch}", leave=False,
                bar_format="{l_bar}{bar:20}{r_bar}")

    for batch_idx, batch in enumerate(pbar):
        inputs = batch["input"].to(device)
        gt_heatmaps = batch["heatmap"].to(device)
        has_pos = batch["has_positive"].to(device)

        optimizer.zero_grad()

        with torch.amp.autocast("cuda", enabled=cfg.amp):
            logits, aux_logit = model(inputs)
            losses = criterion(logits, gt_heatmaps, aux_logit, has_pos)

        scaler.scale(losses["total_loss"]).backward()
        scaler.step(optimizer)
        scaler.update()

        for k in running:
            running[k] += losses[k].item()
        n_batches += 1

        pbar.set_postfix(
            loss=f"{running['total_loss']/n_batches:.4f}",
            j=f"{running['junction_loss']/n_batches:.4f}",
            b=f"{running['bridge_loss']/n_batches:.4f}",
        )

        if writer and (batch_idx + 1) % cfg.log_every == 0:
            step = global_step + batch_idx
            writer.add_scalar("train/total_loss", losses["total_loss"].item(), step)

    pbar.close()
    return {k: v / max(n_batches, 1) for k, v in running.items()}


# ═════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════

@torch.no_grad()
def validate(
    model: nn.Module,
    val_schemas: List[str],
    data_dir: Path,
    device: torch.device,
    cfg: Config,
) -> Dict[str, float]:
    model.eval()

    total_tp_j, total_fp_j, total_fn_j = 0, 0, 0
    total_tp_b, total_fp_b, total_fn_b = 0, 0, 0

    blend = gaussian_blend_mask(cfg.tile_size)

    pbar = tqdm(val_schemas, desc="  Val", leave=False,
                bar_format="{l_bar}{bar:20}{r_bar}")

    for schema in pbar:
        img = cv2.imread(str(data_dir / "images" / f"{schema}.png"))
        if img is None:
            continue
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mask = cv2.imread(str(data_dir / "pipe_masks" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        skel = cv2.imread(str(data_dir / "skeletons" / f"{schema}.png"), cv2.IMREAD_GRAYSCALE)
        if mask is None or skel is None:
            continue

        h, w = img.shape[:2]

        with open(data_dir / "annotations" / f"{schema}.json") as f:
            ann = json.load(f)
        gt_junctions = [(p["x"], p["y"]) for p in ann["junctions"]]
        gt_bridges = [(p["x"], p["y"]) for p in ann["bridges"]]

        tiled = TiledInferenceDataset(img, mask, skel, cfg.tile_size, cfg.val_tile_overlap)

        prob_acc = np.zeros((2, h, w), dtype=np.float32)
        weight_acc = np.zeros((h, w), dtype=np.float32)

        batch_tensors = []
        batch_positions = []

        for i in range(len(tiled)):
            tensor, (ty, tx) = tiled.get_tile_tensor(i)
            batch_tensors.append(tensor)
            batch_positions.append((ty, tx))

            if len(batch_tensors) == cfg.batch_size or i == len(tiled) - 1:
                batch_t = torch.stack(batch_tensors).to(device)

                with torch.amp.autocast("cuda", enabled=cfg.amp):
                    logits, _ = model(batch_t)
                    hm = torch.sigmoid(logits)

                hm_np = hm.cpu().numpy()

                for j, (ty, tx) in enumerate(batch_positions):
                    ts = cfg.tile_size
                    tile_h = min(ts, h - ty)
                    tile_w = min(ts, w - tx)
                    prob_acc[:, ty:ty+tile_h, tx:tx+tile_w] += (
                        hm_np[j, :, :tile_h, :tile_w] * blend[:tile_h, :tile_w]
                    )
                    weight_acc[ty:ty+tile_h, tx:tx+tile_w] += blend[:tile_h, :tile_w]

                batch_tensors.clear()
                batch_positions.clear()

        weight_acc = np.maximum(weight_acc, 1e-8)
        pred_hm = prob_acc / weight_acc

        metrics = evaluate_heatmaps(
            pred_hm, gt_junctions, gt_bridges,
            cfg.val_threshold_junction, cfg.val_threshold_bridge,
            cfg.match_radius, cfg.nms_kernel,
        )

        total_tp_j += metrics["junction"]["tp"]
        total_fp_j += metrics["junction"]["fp"]
        total_fn_j += metrics["junction"]["fn"]
        total_tp_b += metrics["bridge"]["tp"]
        total_fp_b += metrics["bridge"]["fp"]
        total_fn_b += metrics["bridge"]["fn"]

        pbar.set_postfix(j_tp=total_tp_j, j_fp=total_fp_j, b_tp=total_tp_b, b_fp=total_fp_b)

    pbar.close()

    def safe_div(a, b):
        return a / b if b > 0 else 0.0

    p_j = safe_div(total_tp_j, total_tp_j + total_fp_j)
    r_j = safe_div(total_tp_j, total_tp_j + total_fn_j)
    f1_j = safe_div(2 * p_j * r_j, p_j + r_j)

    p_b = safe_div(total_tp_b, total_tp_b + total_fp_b)
    r_b = safe_div(total_tp_b, total_tp_b + total_fn_b)
    f1_b = safe_div(2 * p_b * r_b, p_b + r_b)

    f1_combined = (f1_j + f1_b) / 2.0

    return {
        "val_precision_junction": p_j, "val_recall_junction": r_j, "val_f1_junction": f1_j,
        "val_tp_junction": total_tp_j, "val_fp_junction": total_fp_j, "val_fn_junction": total_fn_j,
        "val_precision_bridge": p_b, "val_recall_bridge": r_b, "val_f1_bridge": f1_b,
        "val_tp_bridge": total_tp_b, "val_fp_bridge": total_fp_b, "val_fn_bridge": total_fn_b,
        "val_f1_combined": f1_combined,
    }


# ═════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════

def train(cfg: Config, resume_path: str = None):
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(output_dir / "config.json", "w") as f:
        json.dump(cfg.__dict__, f, indent=2, default=str)

    device = torch.device(cfg.device if torch.cuda.is_available() else "cpu")
    logger.info("Device: %s", device)

    data_dir = Path(cfg.data_dir)
    with open(data_dir / cfg.meta_json) as f:
        meta = json.load(f)

    train_schemas = meta["split"]["train"]
    val_schemas = meta["split"]["val"]
    logger.info("Train: %d schemas, Val: %d schemas", len(train_schemas), len(val_schemas))

    train_ds = JunctionDataset(train_schemas, cfg.data_dir, cfg, is_train=True)
    logger.info("Train dataset: %d virtual tiles/epoch, %d positive samples",
                len(train_ds), len(train_ds.positives))

    train_loader = DataLoader(
        train_ds, batch_size=cfg.batch_size, shuffle=True,
        num_workers=cfg.num_workers, pin_memory=True, drop_last=True,
        persistent_workers=cfg.num_workers > 0,
    )

    model = create_model(cfg).to(device)
    logger.info("Model: %s, %.1fM parameters", cfg.encoder_name, count_parameters(model) / 1e6)

    criterion = JunctionLoss(cfg)
    optimizer = torch.optim.AdamW(model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=cfg.epochs - cfg.warmup_epochs, eta_min=1e-6
    )
    scaler = torch.amp.GradScaler("cuda", enabled=cfg.amp)

    # ── Resume from checkpoint ───────────────────────────────────────
    start_epoch = 1
    best_metric = 0.0

    if resume_path:
        ckpt = torch.load(resume_path, map_location=device, weights_only=False)
        model.load_state_dict(ckpt["model_state_dict"])
        optimizer.load_state_dict(ckpt["optimizer_state_dict"])
        if "scheduler_state_dict" in ckpt:
            scheduler.load_state_dict(ckpt["scheduler_state_dict"])
        start_epoch = ckpt["epoch"] + 1
        best_metric = ckpt.get("best_metric", 0.0)
        logger.info("Resumed from epoch %d, best=%.4f", ckpt["epoch"], best_metric)

    # ── TensorBoard ──────────────────────────────────────────────────
    writer = SummaryWriter(log_dir=str(output_dir / "tb"))

    patience_counter = 0

    logger.info("=" * 60)
    logger.info("Starting training: epochs %d-%d, batch %d", start_epoch, cfg.epochs, cfg.batch_size)
    logger.info("=" * 60)

    for epoch in range(start_epoch, cfg.epochs + 1):
        t0 = time.time()

        if epoch <= cfg.warmup_epochs:
            for pg in optimizer.param_groups:
                pg["lr"] = cfg.learning_rate * epoch / cfg.warmup_epochs

        train_metrics = train_one_epoch(
            model, train_loader, criterion, optimizer, scaler, device, cfg, epoch, writer
        )

        if epoch > cfg.warmup_epochs:
            scheduler.step()

        current_lr = optimizer.param_groups[0]["lr"]
        train_time = time.time() - t0

        t1 = time.time()
        val_metrics = validate(model, val_schemas, data_dir, device, cfg)
        val_time = time.time() - t1

        writer.add_scalar("lr", current_lr, epoch)
        for k, v in train_metrics.items():
            writer.add_scalar(f"train/{k}", v, epoch)
        for k, v in val_metrics.items():
            if isinstance(v, float):
                writer.add_scalar(f"val/{k}", v, epoch)

        pj = val_metrics["val_precision_junction"]
        rj = val_metrics["val_recall_junction"]
        f1j = val_metrics["val_f1_junction"]
        pb = val_metrics["val_precision_bridge"]
        rb = val_metrics["val_recall_bridge"]
        f1b = val_metrics["val_f1_bridge"]
        f1c = val_metrics["val_f1_combined"]

        logger.info(
            "Epoch %3d/%d  loss=%.4f  "
            "J[P=%.1f%% R=%.1f%% F1=%.1f%%]  "
            "B[P=%.1f%% R=%.1f%% F1=%.1f%%]  "
            "combined=%.1f%%  lr=%.1e  train=%.0fs val=%.0fs",
            epoch, cfg.epochs, train_metrics["total_loss"],
            pj*100, rj*100, f1j*100, pb*100, rb*100, f1b*100,
            f1c*100, current_lr, train_time, val_time,
        )

        monitored = val_metrics.get(cfg.monitor, f1c)

        if monitored > best_metric:
            best_metric = monitored
            patience_counter = 0
            torch.save({
                "epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict(),
                "best_metric": best_metric,
                "config": cfg.__dict__,
                "val_metrics": val_metrics,
            }, output_dir / "best.pth")
            logger.info("  ★ New best %s=%.4f — saved best.pth", cfg.monitor, best_metric)
        else:
            patience_counter += 1
            if patience_counter >= cfg.patience:
                logger.info("Early stopping after %d epochs without improvement", cfg.patience)
                break

        if epoch % cfg.save_every == 0:
            torch.save({
                "epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "best_metric": best_metric,
            }, output_dir / f"epoch_{epoch}.pth")

    writer.close()
    logger.info("=" * 60)
    logger.info("Training complete. Best %s=%.4f", cfg.monitor, best_metric)
    logger.info("Output: %s", output_dir)
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str)
    parser.add_argument("--data-dir", type=str)
    parser.add_argument("--output-dir", type=str)
    parser.add_argument("--epochs", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--lr", type=float)
    parser.add_argument("--device", type=str)
    parser.add_argument("--num-workers", type=int)
    parser.add_argument("--pipe-seg-weights", type=str)
    parser.add_argument("--resume", type=str, help="Resume from checkpoint .pth")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.data_dir: cfg.data_dir = args.data_dir
    if args.output_dir: cfg.output_dir = args.output_dir
    if args.epochs: cfg.epochs = args.epochs
    if args.batch_size: cfg.batch_size = args.batch_size
    if args.lr: cfg.learning_rate = args.lr
    if args.device: cfg.device = args.device
    if args.num_workers is not None: cfg.num_workers = args.num_workers
    if args.pipe_seg_weights: cfg.pipe_seg_weights = args.pipe_seg_weights

    train(cfg, resume_path=args.resume)


if __name__ == "__main__":
    main()