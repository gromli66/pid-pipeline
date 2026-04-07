"""
Junction segmentation model.

Architecture:
  - Encoder: EfficientNet-B2 (5-channel input)
  - Decoder: U-Net++ (dense skip connections)
  - Skeleton attention gate: dilated skeleton → soft mask on decoder output
  - Aux head: tile-level binary classifier
  - Output: 2-channel RAW LOGITS (sigmoid applied in loss or at inference)

Critical design choices:
  1. forward() returns LOGITS, not sigmoid — loss uses F.logsigmoid for stability
  2. Seg head bias = -4.6 → sigmoid ≈ 0.01 → initial loss ≈ 5 (CenterNet trick)
  3. Skeleton attention applied to decoder features before seg head
"""

import logging
import torch
import torch.nn as nn
import torch.nn.functional as F
import segmentation_models_pytorch as smp

from .config import Config

logger = logging.getLogger(__name__)


class SkeletonAttentionGate(nn.Module):
    """Spatial attention: dilate skeleton → soft multiply on decoder features."""

    def __init__(self, dilation_px: int = 20):
        super().__init__()
        self.dilation_px = dilation_px
        self.kernel_size = 2 * dilation_px + 1

    def forward(self, features: torch.Tensor, skeleton: torch.Tensor) -> torch.Tensor:
        """
        features: [B, C, H, W] — decoder output
        skeleton: [B, 1, H, W] — skeleton channel, values in [0, 1]
        """
        attention = F.max_pool2d(
            skeleton,
            kernel_size=self.kernel_size,
            stride=1,
            padding=self.dilation_px,
        )
        # Soft gate: base=0.1 so background isn't completely zeroed
        attention = attention * 0.9 + 0.1
        return features * attention


class AuxHead(nn.Module):
    """Tile-level binary classifier: "does this tile contain any point?" """

    def __init__(self, in_channels: int, dropout: float = 0.3):
        super().__init__()
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head = nn.Sequential(
            nn.Linear(in_channels, 128),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
            nn.Linear(128, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.pool(x).flatten(1))


class JunctionSegModel(nn.Module):
    """
    U-Net++ for junction/bridge heatmap prediction.

    forward()  → (logits [B,2,H,W], aux_logit [B,1])   — for training
    predict()  → (sigmoid [B,2,H,W], aux_logit [B,1])   — for inference
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

        # Build base smp model
        base = smp.UnetPlusPlus(
            encoder_name=cfg.encoder_name,
            encoder_weights=cfg.encoder_weights if cfg.encoder_weights != "none" else None,
            in_channels=cfg.in_channels,
            classes=cfg.classes,
            activation=None,
        )

        # Extract components
        self.encoder = base.encoder
        self.decoder = base.decoder
        self.seg_head = base.segmentation_head

        # CenterNet bias initialization
        # sigmoid(-4.6) ≈ 0.01 → p^α = 0.0001 → initial loss ≈ 5
        # Without this: sigmoid(0) = 0.5 → p^α = 0.25 → initial loss ≈ 60,000
        self._init_seg_head(bias_value=-4.6)

        # Skeleton attention gate
        self.use_attention = cfg.skeleton_attention
        if self.use_attention:
            self.attention_gate = SkeletonAttentionGate(cfg.skeleton_dilation_px)

        # Aux head
        self.use_aux = cfg.aux_head
        if self.use_aux:
            bottleneck_ch = self.encoder.out_channels[-1]
            self.aux_classifier = AuxHead(bottleneck_ch)

    def _init_seg_head(self, bias_value: float):
        """CenterNet/CornerNet init: small weights + negative bias on final conv."""
        for m in self.seg_head.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.normal_(m.weight, mean=0, std=0.001)
                if m.bias is not None:
                    nn.init.constant_(m.bias, bias_value)
                    logger.info(
                        "Seg head init: Conv2d(%d→%d), bias=%.2f, sigmoid(bias)=%.4f",
                        m.in_channels, m.out_channels, bias_value,
                        torch.sigmoid(torch.tensor(bias_value)).item(),
                    )

    def forward(self, x: torch.Tensor) -> tuple:
        """
        Training forward — returns RAW LOGITS (no sigmoid).

        Args:
            x: [B, 5, H, W]
        Returns:
            logits:    [B, 2, H, W] — raw, pre-sigmoid
            aux_logit: [B, 1] or None
        """
        skeleton_ch = x[:, 4:5, :, :]

        features = self.encoder(x)

        aux_logit = None
        if self.use_aux:
            aux_logit = self.aux_classifier(features[-1])

        decoder_output = self.decoder(features)

        if self.use_attention:
            decoder_output = self.attention_gate(decoder_output, skeleton_ch)

        logits = self.seg_head(decoder_output)

        return logits, aux_logit

    @torch.no_grad()
    def predict(self, x: torch.Tensor) -> tuple:
        """
        Inference — returns sigmoid heatmaps.

        Args:
            x: [B, 5, H, W]
        Returns:
            heatmaps:  [B, 2, H, W] in [0, 1]
            aux_logit: [B, 1] or None
        """
        logits, aux_logit = self.forward(x)
        return torch.sigmoid(logits), aux_logit


def create_model(cfg: Config) -> JunctionSegModel:
    model = JunctionSegModel(cfg)
    if cfg.pipe_seg_weights:
        _init_from_pipe_seg(model, cfg.pipe_seg_weights)
    return model


def _init_from_pipe_seg(model: JunctionSegModel, weights_path: str):
    """Init encoder from pipe segmentation weights (4ch → 5ch adaptation)."""
    try:
        state = torch.load(weights_path, map_location="cpu", weights_only=True)
        if "model_state_dict" in state:
            state = state["model_state_dict"]
        elif "state_dict" in state:
            state = state["state_dict"]

        encoder_state = {
            k.replace("encoder.", ""): v
            for k, v in state.items()
            if k.startswith("encoder.")
        }

        if not encoder_state:
            logger.warning("No encoder keys in pipe seg weights, skipping")
            return

        # Adapt first conv: 4 channels → 5 channels
        for k in encoder_state:
            if "conv" in k.lower() and encoder_state[k].dim() == 4:
                w = encoder_state[k]
                if w.shape[1] == 4:
                    new_w = torch.zeros(w.shape[0], 5, w.shape[2], w.shape[3])
                    new_w[:, :3] = w[:, :3]         # RGB
                    new_w[:, 3] = w[:, 3]            # pipe mask
                    new_w[:, 4] = w[:, 3] * 0.5      # skeleton
                    encoder_state[k] = new_w
                elif w.shape[1] == 3:
                    mean_w = w.mean(dim=1, keepdim=True)
                    new_w = torch.zeros(w.shape[0], 5, w.shape[2], w.shape[3])
                    new_w[:, :3] = w
                    new_w[:, 3] = mean_w.squeeze(1)
                    new_w[:, 4] = mean_w.squeeze(1)
                    encoder_state[k] = new_w
                break

        missing, unexpected = model.encoder.load_state_dict(encoder_state, strict=False)
        logger.info("Pipe seg encoder loaded: %d missing, %d unexpected", len(missing), len(unexpected))

    except Exception as e:
        logger.warning("Failed to load pipe seg weights: %s", e)


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
