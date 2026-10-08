# models/fusion.py
"""双流特征融合模块"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class CrossAttentionBlock(nn.Module):
    """交叉注意力块"""

    def __init__(self, dim: int, num_heads: int = 8, drop: float = 0.0):
        super().__init__()
        self.cross_attn = nn.MultiheadAttention(
            embed_dim=dim, num_heads=num_heads,
            dropout=drop, batch_first=True,
        )
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.ffn = nn.Sequential(
            nn.Linear(dim, dim * 4),
            nn.GELU(),
            nn.Dropout(drop),
            nn.Linear(dim * 4, dim),
            nn.Dropout(drop),
        )
        self.norm3 = nn.LayerNorm(dim)

    def forward(
            self,
            query: torch.Tensor,
            context: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            query: (B, N, D)
            context: (B, N, D)
        Returns:
            yolo: (B, N, D)
        """
        # 交叉注意力
        q = self.norm1(query)
        c = self.norm2(context)
        attn_out, _ = self.cross_attn(q, c, c)
        x = query + attn_out

        # FFN
        x = x + self.ffn(self.norm3(x))
        return x


class GatedFusion(nn.Module):
    """门控融合"""

    def __init__(self, dim: int):
        super().__init__()
        self.gate = nn.Sequential(
            nn.Linear(dim * 2, dim),
            nn.LayerNorm(dim),
            nn.Sigmoid(),
        )

    def forward(self, feat_a: torch.Tensor, feat_b: torch.Tensor) -> torch.Tensor:
        """
        Args:
            feat_a, feat_b: (B, N, D)
        Returns:
            fused: (B, N, D)
        """
        gate_input = torch.cat([feat_a, feat_b], dim=-1)  # (B, N, 2D)
        gate_weight = self.gate(gate_input)  # (B, N, D)
        fused = gate_weight * feat_a + (1.0 - gate_weight) * feat_b
        return fused


class DualStreamFusion(nn.Module):
    """
    双流融合模块

    融合来自两个分支的特征:
    - Branch A: 原始 DINOv2 特征 (语义信息)
    - Branch B: 重建差异特征 (异常感知信息)

    方法: 投影 → 交叉注意力 → 门控融合 → 输出投影
    """

    def __init__(
            self,
            encoder_dim: int = 768,
            anomaly_dim: int = 256,
            fused_dim: int = 512,
            num_heads: int = 8,
            drop: float = 0.0,
    ):
        super().__init__()

        # 原始特征投影
        self.orig_proj = nn.Sequential(
            nn.Linear(encoder_dim, fused_dim),
            nn.LayerNorm(fused_dim),
            nn.GELU(),
        )

        # 异常特征投影
        self.anom_proj = nn.Sequential(
            nn.Linear(anomaly_dim, fused_dim),
            nn.LayerNorm(fused_dim),
            nn.GELU(),
        )

        # 交叉注意力：原始特征关注异常区域
        self.cross_attn = CrossAttentionBlock(fused_dim, num_heads, drop)

        # 门控融合
        self.gated_fusion = GatedFusion(fused_dim)

        # 输出投影
        self.output_proj = nn.Sequential(
            nn.Linear(fused_dim, fused_dim),
            nn.LayerNorm(fused_dim),
            nn.GELU(),
        )

        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, std=0.02)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.LayerNorm):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)

    def forward(
            self,
            orig_features: torch.Tensor,
            anomaly_features: torch.Tensor,
            anomaly_scores: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            orig_features: (B, N, D_enc) 原始encoder最后一层特征
            anomaly_features: (B, N, D_anom) 重建差异特征
            anomaly_scores: (B, N) 每个patch的异常分数

        Returns:
            fused: (B, N, fused_dim) 融合特征
        """
        # 投影到统一维度
        orig = self.orig_proj(orig_features)  # (B, N, F)
        anom = self.anom_proj(anomaly_features)  # (B, N, F)

        # 交叉注意力：让原始特征关注异常区域
        cross_out = self.cross_attn(orig, anom)  # (B, N, F)

        # 门控融合
        fused = self.gated_fusion(cross_out, anom)  # (B, N, F)

        # 输出投影
        fused = self.output_proj(fused)  # (B, N, F)

        return fused