"""
P&ID Pipe Segmentation - Augmentations

[PERF] skimage.skeletonize заменён на cv2 thinning (10-30x быстрее)
[PERF] skimage.label заменён на cv2.connectedComponentsWithStats
[FIX] Albumentations v2.x API совместимость (Rotate, CoarseDropout, GaussNoise и др.)
[FIX 2.1] RealisticDashedLineAugmentation
[FIX 2.2] dilate_radius 2→4
[FIX 2.3] Fallback при пустом скелете
"""

import cv2
import numpy as np
import random
from typing import Tuple

import albumentations as A
from albumentations.pytorch import ToTensorV2
from albumentations.core.transforms_interface import DualTransform

from pipe_segmentation.config.defaults import IMAGENET_MEAN, IMAGENET_STD


# ============================================================================
# БЫСТРАЯ СКЕЛЕТОНИЗАЦИЯ (cv2 вместо skimage)
# ============================================================================

def fast_skeletonize(mask: np.ndarray) -> np.ndarray:
    """
    [PERF] Быстрая скелетонизация.
    Приоритет: cv2.ximgproc.thinning > морфологическая (с лимитом итераций).
    
    Args:
        mask: Бинарная маска uint8 (0 или 1, или 0 или 255)
    Returns:
        Скелет uint8 (0/1)
    """
    if mask.max() <= 1:
        binary = (mask * 255).astype(np.uint8)
    else:
        binary = mask.astype(np.uint8)
    
    if binary.sum() == 0:
        return np.zeros_like(mask, dtype=np.uint8)
    
    # Лучший вариант: cv2.ximgproc.thinning (~5ms)
    if hasattr(cv2, 'ximgproc') and hasattr(cv2.ximgproc, 'thinning'):
        skeleton = cv2.ximgproc.thinning(binary)
        return (skeleton > 0).astype(np.uint8)
    
    # Fallback: морфологическая скелетонизация с лимитом итераций
    skel = np.zeros_like(binary)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    temp = binary.copy()
    max_iters = 100  # Лимит чтобы не зависнуть
    for _ in range(max_iters):
        eroded = cv2.erode(temp, element)
        opened = cv2.dilate(eroded, element)
        subset = cv2.subtract(temp, opened)
        skel = cv2.bitwise_or(skel, subset)
        temp = eroded.copy()
        if cv2.countNonZero(temp) == 0:
            break
    return (skel > 0).astype(np.uint8)


def fast_label(mask: np.ndarray):
    """
    [PERF] cv2.connectedComponents вместо skimage.label.
    
    Returns:
        (labeled_array, num_components)
    """
    n, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
    return labels, n - 1  # n включает фон (0)


# ============================================================================
# CUSTOM AUGMENTATIONS
# ============================================================================

class DashedLineAugmentation(DualTransform):
    """
    Создаёт синтетические разрывы в сплошных трубах.
    Учит модель восстанавливать пропуски.
    """

    def __init__(
        self,
        gap_length: Tuple[int, int] = (10, 30),
        num_gaps: Tuple[int, int] = (3, 8),
        dilate_radius: int = 4,
        always_apply: bool = False,
        p: float = 0.3
    ):
        super().__init__(always_apply, p)
        self.gap_length = gap_length
        self.num_gaps = num_gaps
        self.dilate_radius = dilate_radius

    def apply(self, img: np.ndarray, **params):
        pipe_mask = params.get('pipe_mask', None)
        if pipe_mask is None or pipe_mask.max() == 0:
            return img

        pipe_binary = (pipe_mask > 0.5).astype(np.uint8) if pipe_mask.dtype != np.uint8 \
            else (pipe_mask > 127).astype(np.uint8)

        if pipe_binary.sum() < 100:
            return img

        try:
            dashed_mask = self._create_dashed_mask(pipe_binary)
            gap_mask = (pipe_binary > 0) & (dashed_mask == 0)
            if gap_mask.sum() > 0:
                img_modified = img.copy()
                img_modified[gap_mask] = 255
                return img_modified
        except Exception:
            pass
        return img

    def apply_to_mask(self, mask: np.ndarray, **params):
        return mask

    def _create_dashed_mask(self, pipe_binary: np.ndarray) -> np.ndarray:
        skeleton = fast_skeletonize(pipe_binary)

        if skeleton.sum() < 10:
            return self._fallback_random_gaps(pipe_binary)

        skeleton_coords = np.column_stack(np.where(skeleton > 0))
        if len(skeleton_coords) < 10:
            return self._fallback_random_gaps(pipe_binary)

        dashed_skeleton = self._apply_dashed_pattern(skeleton, skeleton_coords)

        if self.dilate_radius > 0:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, 
                (self.dilate_radius * 2 + 1, self.dilate_radius * 2 + 1)
            )
            dashed_mask = cv2.dilate(dashed_skeleton, kernel)
        else:
            dashed_mask = dashed_skeleton
        return dashed_mask

    def _fallback_random_gaps(self, pipe_binary: np.ndarray) -> np.ndarray:
        result = pipe_binary.copy()
        coords = np.column_stack(np.where(pipe_binary > 0))
        if len(coords) < 10:
            return result
        num_gaps = random.randint(self.num_gaps[0], self.num_gaps[1])
        for _ in range(num_gaps):
            idx = random.randint(0, len(coords) - 1)
            cy, cx = coords[idx]
            gap_len = random.randint(self.gap_length[0], self.gap_length[1])
            half = gap_len // 2
            r = self.dilate_radius + 1
            result[max(0, cy - r):cy + r, max(0, cx - half):cx + half] = 0
        return result

    def _apply_dashed_pattern(self, skeleton: np.ndarray, coords: np.ndarray) -> np.ndarray:
        dashed = skeleton.copy()
        num_gaps = random.randint(self.num_gaps[0], self.num_gaps[1])
        for _ in range(num_gaps):
            if len(coords) == 0:
                break
            start_idx = random.randint(0, len(coords) - 1)
            start_point = coords[start_idx]
            gap_len = random.randint(self.gap_length[0], self.gap_length[1])
            distances = np.linalg.norm(coords - start_point, axis=1)
            nearby = np.where(distances <= gap_len)[0]
            for idx in nearby[:gap_len]:
                coord = coords[idx]
                dashed[coord[0], coord[1]] = 0
        return dashed

    def get_transform_init_args_names(self):
        return ("gap_length", "num_gaps", "dilate_radius", "p")

    def get_params_dependent_on_targets(self, params):
        return {"pipe_mask": params.get("mask")}

    @property
    def targets_as_params(self):
        return ["mask"]


class RealisticDashedLineAugmentation(DualTransform):
    """
    [FIX 2.1] Рисует регулярный пунктир на изображении, маска сплошная.
    [PERF] cv2 вместо skimage: ~5ms вместо ~150ms на тайл.
    """

    def __init__(
        self,
        dash_length: Tuple[int, int] = (15, 40),
        gap_length: Tuple[int, int] = (10, 30),
        line_thickness: Tuple[int, int] = (1, 3),
        fraction_pipes: Tuple[float, float] = (0.1, 0.4),
        always_apply: bool = False,
        p: float = 0.35
    ):
        super().__init__(always_apply, p)
        self.dash_length = dash_length
        self.gap_length = gap_length
        self.line_thickness = line_thickness
        self.fraction_pipes = fraction_pipes

    def apply(self, img: np.ndarray, **params):
        pipe_mask = params.get('pipe_mask', None)
        if pipe_mask is None or pipe_mask.max() == 0:
            return img

        pipe_binary = (pipe_mask > 0.5).astype(np.uint8) if pipe_mask.dtype != np.uint8 \
            else (pipe_mask > 127).astype(np.uint8)

        if pipe_binary.sum() < 200:
            return img

        try:
            return self._apply_dashed_pattern(img, pipe_binary)
        except Exception:
            return img

    def apply_to_mask(self, mask: np.ndarray, **params):
        return mask

    def _apply_dashed_pattern(self, img: np.ndarray, pipe_binary: np.ndarray) -> np.ndarray:
        # [PERF] cv2 вместо skimage
        labeled, n_components = fast_label(pipe_binary)
        if n_components == 0:
            return img

        frac = random.uniform(self.fraction_pipes[0], self.fraction_pipes[1])
        n_to_replace = max(1, int(n_components * frac))
        components = random.sample(
            range(1, n_components + 1),
            min(n_to_replace, n_components)
        )

        img_modified = img.copy()

        for comp_id in components:
            comp_mask = (labeled == comp_id).astype(np.uint8)
            if comp_mask.sum() < 100:
                continue

            skeleton = fast_skeletonize(comp_mask)
            if skeleton.sum() < 10:
                continue

            img_modified[comp_mask > 0] = 255
            self._draw_dashed_skeleton(img_modified, skeleton)

        return img_modified

    def _draw_dashed_skeleton(self, img: np.ndarray, skeleton: np.ndarray):
        coords = np.column_stack(np.where(skeleton > 0))
        if len(coords) < 5:
            return

        ordered = self._order_skeleton_coords(coords)

        dash_len = random.randint(self.dash_length[0], self.dash_length[1])
        gap_len = random.randint(self.gap_length[0], self.gap_length[1])
        thickness = random.randint(self.line_thickness[0], self.line_thickness[1])

        accumulated = 0
        drawing = True
        segment_start = 0

        for i in range(1, len(ordered)):
            dist = np.linalg.norm(ordered[i] - ordered[i - 1])
            accumulated += dist
            target_len = dash_len if drawing else gap_len

            if accumulated >= target_len:
                if drawing:
                    for j in range(segment_start, i):
                        y, x = ordered[j]
                        cv2.circle(img, (int(x), int(y)), thickness, 0, -1)
                drawing = not drawing
                accumulated = 0
                segment_start = i

        if drawing:
            for j in range(segment_start, len(ordered)):
                y, x = ordered[j]
                cv2.circle(img, (int(x), int(y)), thickness, 0, -1)

    def _order_skeleton_coords(self, coords: np.ndarray) -> np.ndarray:
        if len(coords) < 3:
            return coords
        ordered = [coords[0]]
        remaining = set(range(1, len(coords)))
        for _ in range(min(len(coords) - 1, 500)):  # cap iterations
            if not remaining:
                break
            last = ordered[-1]
            remaining_list = list(remaining)
            remaining_coords = coords[remaining_list]
            distances = np.linalg.norm(remaining_coords - last, axis=1)
            nearest_idx = np.argmin(distances)
            if distances[nearest_idx] > 5:
                break
            ordered.append(remaining_coords[nearest_idx])
            remaining.remove(remaining_list[nearest_idx])
        return np.array(ordered)

    def get_transform_init_args_names(self):
        return ("dash_length", "gap_length", "line_thickness", "fraction_pipes", "p")

    def get_params_dependent_on_targets(self, params):
        return {"pipe_mask": params.get("mask")}

    @property
    def targets_as_params(self):
        return ["mask"]


# ============================================================================
# ALBUMENTATIONS v2 COMPATIBILITY HELPERS
# ============================================================================

def _make_rotate(limit=10, border_mode=cv2.BORDER_CONSTANT, fill=255, p=0.2):
    """Rotate совместимый с albumentations v1 и v2."""
    try:
        return A.Rotate(limit=limit, border_mode=border_mode, fill=fill, p=p)
    except TypeError:
        return A.Rotate(limit=limit, border_mode=border_mode, value=fill, p=p)


def _make_elastic(alpha=30, sigma=5, border_mode=cv2.BORDER_CONSTANT, fill=255, p=0.15):
    """ElasticTransform совместимый с v1 и v2."""
    try:
        return A.ElasticTransform(alpha=alpha, sigma=sigma, border_mode=border_mode, fill=fill, p=p)
    except TypeError:
        return A.ElasticTransform(alpha=alpha, sigma=sigma, border_mode=border_mode, value=fill, p=p)


def _make_grid_distortion(num_steps=5, distort_limit=0.15, border_mode=cv2.BORDER_CONSTANT, fill=255, p=0.1):
    """GridDistortion совместимый с v1 и v2."""
    try:
        return A.GridDistortion(num_steps=num_steps, distort_limit=distort_limit,
                                border_mode=border_mode, fill=fill, p=p)
    except TypeError:
        return A.GridDistortion(num_steps=num_steps, distort_limit=distort_limit,
                                border_mode=border_mode, value=fill, p=p)


def _make_coarse_dropout(p=0.25):
    """CoarseDropout совместимый с v1 и v2."""
    try:
        # v2 API
        return A.CoarseDropout(
            num_holes_range=(2, 6),
            hole_height_range=(12, 32),
            hole_width_range=(12, 32),
            fill=255,
            p=p
        )
    except TypeError:
        # v1 API
        return A.CoarseDropout(
            max_holes=6, max_height=32, max_width=32,
            min_holes=2, min_height=12, min_width=12,
            fill_value=255, p=p
        )


def _make_gauss_noise(p=0.25):
    """GaussNoise совместимый с v1 и v2."""
    try:
        # v2 API
        return A.GaussNoise(std_range=(0.02, 0.1), p=p)
    except TypeError:
        # v1 API
        return A.GaussNoise(var_limit=(5.0, 25.0), p=p)


# ============================================================================
# TRAINING AUGMENTATIONS
# ============================================================================

def get_train_augmentations(
    image_size: int = 1024,
    use_dashed_line: bool = True,
    dashed_line_prob: float = 0.25,
    use_realistic_dashed: bool = True,
    realistic_dashed_prob: float = 0.35
) -> A.Compose:
    """
    Balanced training augmentations for P&ID diagrams.
    Compatible with albumentations v1.x and v2.x.
    """
    transforms = []

    # STAGE 1: Dashed Line Augmentations
    if use_dashed_line:
        transforms.append(
            DashedLineAugmentation(
                gap_length=(10, 30), num_gaps=(3, 8),
                dilate_radius=4, p=dashed_line_prob
            )
        )

    if use_realistic_dashed:
        transforms.append(
            RealisticDashedLineAugmentation(
                dash_length=(15, 40), gap_length=(10, 30),
                line_thickness=(1, 3), fraction_pipes=(0.1, 0.4),
                p=realistic_dashed_prob
            )
        )

    # STAGE 2: Basic Geometric
    transforms.extend([
        A.RandomRotate90(p=1.0),
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
    ])
    transforms.append(_make_rotate(limit=10, p=0.2))

    # STAGE 3: Moderate Deformations
    transforms.append(_make_elastic(alpha=30, sigma=5, p=0.15))
    transforms.append(_make_grid_distortion(num_steps=5, distort_limit=0.15, p=0.1))

    # STAGE 4: Moderate Cutout
    transforms.append(_make_coarse_dropout(p=0.25))

    # STAGE 5: Color/Intensity
    transforms.extend([
        A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.5),
        A.RandomGamma(gamma_limit=(80, 120), p=0.3),
        A.CLAHE(clip_limit=3.0, tile_grid_size=(8, 8), p=0.2),
    ])

    # STAGE 6: Noise
    transforms.append(_make_gauss_noise(p=0.25))

    # STAGE 7: Blur
    transforms.append(
        A.OneOf([
            A.GaussianBlur(blur_limit=(3, 5), p=1.0),
            A.MotionBlur(blur_limit=3, p=1.0),
        ], p=0.15)
    )

    # STAGE 8: Normalize + Tensor
    transforms.extend([
        A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
        ToTensorV2()
    ])

    return A.Compose(transforms, additional_targets={'node_mask': 'mask'})


def get_val_augmentations() -> A.Compose:
    return A.Compose([
        A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
        ToTensorV2(),
    ], additional_targets={'node_mask': 'mask'})


def get_light_augmentations() -> A.Compose:
    return A.Compose([
        A.RandomRotate90(p=1.0),
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
        A.RandomBrightnessContrast(brightness_limit=0.15, contrast_limit=0.15, p=0.4),
        _make_gauss_noise(p=0.2),
        A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
        ToTensorV2(),
    ], additional_targets={'node_mask': 'mask'})
