# models/heads.py
"""多任务输出头"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class ClassificationHead(nn.Module):
    """
    缺陷分类头

    融合三种信息进行分类:
    1. 全局平均池化特征 (整体语义)
    2. 异常加权池化特征 (关注异常区域)
    3. 最大异常分数 (异常程度标量)
    """

    def __init__(
            self,
            in_dim: int = 512,
            num_classes: int = 5,
            drop: float = 0.3,
    ):
        super().__init__()
        self.num_classes = num_classes

        # 输入维度: in_dim * 2 + 1 (global + anomaly_weighted + max_score)
        total_dim = in_dim * 2 + 1

        self.classifier = nn.Sequential(
            nn.Linear(total_dim, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(drop),
            nn.Linear(256, 128),
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Dropout(drop * 0.5),
            nn.Linear(128, num_classes),
        )

        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, std=0.02)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def forward(
            self,
            fused_features: torch.Tensor,
            anomaly_scores: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            fused_features: (B, N, D)
            anomaly_scores: (B, N)
        Returns:
            logits: (B, num_classes)
        """
        # 1. 全局平均池化
        global_feat = fused_features.mean(dim=1)  # (B, D)

        # 2. 异常加权池化 (让分类器关注异常区域)
        weights = F.softmax(anomaly_scores * 10.0, dim=-1)  # 温度缩放增强关注
        weights = weights.unsqueeze(-1)  # (B, N, 1)
        anomaly_weighted_feat = (fused_features * weights).sum(dim=1)  # (B, D)

        # 3. 最大异常分数
        max_score = anomaly_scores.max(dim=1, keepdim=True)[0]  # (B, 1)

        # 拼接
        combined = torch.cat([global_feat, anomaly_weighted_feat, max_score], dim=-1)

        return self.classifier(combined)


class MaskHead(nn.Module):
    """
    缺陷分割头

    将 patch-level 特征上采样为 pixel-level 掩膜预测
    """

    def __init__(
            self,
            in_dim: int = 512,
            img_size: int = 518,
            patch_size: int = 14,
    ):
        super().__init__()
        self.h = img_size // patch_size
        self.w = img_size // patch_size
        self.img_size = img_size

        # Patch级预测
        self.patch_head = nn.Sequential(
            nn.Linear(in_dim, 256),
            nn.GELU(),
            nn.Linear(256, 64),
            nn.GELU(),
            nn.Linear(64, 1),
        )

        # 上采样精修卷积（pixel-level refinement）
        self.refine = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 1, kernel_size=1),
        )

        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, std=0.02)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def forward(self, fused_features: torch.Tensor) -> torch.Tensor:
        """
        Args:
            fused_features: (B, N, D)
        Returns:
            mask_logits: (B, 1, img_size, img_size)
        """
        B = fused_features.shape[0]

        # Patch级预测
        mask_logits = self.patch_head(fused_features)  # (B, N, 1)
        mask_logits = mask_logits.transpose(1, 2)  # (B, 1, N)
        mask_logits = mask_logits.reshape(B, 1, self.h, self.w)

        # 上采样到原图尺寸
        mask_logits = F.interpolate(
            mask_logits,
            size=(self.img_size, self.img_size),
            mode='bilinear',
            align_corners=False,
        )

        # 精修
        mask_logits = self.refine(mask_logits)

        return mask_logits  # (B, 1, H, W)


class AnomalyScoreHead(nn.Module):
    """
    图像级异常评分头

    预测图像是否包含异常的概率
    """

    def __init__(self, in_dim: int = 512, drop: float = 0.2):
        super().__init__()
        self.head = nn.Sequential(
            nn.Linear(in_dim, 128),
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Dropout(drop),
            nn.Linear(128, 1),
            nn.Sigmoid(),
        )

    def forward(self, fused_features: torch.Tensor) -> torch.Tensor:
        """
        Args:
            fused_features: (B, N, D)
        Returns:
            anomaly_prob: (B, 1) 取值 [0,1]
        """
        global_feat = fused_features.mean(dim=1)  # (B, D)
        return self.head(global_feat)