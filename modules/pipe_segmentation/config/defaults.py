"""
Дефолтные параметры для всех команд pipe_segmentation.

Этот модуль содержит константы по умолчанию, используемые во всех
компонентах системы: подготовка данных, обучение, инференс.
"""

# =============================================================================
# ТАЙЛИНГ
# =============================================================================

DEFAULT_TILE_SIZE = 1024      # Размер тайла в пикселях
DEFAULT_OVERLAP = 128         # Перекрытие между тайлами
DEFAULT_STRIDE = DEFAULT_TILE_SIZE - DEFAULT_OVERLAP  # 896

# Фильтрация тайлов
DEFAULT_MIN_PIPE_PIXELS = 100       # Мин. пикселей труб для сохранения тайла
DEFAULT_MIN_NODE_PIXELS = 50        # Мин. пикселей узлов
DEFAULT_EMPTY_THRESHOLD = 0.90      # Порог пустого тайла (>90% белого)

# =============================================================================
# БИНАРИЗАЦИЯ ИЗОБРАЖЕНИЙ
# =============================================================================

# [FIX 1.1] Смягчённые параметры для сохранения пунктира
BINARIZE_ENABLED = True
BINARIZE_METHOD = 'adaptive'         # 'adaptive' | 'otsu' | 'fixed'
BINARIZE_BLOCK_SIZE = 31             # 51→31: меньше окно = лучше сохранение коротких штрихов
BINARIZE_C = 7                       # 10→7: менее агрессивный порог
BINARIZE_FIXED_THRESHOLD = 128
BINARIZE_INVERT = True

# [FIX 1.2] Порог валидации бинаризации
BINARIZE_MIN_BLACK_RATIO = 0.005     # Минимум 0.5% чёрных пикселей после бинаризации

# =============================================================================
# НОРМАЛИЗАЦИЯ (ImageNet)
# =============================================================================

IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

# [FIX 7.1] Нормализация 4-го канала (node mask)
NODE_CHANNEL_MEAN = 0.05  # Среднее значение node-маски (~5% площади = узлы)
NODE_CHANNEL_STD = 0.2    # Стандартное отклонение

# =============================================================================
# МОДЕЛЬ
# =============================================================================

DEFAULT_MODEL_CONFIG = {
    'architecture': 'UnetPlusPlus',
    'encoder_name': 'efficientnet-b3',
    'encoder_weights': 'imagenet',
    'in_channels': 4,
    'classes': 1,
    'activation': None,
    # SCSE attention в decoder (включать только при ≥16 GB VRAM)
    'decoder_attention_type': None,
    # [NEW] Dual head: mask + skeleton
    'dual_head': False,
}

# =============================================================================
# LOSS
# =============================================================================

DEFAULT_LOSS_CONFIG = {
    'dice_weight': 1.0,
    'focal_weight': 1.0,
    # [v3] clCE (MICCAI 2024) вместо clDice — робастнее
    'use_clce': True,                # True=clCE, False=clDice
    'clce_weight': 0.7,             # Увеличен: 0.5→0.7 для лучшей топологии
    'cldice_weight': 0.5,           # Используется если use_clce=False
    'focal_alpha': 0.25,
    'focal_gamma': 2.0,
    'focal_pos_weight': 10.0,
    'cldice_iters': 10,              # [v3] 5→10 для зазоров пунктира
    'ohem_enabled': True,
    'ohem_top_k_ratio': 0.5,
    # [NEW] Multi-task (dual head)
    'skeleton_weight': 0.5,          # Вес skeleton BCE loss
    'skeleton_pos_weight': 20.0,     # pos_weight для BCE (скелет ~1% пикселей)
}

# =============================================================================
# ОБУЧЕНИЕ (train)
# =============================================================================

DEFAULT_TRAIN_EPOCHS = 50
DEFAULT_TRAIN_BATCH_SIZE = 2
DEFAULT_TRAIN_ACCUMULATION_STEPS = 4
DEFAULT_TRAIN_ENCODER_LR = 1e-5
DEFAULT_TRAIN_DECODER_LR = 1e-4
DEFAULT_TRAIN_WEIGHT_DECAY = 1e-4
DEFAULT_TRAIN_PATIENCE = 15
DEFAULT_GRADIENT_CLIP = 1.0

# =============================================================================
# FINE-TUNE (finetune)
# =============================================================================

DEFAULT_FINETUNE_EPOCHS = 20
DEFAULT_FINETUNE_BATCH_SIZE = 2
DEFAULT_FINETUNE_ACCUMULATION_STEPS = 4
DEFAULT_FINETUNE_LR = 1e-5
DEFAULT_FINETUNE_WEIGHT_DECAY = 1e-5
DEFAULT_FINETUNE_PATIENCE = 10
DEFAULT_FINETUNE_FREEZE_ENCODER = 0

# =============================================================================
# ИНФЕРЕНС (test/infer)
# =============================================================================

DEFAULT_INFERENCE_BATCH_SIZE = 4
DEFAULT_THRESHOLD = 0.5
# [FIX 8.2] TTA включен по умолчанию
DEFAULT_USE_TTA = True
DEFAULT_USE_POSTPROCESS = True

# =============================================================================
# ПОСТОБРАБОТКА
# =============================================================================

DEFAULT_POSTPROCESS_CONFIG = {
    'enabled': True,
    'remove_small_objects': 100,
    'closing_kernel_size': 0,       # v3: отключено — раздувает трубы
    'opening_kernel_size': 0,       # v3: отключено
    # Удаление рамки чертежа
    'remove_border_frame': {
        'enabled': True,
        'margin': 30,               # Полоса краёв (px)
        'min_length_ratio': 0.5,    # Мин. доля стороны для рамки
    },
    # Скелетное соединение разрывов (строгое)
    'skeleton_gap_fill': {
        'enabled': True,
        'max_gap': 40,              # 80→40: меньше = меньше FP
        'direction_tolerance': 20,   # 30→20: строже = меньше FP
        'min_segment_length': 15,
        'verify_path': True,         # Проверять что путь не пересекает трубы
    },
    # Заполнение дыр внутри контуров
    'fill_holes': {
        'enabled': True,
        'max_hole_size': 500,
    },
}

# [FIX 4.2] Увеличен clamp pos_weight
POS_WEIGHT_MAX_CLAMP = 50  # 20→50

# =============================================================================
# SPLIT
# =============================================================================

DEFAULT_SPLIT_RATIOS = "0.7/0.15/0.15"
DEFAULT_FINETUNE_SPLIT_RATIOS = "0.8/0.2"

# =============================================================================
# ПУТИ К МАСКАМ
# =============================================================================

MASKS_PIPES_DIR = 'pipes'
MASKS_NODES_DIR = 'nodes'

SPLIT_IMAGES_DIR = 'images'
SPLIT_PIPE_MASKS_DIR = 'pipe_masks'
SPLIT_NODE_MASKS_DIR = 'node_masks'

# =============================================================================
# РАЗНОЕ
# =============================================================================

DEFAULT_RANDOM_SEED = 42
DEFAULT_NUM_WORKERS = 0
DEFAULT_PIN_MEMORY = True
DEFAULT_DEVICE = 'cuda'

COCO_PIPE_CATEGORY = 'truba'
COCO_ANNOTATION_CATEGORY = 'annotation'

IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg']
