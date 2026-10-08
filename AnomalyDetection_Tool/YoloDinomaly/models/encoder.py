# models/encoder.py
"""DINOv2 冻结编码器 - 修复版"""
import torch
import torch.nn as nn
from typing import List, Tuple


class DINOv2Encoder(nn.Module):
    """
    冻结的 DINOv2 ViT 特征提取器
    提取多层中间特征用于重建和分类
    """

    VALID_MODELS = [
        'dinov2_vits14', 'dinov2_vitb14',
        'dinov2_vitl14', 'dinov2_vitg14',
    ]

    def __init__(
        self,
        model_name: str = 'dinov2_vitb14',
        selected_layers: List[int] = None,
    ):
        super().__init__()

        if model_name not in self.VALID_MODELS:
            raise ValueError(
                f"Invalid model name: {model_name}. "
                f"Choose from {self.VALID_MODELS}"
            )

        # 加载预训练 DINOv2
        self.encoder = torch.hub.load(
            'facebookresearch/dinov2', model_name, pretrained=True
        )

        self.embed_dim = self.encoder.embed_dim
        self.patch_size = self.encoder.patch_size
        self.num_layers = len(self.encoder.blocks)

        if selected_layers is None:
            n = self.num_layers
            selected_layers = [n // 4 - 1, n // 2 - 1, 3 * n // 4 - 1, n - 1]

        self.selected_layers = selected_layers

        # 完全冻结
        for param in self.encoder.parameters():
            param.requires_grad = False
        self.encoder.eval()

        print(f"[Encoder] {model_name} loaded: embed_dim={self.embed_dim}, "
              f"patch_size={self.patch_size}, num_layers={self.num_layers}")
        print(f"[Encoder] Selected layers: {self.selected_layers}")

    def train(self, mode=True):
        """保持 encoder 始终为 eval 模式"""
        super().train(mode)
        self.encoder.eval()
        return self

    @torch.no_grad()
    def forward(self, x: torch.Tensor) -> Tuple[List[torch.Tensor], List[torch.Tensor], Tuple[int, int]]:
        """
        Args:
            x: (B, 3, H, W) 输入图像

        Returns:
            multi_layer_feats: list of (B, N, D) 多层patch特征
            cls_tokens: list of (B, D) 多层CLS token
            spatial_shape: (h, w) patch网格尺寸
        """
        B, _, H, W = x.shape
        h = H // self.patch_size
        w = W // self.patch_size

        # ====== 使用官方 get_intermediate_layers 接口 ======
        # 这是最稳定的方式，兼容所有 DINOv2 版本
        try:
            # 新版接口
            all_features = self.encoder.get_intermediate_layers(
                x,
                n=self.num_layers,        # 取全部层
                reshape=False,
                return_class_token=True,
            )
            # 返回 list of (patch_tokens, cls_token) 或 list of tensor

            multi_layer_feats = []
            cls_tokens = []

            for idx in self.selected_layers:
                if idx >= len(all_features):
                    idx = len(all_features) - 1

                feat = all_features[idx]

                if isinstance(feat, tuple):
                    # (patch_tokens, cls_token)
                    patch_tokens, cls_token = feat
                    cls_tokens.append(cls_token.float())
                    multi_layer_feats.append(patch_tokens.float())
                else:
                    # feat: (B, 1+N, D) 或 (B, N, D)
                    feat = feat.float()
                    if feat.shape[1] == h * w + 1:
                        cls_tokens.append(feat[:, 0])
                        multi_layer_feats.append(feat[:, 1:])
                    elif feat.shape[1] == h * w:
                        cls_tokens.append(feat.mean(dim=1))
                        multi_layer_feats.append(feat)
                    else:
                        cls_tokens.append(feat[:, 0])
                        multi_layer_feats.append(feat[:, 1:])

        except TypeError:
            # 旧版接口 —— get_intermediate_layers(x, n) 只接受层数
            # n 表示取最后 n 层
            all_features = self.encoder.get_intermediate_layers(
                x, n=self.num_layers
            )

            multi_layer_feats = []
            cls_tokens = []

            for idx in self.selected_layers:
                if idx >= len(all_features):
                    idx = len(all_features) - 1

                feat = all_features[idx].float()

                if feat.shape[1] == h * w + 1:
                    cls_tokens.append(feat[:, 0])
                    multi_layer_feats.append(feat[:, 1:])
                elif feat.shape[1] == h * w:
                    cls_tokens.append(feat.mean(dim=1))
                    multi_layer_feats.append(feat)
                else:
                    # 尝试手动分离
                    cls_tokens.append(feat[:, 0])
                    multi_layer_feats.append(feat[:, 1:h*w+1])

        return multi_layer_feats, cls_tokens, (h, w)

    def get_output_info(self, img_size: int = 518):
        """获取输出信息"""
        h = w = img_size // self.patch_size
        return {
            'embed_dim': self.embed_dim,
            'patch_size': self.patch_size,
            'num_patches': h * w,
            'spatial_shape': (h, w),
            'num_selected_layers': len(self.selected_layers),
            'selected_layers': self.selected_layers,
        }