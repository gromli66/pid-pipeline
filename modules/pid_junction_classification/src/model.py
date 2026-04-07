"""
model.py

Dual-Stream CNN для классификации junction'ов.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models


class DualStreamClassifier(nn.Module):
    """
    Dual-Stream CNN: RGB + Skeleton ветки с attention fusion.
    
    Args:
        num_classes: Количество классов (3: connection, bridge, turn)
        pretrained: Использовать pretrained веса для RGB ветки
        use_multiscale: Использовать multi-scale features
    """
    
    def __init__(self, num_classes: int = 3, pretrained: bool = True, 
                 use_multiscale: bool = True):
        super().__init__()
        
        self.num_classes = num_classes
        self.use_multiscale = use_multiscale
        
        # =================================================================
        # RGB BRANCH (pretrained EfficientNet-B0)
        # =================================================================
        rgb_backbone = models.efficientnet_b0(
            weights=models.EfficientNet_B0_Weights.IMAGENET1K_V1 if pretrained else None
        )
        self.rgb_features = rgb_backbone.features
        self.rgb_avgpool = nn.AdaptiveAvgPool2d(1)
        rgb_feature_dim = 1280  # EfficientNet-B0 output
        
        # =================================================================
        # SKELETON BRANCH (EfficientNet-B0 from scratch, 1 channel)
        # =================================================================
        skel_backbone = models.efficientnet_b0(weights=None)
        
        # Модифицируем первый conv для 1 канала
        original_conv = skel_backbone.features[0][0]
        skel_backbone.features[0][0] = nn.Conv2d(
            1, original_conv.out_channels,
            kernel_size=original_conv.kernel_size,
            stride=original_conv.stride,
            padding=original_conv.padding,
            bias=False
        )
        
        self.skel_features = skel_backbone.features
        self.skel_avgpool = nn.AdaptiveAvgPool2d(1)
        skel_feature_dim = 1280
        
        # =================================================================
        # MULTI-SCALE FEATURE EXTRACTION
        # =================================================================
        if use_multiscale:
            # Промежуточные pooling слои
            self.rgb_early_pool = nn.AdaptiveAvgPool2d(1)
            self.rgb_mid_pool = nn.AdaptiveAvgPool2d(1)
            self.skel_early_pool = nn.AdaptiveAvgPool2d(1)
            self.skel_mid_pool = nn.AdaptiveAvgPool2d(1)
            
            # Размеры features на разных уровнях EfficientNet-B0
            early_dim = 40   # После block 3
            mid_dim = 112    # После block 5
            
            # Проекции для объединения
            self.early_proj = nn.Linear(early_dim * 2, 128)
            self.mid_proj = nn.Linear(mid_dim * 2, 256)
            
            total_features = rgb_feature_dim + skel_feature_dim + 128 + 256
        else:
            total_features = rgb_feature_dim + skel_feature_dim
        
        # =================================================================
        # ATTENTION MECHANISM
        # =================================================================
        self.attention_fc = nn.Sequential(
            nn.Linear(skel_feature_dim, skel_feature_dim),
            nn.Sigmoid()
        )
        
        # =================================================================
        # CLASSIFIER HEAD
        # =================================================================
        self.classifier = nn.Sequential(
            nn.Linear(total_features, 512),
            nn.BatchNorm1d(512),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(512, 256),
            nn.BatchNorm1d(256),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(256, num_classes)
        )
    
    def forward(self, rgb: torch.Tensor, skeleton: torch.Tensor) -> torch.Tensor:
        """
        Forward pass.
        
        Args:
            rgb: RGB изображение [B, 3, H, W]
            skeleton: Skeleton изображение [B, 1, H, W]
            
        Returns:
            Logits [B, num_classes]
        """
        batch_size = rgb.size(0)
        
        # =================================================================
        # RGB FEATURES
        # =================================================================
        if self.use_multiscale:
            x_rgb = rgb
            for i, layer in enumerate(self.rgb_features):
                x_rgb = layer(x_rgb)
                if i == 3:
                    rgb_early = self.rgb_early_pool(x_rgb).view(batch_size, -1)
                elif i == 5:
                    rgb_mid = self.rgb_mid_pool(x_rgb).view(batch_size, -1)
            rgb_late = self.rgb_avgpool(x_rgb).view(batch_size, -1)
        else:
            x_rgb = self.rgb_features(rgb)
            rgb_late = self.rgb_avgpool(x_rgb).view(batch_size, -1)
        
        # =================================================================
        # SKELETON FEATURES
        # =================================================================
        if self.use_multiscale:
            x_skel = skeleton
            for i, layer in enumerate(self.skel_features):
                x_skel = layer(x_skel)
                if i == 3:
                    skel_early = self.skel_early_pool(x_skel).view(batch_size, -1)
                elif i == 5:
                    skel_mid = self.skel_mid_pool(x_skel).view(batch_size, -1)
            skel_late = self.skel_avgpool(x_skel).view(batch_size, -1)
        else:
            x_skel = self.skel_features(skeleton)
            skel_late = self.skel_avgpool(x_skel).view(batch_size, -1)
        
        # =================================================================
        # ATTENTION FUSION
        # =================================================================
        attention_weights = self.attention_fc(skel_late)
        rgb_attended = rgb_late * attention_weights
        
        # =================================================================
        # FEATURE CONCATENATION
        # =================================================================
        if self.use_multiscale:
            early_fused = torch.cat([rgb_early, skel_early], dim=1)
            early_fused = self.early_proj(early_fused)
            
            mid_fused = torch.cat([rgb_mid, skel_mid], dim=1)
            mid_fused = self.mid_proj(mid_fused)
            
            combined = torch.cat([rgb_attended, skel_late, early_fused, mid_fused], dim=1)
        else:
            combined = torch.cat([rgb_attended, skel_late], dim=1)
        
        # =================================================================
        # CLASSIFICATION
        # =================================================================
        out = self.classifier(combined)
        
        return out


def load_model(checkpoint_path: str, device: str = 'cuda',
               num_classes: int = 3, use_multiscale: bool = True) -> DualStreamClassifier:
    """
    Загрузка модели из checkpoint'а.
    
    Args:
        checkpoint_path: Путь к .pth файлу
        device: Устройство
        num_classes: Количество классов
        use_multiscale: Multi-scale features
        
    Returns:
        Загруженная модель в eval режиме
    """
    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    
    if 'config' in checkpoint:
        config = checkpoint['config']
        num_classes = config.get('num_classes', num_classes)
        use_multiscale = config.get('use_multiscale', use_multiscale)
    
    model = DualStreamClassifier(
        num_classes=num_classes,
        pretrained=False,
        use_multiscale=use_multiscale
    )
    
    if 'model_state_dict' in checkpoint:
        model.load_state_dict(checkpoint['model_state_dict'])
    else:
        model.load_state_dict(checkpoint)
    
    model = model.to(device)
    model.eval()
    
    return model


def count_parameters(model: nn.Module) -> int:
    """Подсчёт количества обучаемых параметров."""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
