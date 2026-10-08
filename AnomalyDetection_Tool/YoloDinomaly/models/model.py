# models/model.py
"""完整模型"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict

from .encoder import DINOv2Encoder
from .decoder import ReconstructionBranch
from .fusion import DualStreamFusion
from .heads import ClassificationHead, MaskHead, AnomalyScoreHead


class DinoDefectClassifier(nn.Module):
    """
    DinoDefectClassifier: 基于 Dinomaly 核心思想的缺陷分类模型

    核心创新:
    1. 利用重建差异作为异常感知信号 (来自 Dinomaly)
    2. 双流融合: 原始语义 + 异常感知
    3. 多任务学习: 分类 + 分割 + 异常评分
    4. Linear Attention + Noisy Bottleneck (Dinomaly 核心)

    架构:
    DINOv2(frozen) → [Branch A: 原始特征]
                   → [Branch B: 重建差异]  → Fusion → Heads
    """

    def __init__(
            self,
            num_classes: int = 5,
            encoder_name: str = 'dinov2_vitb14',
            selected_layers: list = None,
            decoder_dim: int = 256,
            fused_dim: int = 512,
            decoder_depth: int = 4,
            decoder_heads: int = 8,
            noise_std: float = 0.15,
            mlp_ratio: float = 4.0,
            drop_rate: float = 0.1,
            img_size: int = 518,
    ):
        super().__init__()

        if selected_layers is None:
            selected_layers = [3, 7, 11]

        self.img_size = img_size
        self.num_classes = num_classes

        # ===== Encoder (Frozen) =====
        self.encoder = DINOv2Encoder(encoder_name, selected_layers)
        encoder_dim = self.encoder.embed_dim
        patch_size = self.encoder.patch_size

        # ===== Branch B: 重建差异流 (Dinomaly Core) =====
        self.recon_branch = ReconstructionBranch(
            encoder_dim=encoder_dim,
            decoder_dim=decoder_dim,
            num_decoder_layers=decoder_depth,
            num_heads=decoder_heads,
            num_encoder_layers=len(selected_layers),
            noise_std=noise_std,
            mlp_ratio=mlp_ratio,
            drop_rate=drop_rate,
        )

        # ===== Dual-Stream Fusion =====
        self.fusion = DualStreamFusion(
            encoder_dim=encoder_dim,
            anomaly_dim=decoder_dim,
            fused_dim=fused_dim,
            num_heads=decoder_heads,
            drop=drop_rate,
        )

        # ===== Task Heads =====
        self.cls_head = ClassificationHead(
            in_dim=fused_dim,
            num_classes=num_classes,
            drop=0.3,
        )
        self.mask_head = MaskHead(
            in_dim=fused_dim,
            img_size=img_size,
            patch_size=patch_size,
        )
        self.anomaly_head = AnomalyScoreHead(
            in_dim=fused_dim,
            drop=0.2,
        )

    def forward(self, images: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Args:
            images: (B, 3, H, W)

        Returns:
            dict:
                'class_logits': (B, num_classes)
                'mask_pred': (B, 1, H, W)
                'anomaly_prob': (B, 1)
                'anomaly_map': (B, N) patch级异常分数
                'recon_loss': scalar 重建损失
        """
        # 1. DINOv2 特征提取 (frozen)
        multi_feats, cls_tokens, (h, w) = self.encoder(images)

        # 2. Branch B: 重建差异流
        anomaly_features, anomaly_scores, recon_loss = self.recon_branch(multi_feats)

        # 3. 双流融合
        orig_features = multi_feats[-1]  # 最后一层 encoder 特征
        fused = self.fusion(orig_features, anomaly_features, anomaly_scores)

        # 4. 多任务输出
        class_logits = self.cls_head(fused, anomaly_scores)
        mask_pred = self.mask_head(fused)
        anomaly_prob = self.anomaly_head(fused)

        return {
            'class_logits': class_logits,
            'mask_pred': mask_pred,
            'anomaly_prob': anomaly_prob,
            'anomaly_map': anomaly_scores,
            'recon_loss': recon_loss,
            'spatial_shape': (h, w),
        }

    def get_trainable_params(self, stage: int = 2) -> list:
        """获取不同训练阶段的可训练参数组"""
        if stage == 1:
            # Stage 1: 只训练重建分支
            return [
                {
                    'params': self.recon_branch.parameters(),
                    'lr': 1e-4,
                    'name': 'recon_branch',
                }
            ]
        else:
            # Stage 2: 差异化学习率
            return [
                {
                    'params': self.recon_branch.parameters(),
                    'lr': 1e-5,  # 小学习率微调
                    'name': 'recon_branch',
                },
                {
                    'params': self.fusion.parameters(),
                    'lr': 5e-4,
                    'name': 'fusion',
                },
                {
                    'params': self.cls_head.parameters(),
                    'lr': 5e-4,
                    'name': 'cls_head',
                },
                {
                    'params': self.mask_head.parameters(),
                    'lr': 5e-4,
                    'name': 'mask_head',
                },
                {
                    'params': self.anomaly_head.parameters(),
                    'lr': 5e-4,
                    'name': 'anomaly_head',
                },
            ]

    def get_param_count(self) -> dict:
        """统计参数量"""

        def count_params(module):
            total = sum(p.numel() for p in module.parameters())
            trainable = sum(p.numel() for p in module.parameters() if p.requires_grad)
            return total, trainable

        info = {}
        for name, module in [
            ('encoder', self.encoder),
            ('recon_branch', self.recon_branch),
            ('fusion', self.fusion),
            ('cls_head', self.cls_head),
            ('mask_head', self.mask_head),
            ('anomaly_head', self.anomaly_head),
        ]:
            total, trainable = count_params(module)
            info[name] = {
                'total': total,
                'trainable': trainable,
                'total_M': total / 1e6,
                'trainable_M': trainable / 1e6,
            }

        all_total = sum(v['total'] for v in info.values())
        all_trainable = sum(v['trainable'] for v in info.values())
        info['all'] = {
            'total': all_total,
            'trainable': all_trainable,
            'total_M': all_total / 1e6,
            'trainable_M': all_trainable / 1e6,
        }
        return info