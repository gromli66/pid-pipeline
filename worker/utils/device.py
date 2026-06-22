"""Единый выбор устройства (GPU/CPU) для всех стадий пайплайна.

Управляется ОДНОЙ переменной окружения PID_DEVICE:
    auto  — cuda, если доступна, иначе cpu (значение по умолчанию)
    cpu   — принудительно CPU (даже если на машине есть GPU)
    cuda  — принудительно GPU; если CUDA недоступна — откат на CPU с предупреждением

Использование:
    from worker.utils.device import resolve_device
    device = resolve_device()          # "cuda" или "cpu"
"""
import os
import logging

logger = logging.getLogger(__name__)


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def resolve_device() -> str:
    """Вернуть 'cuda' или 'cpu' согласно PID_DEVICE."""
    mode = os.getenv("PID_DEVICE", "auto").strip().lower()
    if mode == "cpu":
        return "cpu"
    if mode == "cuda":
        if _cuda_available():
            return "cuda"
        logger.warning("PID_DEVICE=cuda, но CUDA недоступна — откат на CPU")
        return "cpu"
    # auto или неизвестное значение
    return "cuda" if _cuda_available() else "cpu"


def apply_torch_device_env() -> str:
    """Проставить TORCH_DEVICE (нужно Surya OCR и др.) и вернуть устройство."""
    dev = resolve_device()
    os.environ["TORCH_DEVICE"] = dev
    return dev
