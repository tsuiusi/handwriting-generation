import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
from torchvision.models import mobilenet_v2

import os
import sys
import pickle
import time

import numpy as np
import matplotlib.pyplot as plt

def get_same_padding(kernel_size):
    if isinstance(kernel_size, int):
        return (kernel_size - 1) // 2
    else:
        return [(k - 1) // 2 for k in kernel_size]

def reshape_up(x, factor=2):
    x_shape = x.shape
    return x.reshape(x_shape[0], x_shape[1] * factor, x_shape[2] // factor)


class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 5000, pos_factor = 1):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term) / pos_factor
        pe[:, 0, 1::2] = torch.cos(position * div_term) / pos_factor
        self.register_buffer('pe', pe)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Arguments:
            x: Tensor, shape ``[seq_len, batch_size, embedding_dim]``
        """
        x = x + self.pe[:x.size(0)]
        return self.dropout(x)

class MLP(nn.Module):
    def __init__(self, input_dims: int, hidden_dims:int = 768, act_before: bool =True):
        self.layers = []
        if act_before:
            layers.append(nn.SiLU())

        layers.extend([
            nn.Linear(input_dims, hidden_dims), 
            nn.SiLU(), 
            nn.Linear(hidden_dims, input_dims)
            ])

        self.ff = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.ff(x)

class AffineTransformLayer(nn.Module):
    """
    Used for conditional normalization
    """
    def __init__(self, num_features):
        super().__init__()
        self.gamma_emb = nn.Linear(1, num_features)
        self.beta_emb = nn.Linear(1, num_features)

        nn.init.ones_(self.gamma_emb.weight)
        nn.init.zeros_(self.gamma_emb.bias)

    def forward(self, x, sigma):
        if sigma.dim() == 1:
            sigma = sigma.unsqueeze(1)

        gammas = self.gamma_emb(sigma).unsqueeze(2).unsqueeze(3)
        betas = self.beta_emb(sigma).unsqueeze(2).unsqueeze(3)

        return x * gammas + betas 

class ConvSubLayer(nn.Module):
    def __init__(self, filters, dils=[1,1], drop_rate=0.0): # activation SiLU
        super().__init__()
        self.silu = nn.SiLU()
        self.affine1 = AffineTransformLayer(filters // 2)
        self.affine2 = AffineTransformLayer(filters)
        self.affine3 = AffineTransformLayer(filters)
        
        self.conv_skip = nn.Conv1D(filters, filters, 3, padding=1)
        self.conv1 = Conv1D(filters // 2, 3, dilation=dils[1])
        self.conv2 = Conv1D(filters, 3, dilation=dils[1])

        self.fc = nn.Linear(filters, filters)
        self.dropout = nn.Dropout(drop_rate)

    def forward(self, x, alpha):
        x_skip = self.conv.skip(x)
        x = self.conv1(self.silu(x))
        x = self.dropout(self.affine1(x, alpha))

        x = self.conv2(sel.silu(x))
        x = self.dropout(self.affine2(x, alpha))

        x = self.fc(self.silu(x))
        x = self.drop(self.affine3(x, alpha))
        x += x_skip

        return x

class StyleExtractor(nn.Module):
    def __init__(self):
        super().__init__()
        self.mobilenet = mobilenet_v2(pretrained=True)
        self.features = nn.Sequential(*list(self.mobilenet.features))

        self.local_pool = nn.AvgPool2d((3, 3))
        self.freeze_all_layers()

    def freeze_all_layers(self):
        for param in self.mobilenet.parameters():
            param.requires_grad = False

    def forward(self, im, im2=None, get_similarity=False, training=False):
        x = im.float() / 127.5 - 1
        x = x.repeat(1, 3, 1, 1)
        x = self.features(x)
        x = self.local_pool(x)
        x = x.squeeze(2)

        return x

class DecoderLayer(nn.Module):
    def __init__(self, d_model: int, num_heads: int, drop_rate: float = 0.1, pos_factor: float = 1):
        super().__init__()
        self.text_pe = PositionalEncoding(d_model=d_model, max_len=2000)
        self.stroke_pe = PositionalEncoding(d_model=d_model, max_len=2000)
        self.dropout = nn.Dropout(drop_rate)
        self.layernorm = nn.LayerNorm(eps=1e-6, elementwise_affine=False)
        self.text_dense = nn.Linear(d_model, d_model)

        self.mha1 = nn.MultiHeadedAttention(d_model, num_heads)
        self.mha2 = nn.MultiHeadedAttention(d_model, num_heads)
        self.ff = MLP(d_model, d_model*2)
        self.affine0 = AffineTransformLayer(d_model)
        self.affine1 = AffineTransformLayer(d_model)
        self.affine2 = AffineTransformLayer(d_model)
        self.affine3 = AffineTransformLayer(d_model)

        self.silu = nn.SiLU()

    def forward(self, x, text, sigma, text_mask):
        text = self.text_dense(self.silu(text))
        text = self.affine0(self.layernorm(text), sigma)
        text_pe = text + self.text_pe[:, :text.shape[1]]  # Use square brackets instead of parentheses

        x_pe = x + self.stroke_pe[:, x.shape[1]]
        x2, att = self.mha1(x_pe, text_pe, text, text_mask)
        x2 = self.layernorm(self.dropout(x2))
        x2 = self.affine1(x2, sigma) + x

        x2_pe = x2 + self.stroke_pe[:, x.shape[1]]
        x3, _ = self.mha2(x2_pe, x2_pe, x2)
        x3 = self.layernorm(x2 + self.drop(x3))
        x3 = self.affine2(x3, sigma)

        x4 = self.mlp(x3)
        x4 = self.dropout(x4) + x3
        out = self.affine3(self.layernorm(x4), sigma)
        return out, att

class Text_Style_Encoder(nn.Module):
    def __init__(self, d_model: int, input_dims: int = 512):
        super().__init__()
        self.emb = nn.Embedding(73, d_model)
        self.text_conv = nn.Conv1d(in_channels=d_model, out_channels=d_model, kernel_size=3, padding=get_same_padding(3))
        self.style_mlp = MLP(d_model, input_dims)
        self.mha = MultiHeadedAttention(d_model, 8)
        self.layernorm = nn.LayerNorm(eps=1e-6, elementwise_affine=False)
        self.dropout = nn.Dropout(p=0.3)

        self.affine1 = AffineTransformLayer(d_model)
        self.affine2 = AffineTransformLayer(d_model)
        self.affine3 = AffineTransformLayer(d_model)
        self.affine4 = AffineTransformLayer(d_model)
        self.text_mlp = MLP(d_model, d_model*2)

    def forward(self, x, style, sigma):
        style = reshape_up(self.dropout(style), 5)
        style = self.affine1(self.layernorm(self.style_mlp(style)), sigma)
        text = self.emb(text)
        text = self.affine2(self.layernorm(text), sigma)
        
        mha_out, _ = self.mha(text, style, style)
        text = self.affine3(self.layernorm(text + mha_out), sigma)
        text_out = self.affine4(self.layernorm(self.text_mlp(text)), sigma)
        return text_out

class DiffusionWriter(nn.Module):
    def __init__(self, num_layers: int = 4, c1: int = 128, c2: int = 192, c3: int = 256, drop_rate: float = 0.1, num_heads:int = 8):
        super().__init__()
        self.input_fc = nn.Linear(c1, c1)
        self.sigma_mlp = MLP(c1 // 4, 2048)
        self.enc1 = ConvSubLayer(c1, [1,2])
        self.enc2 = ConvSubLayer(c2, [1, 2])
        self.enc3 = DecoderLayer(c2, 3, drop_rate, pos_factor=4)
        self.enc4 = ConvSubLayer(c3, [1, 2])
        self.enc5 = DecoderLayer(c3, 4, drop_rate, pos_factor=2)
        self.pool = nn.AvgPool1d(2)
        self.upsample = UpSample(2)

        self.skip_conv1 = nn.Conv1d(c2, c2, kernel_size=3, padding=get_same_padding(3))
        self.skip_conv2 = nn.Conv1d(c3, c3, kernel_size=3, padding=get_same_padding(3))
        self.skip_conv3 = nn.Conv1d(c2*2, c2*2, kernel_size=3, padding=get_same_padding(3))

        self.text_style_encoder = Text_Style_Encoder(c2*2, c2*4)
        self.att_fc = nn.Linear(c2*2, c2*2)
        self.att_layers = [DecoderLayer(c2*2, 6, drop_rate) for _ in range(num_layers)]

        self.dec3 = ConvSubLayer(c3, [1,2])
        self.dec2 = ConvSubLayer(c2, [1,1])
        self.dec1 = ConvSubLayer(c1, [1,1])

        self.output_fc = nn.Linear(2, 2)
        self.pen_lifts_fc = nn.Sequential(nn.Linear(1, 1), nn.Sigmoid())

    def forward(self, strokes, text, sigma, style_vector):
        sigma = self.sigma_mlp(sigma)
        text_mask = create_padding_mask(text)
        text = self.text_style_encoder(text, style_vector, sigma)

        x = self.input_fc(strokes)
        h1 = self.enc1(x, sigma)
        h2 = self.pool(h1)

        h2 = self.enc2(h2, sigma)
        h2, _ = self.enc3(h2, text, sigma, text_mask)
        h3 = self.pool(h2)

        h3 = self.enc4(h3, sigma)
        h3, _ = self.enc5(h3, text, sigma, text_mask)
        x = self.pool(h3)
        
        x = self.att_fc(x)
        for att_layer in self.att_layers:
            x, att = att_layer(x, text, sigma, text_mask)

        x = self.upsample(x) + self.skip_conv3(h3)
        x = self.dec3(x, sigma)

        x = self.upsample(x) + self.skip_conv2(h2)
        x = self.dec2(x, sigma)

        x = self.upsample(x) + self.skip_conv1(h1)
        x = self.dec1(x, sigma)
        
        output = self.output_fc(x)
        pl = self.pen_lifts_fc(x)
        return output, pl, att
