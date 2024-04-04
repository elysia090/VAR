import math
from functools import partial
from typing import List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F

import dist
from models.basic import AdaLNSABlock, SABlock
from models.head import AdaLNBeforeHead, MultiInpIdentity
from models.helpers import gumbel_softmax_with_rng, sample_with_top_k_top_p_
from models.vae import DiscreteVAE, VectorQuantizer2


class SharedAdaLin(nn.Linear):
    def forward(self, cond_BD):
        C = self.weight.shape[0] // 6
        return super().forward(cond_BD).view(-1, 1, 6, C)   # B16C


class VAR(nn.Module):
    def __init__(
        self, vae_local: DiscreteVAE,
        num_classes=1000, norm_eps=1e-6, aln=-1, aln_gamma_init=-1, shared_aln=False, cond_drop_rate=0.1,
        depth=16, embed_dim=1024, num_heads=16, mlp_ratio=4., drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
        layer_scale=-1., tau=4, cos_attn=False,
        patch_nums=(1, 2, 3, 4, 5, 6, 8, 10, 13, 16),   # 10 steps by default
        flash_if_available=True, fused_if_available=True,
    ):
        super().__init__()
        # 0. hyperparameters
        assert embed_dim % num_heads == 0
        self.Cvae, self.V = vae_local.Cvae, vae_local.vocab_size
        self.depth, self.C, self.D, self.num_heads = depth, embed_dim, embed_dim, num_heads
        self.using_aln, self.aln_init, self.aln_gamma_init, self.layer_scale = aln >= 0, aln, aln_gamma_init, layer_scale
        if self.using_aln:
            print(f'[aln] using AdaLNSABlock with AdaLN {aln=:g}, {aln_gamma_init=:g}. The {layer_scale=:g} is useless because only SABlock uses layer_scale', flush=True)
        
        self.cond_drop_rate = cond_drop_rate
        self.prog_si = -1   # progressive training
        
        self.patch_nums: Tuple[int] = patch_nums
        self.L = sum(pn ** 2 for pn in self.patch_nums)
        self.first_l = self.patch_nums[0] ** 2
        self.begin_ends = []
        cur = 0
        for i, pn in enumerate(self.patch_nums):
            self.begin_ends.append((cur, cur+pn ** 2))
            cur += pn ** 2
        
        self.num_stages_minus_1 = len(self.patch_nums) - 1
        self.rng = torch.Generator(device=dist.get_device())
        
        # 1. input (word) embedding
        quant: VectorQuantizer2 = vae_local.quantize
        self.vae_quant_proxy: Tuple[VectorQuantizer2] = (quant,)
        self.word_embed = nn.Linear(self.Cvae, self.C)
        
        # 2. class embedding
        init_std = math.sqrt(1 / self.C / 3)
        self.num_classes = num_classes
        self.selecting_idx = torch.full((1, num_classes), fill_value=1/num_classes, dtype=torch.float32, device=dist.get_device())
        self.class_emb = nn.Embedding(self.num_classes + 1, self.C)
        nn.init.trunc_normal_(self.class_emb.weight.data, mean=0, std=init_std)
        self.pos_start = nn.Parameter(torch.empty(1, self.first_l, self.C))
        nn.init.trunc_normal_(self.pos_start.data, mean=0, std=init_std)
        
        # 3. absolute position embedding
        pos_1LC = []
        for i, pn in enumerate(self.patch_nums):
            pe = torch.empty(1, pn*pn, self.C)
            nn.init.trunc_normal_(pe, mean=0, std=init_std)
            pos_1LC.append(pe)
        pos_1LC = torch.cat(pos_1LC, dim=1)     # 1, L, C
        assert tuple(pos_1LC.shape) == (1, self.L, self.C)
        self.pos_1LC = nn.Parameter(pos_1LC)
        # level embedding (similar to GPT's segment embedding, used to distinguish different levels of token pyramid)
        self.lvl_embed = nn.Embedding(len(self.patch_nums), self.C)
        nn.init.trunc_normal_(self.lvl_embed.weight.data, mean=0, std=init_std)
        
        # 4. backbone blocks
        self.shared_ada_lin = nn.Sequential(nn.SiLU(inplace=False), SharedAdaLin(self.D, 6*self.C)) if shared_aln and self.using_aln else nn.Identity()
        
        norm_layer = partial(nn.LayerNorm, eps=norm_eps)
        self.drop_path_rate = drop_path_rate
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule (linearly increasing)
        self.blocks = nn.ModuleList([
            AdaLNSABlock(
                cond_dim=self.D, shared_aln=shared_aln,
                block_idx=block_idx, embed_dim=self.C, norm_layer=norm_layer, num_heads=num_heads, mlp_ratio=mlp_ratio,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[block_idx], last_drop_p=0 if block_idx == 0 else dpr[block_idx-1],
                tau=tau, cos_attn=cos_attn,
                flash_if_available=flash_if_available, fused_if_available=fused_if_available,
            ) if self.using_aln else SABlock(
                layer_scale=layer_scale,
                block_idx=block_idx, embed_dim=self.C, norm_layer=norm_layer, num_heads=num_heads, mlp_ratio=mlp_ratio,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[block_idx], last_drop_p=0 if block_idx == 0 else dpr[block_idx-1],
                tau=tau, cos_attn=cos_attn,
                flash_if_available=flash_if_available, fused_if_available=fused_if_available,
            )
            for block_idx in range(depth)
        ])
        
        if self.blocks[-1].fused_add_norm_fn is not None:
            self.gamma2_last = nn.Parameter(self.layer_scale * torch.ones(embed_dim), requires_grad=True) if self.layer_scale >= 0 else 1
        else:
            self.gamma2_last = None
        
        fused_add_norm_fns = [b.fused_add_norm_fn is not None for b in self.blocks]
        self.using_fused_add_norm_fn = any(fused_add_norm_fns)
        print(
            f'\n[constructor]  ==== flash_if_available={flash_if_available} ({sum(b.attn.using_flash for b in self.blocks)}/{self.depth}), fused_if_available={fused_if_available} (fusing_add_ln={sum(fused_add_norm_fns)}/{self.depth}, fusing_mlp={sum(b.ffn.fused_mlp_func is not None for b in self.blocks)}/{self.depth}) ==== \n'
            f'    [vGPT config ] embed_dim={embed_dim}, num_heads={num_heads}, depth={depth}, mlp_ratio={mlp_ratio}\n'
            f'    [drop ratios ] drop_rate={drop_rate}, attn_drop_rate={attn_drop_rate}, drop_path_rate={drop_path_rate:g} ({torch.linspace(0, drop_path_rate, depth)})',
            end='\n\n', flush=True
        )
        
        # 5. attention mask used in training (for masking out the future)
        #    no mask for inference, as kv cache is used
        d: torch.Tensor = torch.cat([torch.full((pn*pn,), i) for i, pn in enumerate(self.patch_nums)]).view(1, self.L, 1)
        dT = d.transpose(1, 2)    # dT: 11L
        lvl_1L = dT[:, 0].contiguous()
        self.register_buffer('lvl_1L', lvl_1L)
        attn_bias_for_masking = torch.where(d >= dT, 0., -torch.inf).reshape(1, 1, self.L, self.L)
        self.register_buffer('attn_bias_for_masking', attn_bias_for_masking.contiguous())
        
        # 6. classifier head
        if self.using_aln:
            self.head_nm = AdaLNBeforeHead(self.C, self.D, norm_layer=norm_layer)
            self.head = nn.Linear(self.C, self.V)
        else:
            self.head_nm = MultiInpIdentity()
            self.head = nn.Sequential(norm_layer(self.C), nn.Linear(self.C, self.V))
    
    def get_logits(self, h_or_h_and_residual: Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]], cond_BD: Optional[torch.Tensor], tau=1):
        if not isinstance(h_or_h_and_residual, torch.Tensor):
            h, resi = h_or_h_and_residual   # is h_and_residual, so fused_add_norm must be used, so self.gamma2_last is not None
            h = resi + self.gamma2_last * self.blocks[-1].drop_path(h)
        else:   # is h, so fused_add_norm is not used, and self.gamma2_last is None
            h = h_or_h_and_residual
        return self.head(self.head_nm(h.float(), cond_BD).float()).float().mul(1/tau)
    
    @torch.no_grad()
    def autoregressive_infer_cfg(
        self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
        g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
        returns_vemb=False, gumbel=0, tau=1,
    ) -> List[torch.Tensor]:   # returns List[idx_Bl]
        """
        only used for inference, on autoregressive mode
        :param B: batch size
        :param label_B: imagenet label; if None, randomly sampled
        :param g_seed: random seed
        :param cfg: classifier-free guidance ratio
        :param top_k: top-k sampling
        :param top_p: top-p sampling
        :param returns_vemb: whether to return vae embedding or idx_Bl
        :param gumbel: gumbel softmax ratio
        :param tau: temperature for logits
        :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
        """
        if g_seed is None: rng = None
        else: self.rng.manual_seed(g_seed); rng = self.rng
        
        if label_B is None:
            label_B = torch.multinomial(self.selecting_idx, num_samples=B, replacement=True, generator=rng).reshape(B)
        elif isinstance(label_B, int):
            label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)
        
        sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        
        lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
        cur_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
        cur_L, ret = 0, []
        f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])
        
        for b in self.blocks: b.attn.kv_caching(True)
        for si, pn in enumerate(self.patch_nums):   # si: i-th segment
            ratio = si / self.num_stages_minus_1
            # last_L = cur_L
            cur_L += pn*pn
            # assert self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].sum() == 0, f'AR with {(self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L] != 0).sum()} / {self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].numel()} mask item'
            cond_BD_or_gss = self.shared_ada_lin(cond_BD)
            SABlock.forward
            x = cur_token_map
            for b in self.blocks:
                x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)
            logits_BlV = self.get_logits(x, cond_BD, tau=tau)
            
            t = cfg * ratio
            logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]
            
            idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k, top_p=top_p, num_samples=1)[:, :, 0]
            if gumbel == 0:
                h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
            else:
                # gumbel_softmax_with_rng: refer to mask-git
                gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)
                h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio * gumbel), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)
            
            h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)
            ret.append(h_BChw if returns_vemb else idx_Bl)
            
            if si != self.num_stages_minus_1:   # prepare for next stage
                f_hat, cur_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)
                cur_token_map = cur_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                cur_token_map = self.word_embed(cur_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                cur_token_map = cur_token_map.repeat(2, 1, 1)   # double the batch sizes for the next CFG
        
        for b in self.blocks: b.attn.kv_caching(False)
        return ret
    
    def forward(self, label_B: torch.LongTensor, x_BLCv_wo_first_l: torch.Tensor) -> torch.Tensor:  # returns logits_BLV
        """
        :param label_B: label_B
        :param x_BLCv_wo_first_l: teacher forcing input (B, self.L-self.first_l, self.Cvae)
        :return: logits BLV, V is vocab_size
        """
        bg, ed = self.begin_ends[self.prog_si] if self.prog_si >= 0 else (0, self.L)
        B = x_BLCv_wo_first_l.shape[0]
        with torch.cuda.amp.autocast(enabled=False):
            label_B = torch.where(torch.rand(B, device=label_B.device) < self.cond_drop_rate, self.num_classes, label_B)
            sos = cond_BD = self.class_emb(label_B)
            sos = sos.unsqueeze(1).expand(B, self.first_l, -1) + self.pos_start.expand(B, self.first_l, -1)
            
            if self.prog_si == 0: x_BLC = sos
            else: x_BLC = torch.cat((sos, self.word_embed(x_BLCv_wo_first_l.float())), dim=1)
            x_BLC += self.lvl_embed(self.lvl_1L[:, :ed].expand(B, -1)) + self.pos_1LC[:, :ed] # lvl: BLC;  pos: 1LC
        
        attn_bias = self.attn_bias_for_masking[:, :, :ed, :ed]
        cond_BD_or_gss = self.shared_ada_lin(cond_BD)
        
        # hack: get the dtype if mixed precision is used
        temp = x_BLC.new_ones(8, 8)
        main_type = torch.matmul(temp, temp).dtype
        
        x_BLC = x_BLC.to(dtype=main_type)
        cond_BD_or_gss = cond_BD_or_gss.to(dtype=main_type)
        attn_bias = attn_bias.to(dtype=main_type)
        
        SABlock.forward, AdaLNSABlock.forward
        for i, b in enumerate(self.blocks):
            x_BLC = b(x=x_BLC, cond_BD=cond_BD_or_gss, attn_bias=attn_bias)
        x_BLC = self.get_logits(x_BLC.float(), cond_BD)
        
        if self.prog_si == 0:
            if isinstance(self.word_embed, nn.Linear):
                x_BLC[0, 0, 0] += self.word_embed.weight[0, 0] * 0 + self.word_embed.bias[0] * 0
            else:
                s = 0
                for p in self.word_embed.parameters():
                    if p.requires_grad:
                        s += p.view(-1)[0] * 0
                x_BLC[0, 0, 0] += s
        return x_BLC    # logits BLV, V is vocab_size
    
    def special_init(self, hd0: float): # hd0: head init scale
        if hd0 >= 0:
            if isinstance(self.head, nn.Linear):
                self.head.weight.data.mul_(hd0)
                self.head.bias.data.zero_()
            elif isinstance(self.head, nn.Sequential):
                self.head[-1].weight.data.mul_(hd0)
                self.head[-1].bias.data.zero_()
        
        if isinstance(self.head_nm, AdaLNBeforeHead):
            if True:
                self.head_nm.ada_lin[-1].weight.data.mul_(self.aln_init)
                if hasattr(self.head_nm.ada_lin[-1], 'bias') and self.head_nm.ada_lin[-1].bias is not None:
                    self.head_nm.ada_lin[-1].bias.data.zero_()
        
        depth = len(self.blocks)
        for block_idx, sab in enumerate(self.blocks):
            sab: Union[AdaLNSABlock, SABlock]
            sab.attn.proj.weight.data.div_(math.sqrt(2 * depth))
            sab.ffn.fc2.weight.data.div_(math.sqrt(2 * depth))
            if hasattr(sab.ffn, 'fcg') and sab.ffn.fcg is not None:
                nn.init.ones_(sab.ffn.fcg.bias)
                nn.init.trunc_normal_(sab.ffn.fcg.weight, std=1e-5)
            if hasattr(sab, 'ada_lin'):
                sab.ada_lin[-1].weight.data[:2*self.C].mul_(self.aln_gamma_init)
                sab.ada_lin[-1].weight.data[2*self.C:].mul_(self.aln_init)
                if hasattr(sab.ada_lin[-1], 'bias') and sab.ada_lin[-1].bias is not None:
                    sab.ada_lin[-1].bias.data.zero_()
            elif hasattr(sab, 'ada_gss'):
                sab.ada_gss.data[:, :, :2].mul_(self.aln_gamma_init)
                sab.ada_gss.data[:, :, 2:].mul_(self.aln_init)
    
    def extra_repr(self):
        gamma2_last = self.gamma2_last
        if isinstance(gamma2_last, nn.Parameter):
            gamma2_last = f'<vector {self.layer_scale}>'
        return f'drop_path_rate={self.drop_path_rate:g}, layer_scale={self.layer_scale:g}, gamma2_last={gamma2_last}'


def build_var(
    vae: DiscreteVAE, depth: int,
    patch_nums=(1, 2, 3, 4, 5, 6, 8, 10, 13, 16),   # 10 steps by default
    aln=-1, aln_gamma_init=-1, shared_aln=False, layer_scale=-1,
    tau=4, cos_attn=False,
    flash_if_available=True, fused_if_available=True,
):
    return VAR(
        vae_local=vae, patch_nums=patch_nums,
        depth=depth, embed_dim=depth*64, num_heads=depth, drop_path_rate=0.1 * depth/24,
        aln=aln, aln_gamma_init=aln_gamma_init, shared_aln=shared_aln, layer_scale=layer_scale,
        tau=tau, cos_attn=cos_attn,
        flash_if_available=flash_if_available, fused_if_available=fused_if_available,
    )
# if depth <= 8: layer_scale = 1.
# elif depth <= 12: layer_scale = 1e-1
# elif depth <= 16: layer_scale = 1e-2
# elif depth <= 20: layer_scale = 1e-3
# elif depth <= 34: layer_scale = 1e-5


def var_test():
    V = 4096
    ch = 160
    Cvae = 32
    patch_nums = (1, 2, 3, 4, 5, 6, 8, 10, 13, 16)
    v = DiscreteVAE(vocab_size=V, z_channels=Cvae, ch=ch, test_mode=True, v_patch_nums=patch_nums)
    var = build_var(
        vae=v, depth=12, patch_nums=patch_nums,
        aln=1, aln_gamma_init=1, shared_aln=False, layer_scale=-1,
    )
    
    dd: dict = torch.load('../d12.pth', map_location='cpu')
    states = dd['trainer']
    v.load_state_dict(states['vae_local'])
    var.load_state_dict(states['gpt_wo_ddp'])
    
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    B = 2
    x_BLCv_wo_first_l: torch.FloatTensor = torch.randn(B, var.L-var.first_l, var.Cvae, device=device) * 0.1
    label_B: torch.LongTensor = torch.randint(0, var.num_classes, (B,), device=device)
    var.forward
    logits_BLV = var(label_B=label_B, x_BLCv_wo_first_l=x_BLCv_wo_first_l)
    
    targets_BL = torch.randint(0, var.V, (B, var.L), device=device)
    F.cross_entropy(logits_BLV.view(-1, var.V), targets_BL.view(-1)).backward()
    
    with torch.no_grad():
        var.autoregressive_infer_cfg(B=B, label_B=label_B, cfg=1.5, top_k=var.V//2, top_p=0.9, returns_vemb=True)


if __name__ == '__main__':
    var_test()