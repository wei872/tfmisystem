# models/decoder.py
"""重建差异流 —— 保留Dinomaly核心（修复数值稳定性）"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import List, Tuple


class LinearAttention(nn.Module):
    """
    线性注意力 —— Dinomaly 核心组件
    修复: 增强数值稳定性，防止 AMP fp16 下溢出
    """

    def __init__(self, dim: int, num_heads: int = 8, qkv_bias: bool = True):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = dim // num_heads

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.proj = nn.Linear(dim, dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, N, C = x.shape

        #  强制 float32 计算，防止 fp16 溢出
        x_float = x.float()

        qkv = self.qkv(x_float).reshape(B, N, 3, self.num_heads, self.head_dim)
        qkv = qkv.permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)

        # 核函数映射: φ(x) = elu(x) + 1，保证非负
        q = F.elu(q) + 1.0
        k = F.elu(k) + 1.0

        #  数值稳定性: 对 q, k 做 L2 归一化防止数值爆炸
        q = q / (q.sum(dim=-1, keepdim=True) + 1e-6)
        k = k / (k.sum(dim=-1, keepdim=True) + 1e-6)

        # Linear Attention
        kv = torch.einsum('bhnd,bhne->bhde', k, v)
        qkv_out = torch.einsum('bhnd,bhde->bhne', q, kv)

        # 归一化
        k_sum = k.sum(dim=2)
        denominator = torch.einsum('bhnd,bhd->bhn', q, k_sum)
        denominator = denominator.unsqueeze(-1).clamp(min=1e-6)  #  clamp 而非 +eps

        out = qkv_out / denominator

        out = out.transpose(1, 2).reshape(B, N, C)
        out = self.proj(out)

        return out.to(x.dtype)  #  转回原始 dtype


class DecoderFFN(nn.Module):
    """Decoder 前馈网络"""

    def __init__(self, dim: int, mlp_ratio: float = 4.0, drop: float = 0.0):
        super().__init__()
        hidden_dim = int(dim * mlp_ratio)
        self.fc1 = nn.Linear(dim, hidden_dim)
        self.act = nn.GELU()
        self.fc2 = nn.Linear(hidden_dim, dim)
        self.drop = nn.Dropout(drop)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class DecoderBlock(nn.Module):
    """Decoder Transformer Block with Linear Attention"""

    def __init__(
        self,
        dim: int,
        num_heads: int = 8,
        mlp_ratio: float = 4.0,
        drop: float = 0.0,
    ):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)
        self.attn = LinearAttention(dim, num_heads)
        self.norm2 = nn.LayerNorm(dim)
        self.ffn = DecoderFFN(dim, mlp_ratio, drop)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm1(x))
        x = x + self.ffn(self.norm2(x))
        return x


class NoisyBottleneck(nn.Module):
    """加噪瓶颈层"""

    def __init__(self, noise_std: float = 0.15):
        super().__init__()
        self.noise_std = noise_std

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.training and self.noise_std > 0:
            noise = torch.randn_like(x) * self.noise_std
            return x + noise
        return x

    def extra_repr(self) -> str:
        return f'noise_std={self.noise_std}'


class FeatureAggregator(nn.Module):
    """多层特征聚合模块"""

    def __init__(self, encoder_dim: int, decoder_dim: int, num_layers: int):
        super().__init__()
        self.projections = nn.ModuleList([
            nn.Sequential(
                nn.Linear(encoder_dim, decoder_dim),
                nn.LayerNorm(decoder_dim),
            )
            for _ in range(num_layers)
        ])
        self.layer_weights = nn.Parameter(torch.ones(num_layers) / num_layers)

    def forward(self, features: List[torch.Tensor]) -> torch.Tensor:
        weights = F.softmax(self.layer_weights, dim=0)
        projected = []
        for i, feat in enumerate(features):
            proj = self.projections[i](feat.float())  #  确保 float32
            projected.append(proj * weights[i])
        return sum(projected)


class ReconstructionBranch(nn.Module):
    """
    重建差异流：保留 Dinomaly 核心能力（修复版）
    """

    def __init__(
        self,
        encoder_dim: int = 768,
        decoder_dim: int = 256,
        num_decoder_layers: int = 4,
        num_heads: int = 8,
        num_encoder_layers: int = 3,
        noise_std: float = 0.15,
        mlp_ratio: float = 4.0,
        drop_rate: float = 0.0,
    ):
        super().__init__()
        self.encoder_dim = encoder_dim
        self.decoder_dim = decoder_dim
        self.num_encoder_layers = num_encoder_layers
        self.num_decoder_layers = num_decoder_layers

        self.aggregator = FeatureAggregator(
            encoder_dim, decoder_dim, num_encoder_layers
        )
        self.bottleneck = NoisyBottleneck(noise_std)

        self.decoder_blocks = nn.ModuleList([
            DecoderBlock(decoder_dim, num_heads, mlp_ratio, drop_rate)
            for _ in range(num_decoder_layers)
        ])

        self.output_projs = nn.ModuleList([
            nn.Sequential(
                nn.LayerNorm(decoder_dim),
                nn.Linear(decoder_dim, encoder_dim),
            )
            for _ in range(num_encoder_layers)
        ])

        self._compute_output_indices()
        self._init_weights()

    def _compute_output_indices(self):
        indices = torch.linspace(
            0, self.num_decoder_layers - 1, self.num_encoder_layers
        ).long().tolist()
        self.output_indices = indices

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
        encoder_features: List[torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Returns:
            anomaly_features: (B, N, decoder_dim)
            anomaly_scores: (B, N)
            recon_loss: scalar
        """
        #  确保输入为 float32
        encoder_features_f32 = [f.float() for f in encoder_features]

        # 1. 聚合
        aggregated = self.aggregator(encoder_features_f32)

        # 2. 加噪
        bottleneck_out = self.bottleneck(aggregated)

        # 3. Decoder
        x = bottleneck_out
        decoder_outputs = []
        for block in self.decoder_blocks:
            x = block(x)
            decoder_outputs.append(x)

        # 4. 计算重建误差
        recon_loss_maps = []
        total_recon_loss = torch.tensor(0.0, device=x.device)

        for i, dec_idx in enumerate(self.output_indices):
            recon_feat = self.output_projs[i](decoder_outputs[dec_idx])
            target_feat = encoder_features_f32[i].detach()

            #  安全的余弦距离计算
            recon_norm = F.normalize(recon_feat, p=2, dim=-1, eps=1e-6)
            target_norm = F.normalize(target_feat, p=2, dim=-1, eps=1e-6)
            cos_sim = (recon_norm * target_norm).sum(dim=-1)  # (B, N)
            cos_sim = cos_sim.clamp(-1.0, 1.0)                #  夹紧到 [-1, 1]
            cos_dist = 1.0 - cos_sim

            recon_loss_maps.append(cos_dist)
            total_recon_loss = total_recon_loss + cos_dist.mean()

        total_recon_loss = total_recon_loss / max(len(self.output_indices), 1)

        # 5. 异常分数
        anomaly_scores = torch.stack(recon_loss_maps, dim=-1).mean(dim=-1)  # (B, N)

        #  安全的归一化
        score_weight = anomaly_scores.detach()
        s_min = score_weight.min()
        s_max = score_weight.max()
        s_range = s_max - s_min
        if s_range > 1e-8:
            score_weight = (score_weight - s_min) / s_range
        else:
            score_weight = torch.zeros_like(score_weight)

        anomaly_features = x * (1.0 + score_weight.unsqueeze(-1))

        return anomaly_features, anomaly_scores, total_recon_loss