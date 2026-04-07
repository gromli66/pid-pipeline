"""Модуль инференса: TiledInference, предобработка, постобработка, TTA."""

from pipe_segmentation.inference.engine import (
    TiledInference,
    run_inference,
)

from pipe_segmentation.inference.preprocessing import (
    normalize_tile,
    prepare_tile_batch,
)

from pipe_segmentation.inference.postprocessing import (
    post_process_mask,
    clean_mask,
)

from pipe_segmentation.inference.tta import (
    predict_with_tta,
)

__all__ = [
    "TiledInference",
    "run_inference",
    "normalize_tile",
    "prepare_tile_batch",
    "post_process_mask",
    "clean_mask",
    "predict_with_tta",
]
