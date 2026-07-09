# sam2_contour.py
# ================================================================
# P&ID Symbol Contour Extraction — Production Inference Module
# ================================================================
#
# Single-file module for extracting P&ID symbol contours using
# fine-tuned SAM2 with LoRA. Provides a clean API for integration
# into the P&ID processing pipeline.
#
# Usage as import:
#     from sam2_contour import ContourExtractor
#     extractor = ContourExtractor('exp_v7/sam2_pid_best.pth')
#     result = extractor.predict(image, rough_bbox, pipe_mask, other_anns)
#
# Usage as CLI:
#     python sam2_contour.py \
#         --image image.png \
#         --bbox 100,200,300,400 \
#         --pipe-mask pipe_mask.png \
#         --checkpoint exp_v7/sam2_pid_best.pth \
#         --output result.json
#
# Technical notes:
#   - SAM2: use forward_image(), NOT image_encoder()
#   - sam_mask_decoder requires high_res_features, repeat_image=False
#   - Returns 4 values: (logits, iou_pred, _, _)
#   - RTX 4070 Laptop 8GB: batch_size=2 for small, batch=1 for base+
#   - Windows cp1251: Russian in .py crashes silently -> ASCII only
# ================================================================

import cv2
import json
import numpy as np
import argparse
from pathlib import Path
from typing import Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy.ndimage import distance_transform_edt

# --- Observability (Wave 3, batch3): progress + typed inference in predict_batch.
# sam2_contour runs in worker (app on PYTHONPATH) and standalone CLI; obs imported
# optionally (like engine.py): CLI -> no-op logger + stub errors. ASCII-only file.
try:
    from app.core.logging import get_logger
    from app.core.errors import InferenceError, PipelineError
    logger = get_logger(__name__)
except Exception:  # standalone: app not on PYTHONPATH
    import logging as _logging
    logger = _logging.getLogger(__name__)

    class PipelineError(Exception):
        def __init__(self, message="", **_kw):
            super().__init__(message)
            self.step = _kw.get("step")
            self.cause = _kw.get("cause")

    class InferenceError(PipelineError):
        pass


# ================================================================
# SAM2 Model Setup
# ================================================================

class LoRALayer(nn.Module):
    """Low-Rank Adaptation layer for SAM2 Linear modules.

    Adds trainable low-rank matrices A (r x in) and B (out x r) to
    the original Linear layer. Forward: original(x) + x @ A^T @ B^T * scale.
    Only lora_A, lora_B are saved in the checkpoint.
    """
    def __init__(self, original_layer, r=16, alpha=16):
        super().__init__()
        self.original = original_layer
        self.lora_A = nn.Parameter(torch.zeros(r, original_layer.in_features))
        self.lora_B = nn.Parameter(torch.zeros(original_layer.out_features, r))
        self.scale = alpha / r

    def forward(self, x):
        return self.original(x) + (x @ self.lora_A.T @ self.lora_B.T) * self.scale


def _apply_lora(model, r=16, alpha=16):
    """Insert LoRA layers into all SAM2 Linear modules with >= 64 features."""
    for name, module in list(model.named_modules()):
        if isinstance(module, nn.Linear) and module.in_features >= 64:
            parts = name.split('.')
            parent = model
            for p in parts[:-1]:
                parent = getattr(parent, p)
            setattr(parent, parts[-1], LoRALayer(module, r=r, alpha=alpha))


def _expand_patch_embed(model, new_channels=6):
    """Replace the 3-channel patch embedding conv with a 6-channel version.

    Copies pre-trained RGB weights to channels 0-2, initializes ch 3-5 to zero.
    """
    for name, module in model.image_encoder.named_modules():
        if isinstance(module, nn.Conv2d) and module.in_channels == 3:
            new_conv = nn.Conv2d(
                new_channels, module.out_channels,
                kernel_size=module.kernel_size, stride=module.stride,
                padding=module.padding, bias=module.bias is not None)
            new_conv.weight.data.zero_()
            new_conv.weight.data[:, :3] = module.weight.data.clone()
            if module.bias is not None:
                new_conv.bias.data = module.bias.data.clone()
            parts = name.split('.')
            parent = model.image_encoder
            for p in parts[:-1]:
                parent = getattr(parent, p)
            setattr(parent, parts[-1], new_conv)
            break


def _encode_image(model, image_tensor):
    """Run SAM2 image encoder + FPN. Returns (embedding, high_res_features).

    IMPORTANT: Uses forward_image(), not image_encoder().
    The latter skips FPN features required by the mask decoder.
    """
    backbone_out = model.forward_image(image_tensor)
    fpn = backbone_out["backbone_fpn"]
    n = getattr(model, 'num_feature_levels', len(fpn))
    fpn = fpn[-n:]
    return fpn[-1], fpn[:-1]


def _load_sam2_model(checkpoint_path, device='cuda'):
    """Load fine-tuned SAM2 model from checkpoint.

    Checkpoint contains only trainable weights (LoRA + patch_embed,
    optionally mask decoder). Base SAM2 weights are loaded from
    HuggingFace and merged.
    """
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    ckpt = torch.load(checkpoint_path, map_location='cpu')
    cfg = ckpt.get('config', {})
    base_checkpoint = cfg.get('checkpoint', 'facebook/sam2.1-hiera-small')
    lora_r = cfg.get('lora_r', 8)

    predictor = SAM2ImagePredictor.from_pretrained(base_checkpoint, device='cpu')
    model = predictor.model

    _expand_patch_embed(model, 6)
    _apply_lora(model, r=lora_r)
    model.load_state_dict(ckpt['state_dict'], strict=False)
    model.to(device).eval()

    return model, cfg


# ================================================================
# Geometry Helpers
# ================================================================

def compute_dt_max(mask):
    """Distance Transform Maximum — point maximally inside the mask.

    Args:
        mask: Binary mask, HxW, values 0/1 or 0/255.

    Returns:
        (x, y) tuple of the point with maximum distance to boundary.
        Falls back to mask center if mask is empty.
    """
    binary = (mask > 0.5).astype(np.uint8) if mask.max() <= 1 else (mask > 127).astype(np.uint8)
    if binary.sum() == 0:
        h, w = binary.shape
        return w // 2, h // 2
    dist = distance_transform_edt(binary)
    idx = np.unravel_index(dist.argmax(), dist.shape)
    return int(idx[1]), int(idx[0])


def compute_adaptive_K(bbox_w, bbox_h):
    """Compute crop multiplier based on object size.

    Small objects (max_side * 2 <= 1024): K=2.0, full context.
    Medium (max_side <= 1024): K scales so crop ~= 1024px.
    Large (max_side > 1024): K=1.2, preserve resolution.
    """
    max_side = max(bbox_w, bbox_h)
    if max_side * 2.0 <= 1024:
        return 2.0
    elif max_side <= 1024:
        return 1024.0 / max_side
    else:
        return 1.2


def compute_smart_crop(bbox_xywh, img_w, img_h):
    """Compute a square crop centered on bbox with adaptive K padding.

    Returns:
        (x1, y1, x2, y2, K) — crop bounds in image coordinates.
    """
    bx, by, bw, bh = [float(v) for v in bbox_xywh]
    bcx = bx + bw / 2
    bcy = by + bh / 2
    K = compute_adaptive_K(bw, bh)

    side = int(max(bw, bh) * K)
    side = max(side, 32)

    x1 = int(bcx - side / 2)
    y1 = int(bcy - side / 2)
    x2 = x1 + side
    y2 = y1 + side

    # Shift (not clip) to stay within image
    if x1 < 0:
        x2 -= x1; x1 = 0
    if y1 < 0:
        y2 -= y1; y1 = 0
    if x2 > img_w:
        x1 -= (x2 - img_w); x2 = img_w
    if y2 > img_h:
        y1 -= (y2 - img_h); y2 = img_h
    x1, y1 = max(0, x1), max(0, y1)

    return x1, y1, x2, y2, K


def smart_snap(mask, dp_eps_pct=0.15, snap_threshold=0.08, min_edge_pct=0.03):
    """Convert SAM2 pixel mask to regularized polygon.

    Algorithm:
        1. Find largest contour in binary mask
        2. Douglas-Peucker simplification (eps = dp_eps_pct% of perimeter)
        3. H/V edge snap: if edge cross-axis deviation < snap_threshold * length,
           force the edge to be perfectly horizontal or vertical
        4. 3 iterative snap passes for propagation
        5. Remove collinear points (threshold 1.5px)

    Args:
        mask:           Binary mask, HxW, values 0/1 or 0/255.
        dp_eps_pct:     DP epsilon as % of perimeter (0.15 = conservative).
        snap_threshold: Max deviation ratio for H/V snap (0.08 = snap if < 8% off).
        min_edge_pct:   Min edge length as % of min(H,W) to consider for snap.

    Returns:
        Nx2 int32 array of polygon vertices in crop coordinates, or None.
    """
    h, w = mask.shape
    m = (mask * 255).astype(np.uint8) if mask.max() <= 1 else mask.astype(np.uint8)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 100:
        return None

    eps = dp_eps_pct * 0.01 * cv2.arcLength(cnt, True)
    dp = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2).astype(np.float64)
    if len(dp) < 3:
        return None

    result = dp.copy()
    min_edge = min(w, h) * min_edge_pct

    # H/V snap: 3 passes for propagation
    for _ in range(3):
        for i in range(len(result)):
            j = (i + 1) % len(result)
            dx = abs(result[j][0] - result[i][0])
            dy = abs(result[j][1] - result[i][1])
            el = np.hypot(dx, dy)
            if el < min_edge:
                continue
            if dy / max(el, 1) < snap_threshold:
                avg = (result[i][1] + result[j][1]) / 2
                result[i][1] = result[j][1] = avg
            elif dx / max(el, 1) < snap_threshold:
                avg = (result[i][0] + result[j][0]) / 2
                result[i][0] = result[j][0] = avg

    # Remove collinear points
    cleaned = [result[0]]
    for i in range(1, len(result)):
        prev = cleaned[-1]
        curr = result[i]
        nxt = result[(i + 1) % len(result)]
        d = np.hypot(nxt[0] - prev[0], nxt[1] - prev[1])
        if d < 2:
            cleaned.append(curr)
            continue
        cross = abs((curr[0] - prev[0]) * (nxt[1] - prev[1]) -
                    (curr[1] - prev[1]) * (nxt[0] - prev[0]))
        if cross / d > 1.5:
            cleaned.append(curr)
    if len(cleaned) < 3:
        return np.int32(result)
    return np.int32(cleaned)


# ================================================================
# Channel Building Helpers
# ================================================================

def _build_rough_mask(crop_shape, rough_bbox, rough_polygon, crop_offset, pad=4):
    """Build channel 3 (rough mask) in crop coordinates.

    Uses polygon if available, otherwise falls back to bounding box.
    Dilated by `pad` pixels for tolerance.
    """
    ch, cw = crop_shape
    mask = np.zeros((ch, cw), dtype=np.uint8)
    ox, oy = crop_offset

    if rough_polygon is not None and len(rough_polygon) >= 3:
        local_pts = [(px - ox, py - oy) for (px, py) in rough_polygon]
        pts = np.array(local_pts, dtype=np.int32)
        cv2.fillPoly(mask, [pts], 255)
    else:
        bx, by, bw, bh = rough_bbox
        lx1 = max(0, int(bx - ox - pad))
        ly1 = max(0, int(by - oy - pad))
        lx2 = min(cw, int(bx + bw - ox + pad))
        ly2 = min(ch, int(by + bh - oy + pad))
        if lx2 > lx1 and ly2 > ly1:
            mask[ly1:ly2, lx1:lx2] = 255

    if pad > 0 and mask.max() > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (pad*2+1, pad*2+1))
        mask = cv2.dilate(mask, kernel)
    return mask


def _build_other_nodes_mask(crop_shape, crop_offset, other_anns, exclude_bbox=None):
    """Build channel 5 (neighboring nodes mask) in crop coordinates.

    Renders all other detected objects that overlap with the crop region.
    Excludes the target object itself (matched by exclude_bbox IoU).
    """
    ch, cw = crop_shape
    mask = np.zeros((ch, cw), dtype=np.uint8)
    ox, oy = crop_offset

    for ann in (other_anns or []):
        bbox = ann.get('bbox')
        if not bbox:
            continue

        # Skip target object (rough IoU match)
        if exclude_bbox is not None:
            ax, ay, aw, ah = bbox
            ex, ey, ew, eh = exclude_bbox
            ix = max(0, min(ax+aw, ex+ew) - max(ax, ex))
            iy = max(0, min(ay+ah, ey+eh) - max(ay, ey))
            inter = ix * iy
            union = aw * ah + ew * eh - inter
            if union > 0 and inter / union > 0.5:
                continue

        ax, ay, aw, ah = bbox
        # Skip if outside crop
        if ax + aw < ox or ax > ox + cw or ay + ah < oy or ay > oy + ch:
            continue

        seg = ann.get('segmentation')
        if seg and isinstance(seg, list) and len(seg) > 0:
            flat = seg[0] if isinstance(seg[0], list) else seg
            if len(flat) >= 6:
                pts = [(flat[i] - ox, flat[i+1] - oy) for i in range(0, len(flat), 2)]
                cv2.fillPoly(mask, [np.array(pts, dtype=np.int32)], 255)
                continue

        lx1 = max(0, int(ax - ox))
        ly1 = max(0, int(ay - oy))
        lx2 = min(cw, int(ax + aw - ox))
        ly2 = min(ch, int(ay + ah - oy))
        if lx2 > lx1 and ly2 > ly1:
            mask[ly1:ly2, lx1:lx2] = 255
    return mask


# ================================================================
# Single-Model Inference
# ================================================================

@torch.no_grad()
def _predict_mask(model, input_6ch, device, target_size=1024):
    """Run SAM2 inference on a 6-channel crop.

    Args:
        model:       SAM2 model with LoRA.
        input_6ch:   HxWx6 uint8 numpy array (crop).
        device:      'cuda' or 'cpu'.
        target_size: Resize input to this size for SAM2.

    Returns:
        mask:   HxW uint8 binary mask (0/1) in original crop size.
        prob:   HxW float32 probability map in original crop size.
        conf:   float, SAM2 iou_pred confidence score.
    """
    h, w = input_6ch.shape[:2]
    s = target_size

    # Resize to target size
    rgb = cv2.resize(input_6ch[:, :, :3], (s, s))
    ch345 = np.stack([
        cv2.resize(input_6ch[:, :, c], (s, s), interpolation=cv2.INTER_NEAREST)
        for c in [3, 4, 5]
    ], axis=-1)

    # Build tensor: 6 x H x W, float32 [0,1]
    tensor = np.zeros((6, s, s), dtype=np.float32)
    tensor[:3] = rgb.transpose(2, 0, 1) / 255.0
    tensor[3:] = ch345.transpose(2, 0, 1) / 255.0

    # DT-max prompt from rough mask (ch3)
    rough_r = cv2.resize(input_6ch[:, :, 3], (s, s), interpolation=cv2.INTER_NEAREST)
    cx, cy = compute_dt_max(rough_r)

    # Forward pass
    inp_t = torch.from_numpy(tensor).unsqueeze(0).to(device)
    emb, hr = _encode_image(model, inp_t)

    sp, dn = model.sam_prompt_encoder(
        points=(
            torch.tensor([[[float(cx), float(cy)]]], device=device),
            torch.tensor([[1]], device=device),
        ),
        boxes=None, masks=None,
    )

    logits, iou_pred, _, _ = model.sam_mask_decoder(
        image_embeddings=emb,
        image_pe=model.sam_prompt_encoder.get_dense_pe(),
        sparse_prompt_embeddings=sp,
        dense_prompt_embeddings=dn,
        multimask_output=False,
        repeat_image=False,
        high_res_features=hr,
    )

    # Resize back to original crop size
    pred = F.interpolate(logits, (h, w), mode='bilinear', align_corners=False)
    prob = torch.sigmoid(pred.squeeze()).cpu().numpy()
    mask = (prob > 0.5).astype(np.uint8)
    conf = float(iou_pred.squeeze().item())

    return mask, prob, conf


# ================================================================
# Main Extractor Class
# ================================================================

class ContourExtractor:
    """P&ID symbol contour extractor using fine-tuned SAM2.

    Handles: model loading, 6-channel input assembly, inference,
    polygon regularization, coordinate transform, optional ensemble.

    Args:
        checkpoint:      Path to v7 checkpoint (frozen decoder).
        checkpoint_v8:   Optional path to v8 checkpoint (decoder unfreeze)
                         for ensemble mode.
        device:          'cuda' or 'cpu'.
        target_size:     SAM2 input resolution (default 1024).
        snap_dp_eps:     DP simplification % of perimeter (default 0.15).
        snap_threshold:  H/V snap sensitivity (default 0.08).
        snap_min_edge:   Min edge % of min dim (default 0.03).
        confidence_threshold: Below this, mark as manual_review (default 0.85).
    """

    def __init__(
        self,
        checkpoint: str,
        checkpoint_v8: Optional[str] = None,
        device: str = 'cuda',
        target_size: int = 1024,
        snap_dp_eps: float = 0.15,
        snap_threshold: float = 0.08,
        snap_min_edge: float = 0.03,
        confidence_threshold: float = 0.85,
    ):
        self.device = device
        self.target_size = target_size
        self.snap_dp_eps = snap_dp_eps
        self.snap_threshold = snap_threshold
        self.snap_min_edge = snap_min_edge
        self.confidence_threshold = confidence_threshold

        self.model, self.cfg = _load_sam2_model(checkpoint, device)
        self.model_v8 = None
        if checkpoint_v8:
            self.model_v8, _ = _load_sam2_model(checkpoint_v8, device)

    def predict(
        self,
        image: np.ndarray,
        rough_bbox: list,
        rough_polygon: Optional[list] = None,
        pipe_mask: Optional[np.ndarray] = None,
        other_anns: Optional[list] = None,
    ) -> dict:
        """Extract contour polygon for one P&ID symbol.

        Args:
            image:         Full P&ID image, HxWx3 uint8 BGR.
            rough_bbox:    [x, y, w, h] COCO-format bounding box.
            rough_polygon: Optional list of (x, y) points. If provided,
                           used instead of bbox for rough mask (ch3).
            pipe_mask:     Full-image pipe mask, HxW uint8 (0 or 255).
            other_anns:    List of COCO annotation dicts for neighboring objects.
                           Each must have 'bbox', optionally 'segmentation'.

        Returns:
            dict with keys:
                polygon:        List of (x, y) in original image coordinates
                polygon_flat:   [x1,y1,x2,y2,...] COCO segmentation format
                mask:           Binary mask in crop coordinates, HxW uint8
                confidence:     SAM2 iou_pred (0-1)
                status:         'auto' or 'manual_review'
                crop_offset:    (x, y) of crop top-left in original image
                crop_size:      (w, h) of crop
                n_points:       Number of polygon vertices
                K:              Crop multiplier used
        """
        img_h, img_w = image.shape[:2]

        # 1. Compute crop region
        x1, y1, x2, y2, K = compute_smart_crop(rough_bbox, img_w, img_h)
        crop_w, crop_h = x2 - x1, y2 - y1

        # 2. Build 6-channel input
        input_6ch = np.zeros((crop_h, crop_w, 6), dtype=np.uint8)

        # Ch 0-2: RGB
        input_6ch[:, :, :3] = image[y1:y2, x1:x2]

        # Ch 3: Rough mask
        input_6ch[:, :, 3] = _build_rough_mask(
            (crop_h, crop_w), rough_bbox, rough_polygon, (x1, y1))

        # Ch 4: Pipe mask
        if pipe_mask is not None:
            input_6ch[:, :, 4] = pipe_mask[y1:y2, x1:x2]

        # Ch 5: Other nodes
        input_6ch[:, :, 5] = _build_other_nodes_mask(
            (crop_h, crop_w), (x1, y1), other_anns, exclude_bbox=rough_bbox)

        # 3. Inference
        if self.model_v8 is not None:
            result = self._predict_ensemble(input_6ch)
        else:
            result = self._predict_single(input_6ch)

        # 4. Polygon regularization
        polygon_crop = smart_snap(
            result['mask'],
            dp_eps_pct=self.snap_dp_eps,
            snap_threshold=self.snap_threshold,
            min_edge_pct=self.snap_min_edge,
        )

        # 5. Convert to original image coordinates
        if polygon_crop is not None and len(polygon_crop) >= 3:
            polygon = [(int(pt[0] + x1), int(pt[1] + y1)) for pt in polygon_crop]
            polygon_flat = []
            for px, py in polygon:
                polygon_flat.extend([float(px), float(py)])
            n_points = len(polygon)
        else:
            polygon = []
            polygon_flat = []
            n_points = 0

        # 6. Status
        conf = result['confidence']
        status = 'auto' if conf >= self.confidence_threshold else 'manual_review'

        return {
            'polygon': polygon,
            'polygon_flat': polygon_flat,
            'mask': result['mask'],
            'confidence': round(conf, 4),
            'status': status,
            'crop_offset': (x1, y1),
            'crop_size': (crop_w, crop_h),
            'n_points': n_points,
            'K': round(K, 3),
            **{k: v for k, v in result.items() if k not in ('mask', 'confidence')},
        }

    def _predict_single(self, input_6ch):
        """Single model prediction."""
        mask, prob, conf = _predict_mask(
            self.model, input_6ch, self.device, self.target_size)
        return {'mask': mask, 'confidence': conf}

    def _predict_ensemble(self, input_6ch):
        """Ensemble prediction with v7 + v8 models.

        Strategy:
            Both confident (> 0.98): average probability maps
            Otherwise: intersection (conservative)
        """
        mask_v7, prob_v7, conf_v7 = _predict_mask(
            self.model, input_6ch, self.device, self.target_size)
        mask_v8, prob_v8, conf_v8 = _predict_mask(
            self.model_v8, input_6ch, self.device, self.target_size)

        if conf_v7 > 0.98 and conf_v8 > 0.98:
            # Average mode: combine probabilities
            avg_prob = (prob_v7 + prob_v8) / 2.0
            mask = (avg_prob > 0.5).astype(np.uint8)
            ensemble_mode = 'average'
        else:
            # Intersection mode: conservative
            mask = (mask_v7 & mask_v8).astype(np.uint8)
            ensemble_mode = 'intersection'

        conf = max(conf_v7, conf_v8)
        return {
            'mask': mask,
            'confidence': conf,
            'confidence_v7': round(conf_v7, 4),
            'confidence_v8': round(conf_v8, 4),
            'ensemble_mode': ensemble_mode,
        }

    def predict_batch(
        self,
        image: np.ndarray,
        detections: list,
        pipe_mask: Optional[np.ndarray] = None,
    ) -> list:
        """Process all symbols in a P&ID image.

        Args:
            image:      Full P&ID image, HxWx3 uint8 BGR.
            detections: List of COCO annotation dicts. Each must have 'bbox'
                        and optionally 'segmentation'.
            pipe_mask:  Full-image pipe mask, HxW uint8.

        Returns:
            List of result dicts, one per detection (same format as predict()).
        """
        results = []
        n = len(detections)
        log_every = max(1, n // 10)  # ~10 progress lines per batch (CPU visibility)
        for _idx, det in enumerate(detections, 1):
            bbox = det.get('bbox')
            if not bbox:
                continue

            # Parse polygon from COCO segmentation
            seg = det.get('segmentation')
            rough_polygon = None
            if seg and isinstance(seg, list) and len(seg) > 0:
                flat = seg[0] if isinstance(seg[0], list) else seg
                if len(flat) >= 6:
                    rough_polygon = [(flat[i], flat[i+1]) for i in range(0, len(flat), 2)]

            # Other annotations = all detections except current
            other_anns = [d for d in detections if d is not det]

            # Progress in logs -> movement visible on CPU (~25s batch, obs batch3);
            # typed per-node failure -> InferenceError(step=compute).
            if _idx % log_every == 0 or _idx == n:
                logger.info("SAM2 contour node %d/%d", _idx, n)
            try:
                result = self.predict(
                    image=image,
                    rough_bbox=bbox,
                    rough_polygon=rough_polygon,
                    pipe_mask=pipe_mask,
                    other_anns=other_anns,
                )
            except PipelineError:
                raise
            except Exception as exc:
                raise InferenceError(
                    "SAM2 contour inference failed (ann_id=%s)" % det.get('id'),
                    step="compute", cause=exc,
                ) from exc
            result['ann_id'] = det.get('id')
            result['category_id'] = det.get('category_id')
            results.append(result)

        return results


# ================================================================
# Cached factory (avoids reloading SAM2 weights on every task run)
# ================================================================

_EXTRACTOR_CACHE = {}


def get_contour_extractor(
    checkpoint,
    checkpoint_v8=None,
    device='cuda',
    target_size=1024,
    snap_dp_eps=0.15,
    snap_threshold=0.08,
    snap_min_edge=0.03,
    confidence_threshold=0.85,
):
    """Return a process-cached ContourExtractor.

    The SAM2 base model (hiera-small + LoRA) is heavy to build, so we keep
    one instance per unique parameter set alive in the worker process and
    reuse it across task invocations instead of rebuilding it on every call.
    """
    key = (
        str(checkpoint), str(checkpoint_v8), str(device), int(target_size),
        float(snap_dp_eps), float(snap_threshold), float(snap_min_edge),
        float(confidence_threshold),
    )
    ext = _EXTRACTOR_CACHE.get(key)
    if ext is None:
        ext = ContourExtractor(
            checkpoint=checkpoint,
            checkpoint_v8=checkpoint_v8,
            device=device,
            target_size=target_size,
            snap_dp_eps=snap_dp_eps,
            snap_threshold=snap_threshold,
            snap_min_edge=snap_min_edge,
            confidence_threshold=confidence_threshold,
        )
        _EXTRACTOR_CACHE[key] = ext
    return ext


# ================================================================
# CLI Interface
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description='P&ID Symbol Contour Extraction')
    parser.add_argument('--image', type=Path, required=True,
                        help='P&ID image (PNG/JPEG)')
    parser.add_argument('--bbox', type=str, default=None,
                        help='Single bbox: x,y,w,h')
    parser.add_argument('--coco', type=Path, default=None,
                        help='COCO JSON with detection annotations (batch mode)')
    parser.add_argument('--pipe-mask', type=Path, default=None,
                        help='Pipe segmentation mask')
    parser.add_argument('--checkpoint', type=Path, required=True,
                        help='v7 checkpoint path')
    parser.add_argument('--checkpoint-v8', type=Path, default=None,
                        help='v8 checkpoint path (enables ensemble)')
    parser.add_argument('--output', type=Path, default=Path('contour_result.json'),
                        help='Output JSON path')
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--min-side', type=int, default=150,
                        help='Skip objects with max bbox side < this')
    args = parser.parse_args()

    # Load image
    image = cv2.imread(str(args.image))
    if image is None:
        raise FileNotFoundError(f"Cannot load image: {args.image}")

    # Load pipe mask
    pipe_mask = None
    if args.pipe_mask:
        pipe_mask = cv2.imread(str(args.pipe_mask), cv2.IMREAD_GRAYSCALE)

    # Initialize extractor
    extractor = ContourExtractor(
        checkpoint=str(args.checkpoint),
        checkpoint_v8=str(args.checkpoint_v8) if args.checkpoint_v8 else None,
        device=args.device,
    )

    results = []

    if args.coco:
        # Batch mode: process all annotations from COCO file
        with open(args.coco) as f:
            coco = json.load(f)

        detections = []
        for ann in coco.get('annotations', []):
            bbox = ann.get('bbox')
            if not bbox:
                continue
            if max(bbox[2], bbox[3]) < args.min_side:
                continue
            detections.append(ann)

        print(f"Processing {len(detections)} detections...")
        results = extractor.predict_batch(image, detections, pipe_mask)

    elif args.bbox:
        # Single bbox mode
        bbox = [float(v) for v in args.bbox.split(',')]
        result = extractor.predict(
            image=image,
            rough_bbox=bbox,
            pipe_mask=pipe_mask,
        )
        results = [result]

    else:
        parser.error('Provide either --bbox or --coco')

    # Save results (without numpy arrays)
    output_data = []
    for r in results:
        out = {k: v for k, v in r.items() if k != 'mask'}
        output_data.append(out)

    with open(args.output, 'w') as f:
        json.dump(output_data, f, indent=2)

    # Summary
    confs = [r['confidence'] for r in results]
    auto = sum(1 for r in results if r['status'] == 'auto')
    review = sum(1 for r in results if r['status'] == 'manual_review')
    print(f"\nDone: {len(results)} symbols processed")
    print(f"  Auto: {auto}, Manual review: {review}")
    if confs:
        print(f"  Confidence: min={min(confs):.3f}, mean={np.mean(confs):.3f}, max={max(confs):.3f}")
    print(f"Output: {args.output}")


if __name__ == '__main__':
    main()
