from copy import deepcopy

import torch
from torch import nn
from torch.nn import CrossEntropyLoss
from torch.nn import ModuleList
from transformers.models.gpt2.modeling_gpt2 import GPT2Block
from transformers.models.t5.modeling_t5 import T5Block, T5LayerNorm

from sfl.config import FLConfig
from sfl.model.llm.gpt2.gpt2_wrapper import GPT2SplitLMHeadModel
from sfl.model.llm.split_model import SplitWrapperModel
from sfl.model.llm.t5.t5wrapper import T5ForConditionalGenerationSplitModel
from sfl.utils.model import calc_shifted_loss_logits


class DLGAttacker(nn.Module):
    """
    DLG攻击模型
    """
    def __init__(self, fl_config: FLConfig, model: SplitWrapperModel, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_model_config = model.config
        self.fl_config = fl_config
        self.model_type = model.type

    def fit(self, inter, gradient, epochs=300, adjust=0, beta=0.5, lr=0.09, gt_init=None, gt_reg=0.0, temp_range=0.0,
            further_ft=10, **kwargs):
        batch_size, seq_len = inter.shape[:2]
        vocab_size = self.target_model_config.vocab_size
        dra_attacked = None
        if gt_init is not None:
            if gt_init.shape[1] != gradient.shape[1]:
                gt_init = gt_init[:, -gradient.shape[1]:, :]
            dra_attacked = gt_init.clone().to(inter.device)  # (batch_size, seq_len, vocab_size)
            # unc_vec = torch.softmax(torch.norm(gradient, p=1, dim=-1), dim=-1)  # (batch_size, seq_len)
            # unc_vec = 1 - temp_range + temp_range * 2 * (unc_vec - unc_vec.min()) / (unc_vec.max() - unc_vec.min())
            # get the tok-k indices of the vector
            # dra_attacked = dra_attacked / unc_vec.unsqueeze(-1)
            # temperature softmax
            dra_attacked = torch.softmax(dra_attacked, dim=-1)
            gt = dra_attacked.clone()
        else:
            gt = torch.softmax(torch.randn((batch_size, seq_len, vocab_size)).to(inter.device), dim=-1)
        gt.requires_grad = True
        inter.requires_grad = True
        optimizer = torch.optim.AdamW([gt], lr=lr, betas=(0.9, 0.999), eps=1e-6, weight_decay=0.01)
        for i in range(epochs):
            optimizer.zero_grad()
            x = self.forward(inter, **kwargs)
            if self.model_type == 'encoder-decoder':
                loss = CrossEntropyLoss(ignore_index=-100)(x.view(-1, x.size(-1)),
                                                           torch.softmax(gt, dim=-1).view(-1, gt.size(-1)))
            else:
                loss = calc_shifted_loss_logits(x, torch.softmax(gt, dim=-1))
            grad = torch.autograd.grad(loss, inter, create_graph=True)
            grad_diff = 0
            for gx, gy in zip(grad, gradient.to(loss.device)):
                grad_diff += beta * ((gx - gy) ** 2).sum() + (1 - beta) * (torch.abs(gx - gy)).sum()
            if dra_attacked is not None and gt_reg > 0:
                grad_diff += gt_reg * (torch.softmax(gt - dra_attacked, dim=-1) ** 2).sum()
            grad_diff.backward()
            # gt.grad *= unc_vec.unsqueeze(-1)
            optimizer.step()

        # further adjust
        if further_ft > 0:
            gt2 = gt.detach().clone().to(gt.device)
            gt2.requires_grad = True
            unc_vec = torch.softmax(torch.norm(gradient, p=1, dim=-1), dim=-1)  # (batch_size, seq_len)
            # k = 20% of the seq_len
            k = int(0.2 * seq_len)
            largest_indexes = torch.topk(unc_vec, k, dim=-1)[1]  # (batch_size, k)
            mask = torch.ones(gt2.shape[:2]).to(gt2.device)
            mask = mask.scatter_(1, largest_indexes, 0).unsqueeze(-1).expand(-1, -1, vocab_size)
            # gt2 = gt2.masked_fill(mask.bool(), 0)
            optimizer2 = torch.optim.AdamW([gt2], lr=lr, betas=(0.9, 0.999), eps=1e-6, weight_decay=0.01)
            for i in range(further_ft):
                optimizer2.zero_grad()
                x = self.forward(inter)
                loss = calc_shifted_loss_logits(x, torch.softmax(gt2, dim=-1))
                grad = torch.autograd.grad(loss, inter, create_graph=True)
                grad_diff = 0
                for gx, gy in zip(grad, gradient.to(loss.device)):
                    grad_diff += beta * ((gx - gy) ** 2).sum() + (1 - beta) * (torch.abs(gx - gy)).sum()
                grad_diff.backward()
                gt2.grad = gt2.grad.masked_fill(mask.bool(), 0)
                optimizer2.step()
            nmask = 1 - mask
            gt = gt.masked_scatter(nmask.bool(), gt2.masked_select(nmask.bool()))

        if adjust > 0:
            optimizer2 = torch.optim.AdamW(self.parameters(), lr=1e-5)
            for i in range(adjust):
                optimizer2.zero_grad()
                x = self.forward(inter, **kwargs)
                loss = calc_shifted_loss_logits(x, torch.softmax(gt, dim=-1))
                loss.backward()
                optimizer2.step()
        return gt


class GPT2TopDLGAttacker(DLGAttacker):

    def __init__(self, fl_config: FLConfig, model: GPT2SplitLMHeadModel):
        super().__init__(fl_config, model)
        num_blocks = model.config.n_layer - fl_config.split_point_2
        model.config_sfl(fl_config, None)
        self.decoder = ModuleList([GPT2Block(model.config, i) for i in range(num_blocks)])
        self.ln = nn.LayerNorm(model.transformer.embed_dim, eps=model.config.layer_norm_epsilon)
        # copy the parameters
        for (nm1, param1), (nm2, param2) in zip(model.get_top_params(), self.named_parameters()):
            param2.data = param1.data.clone()
        self.head = deepcopy(model.lm_head)

    def forward(self, x, **kwargs):
        for block in self.decoder:
            x = block(x)[0]
        x = self.ln(x)
        return self.head(x)


class T5DecoderDLGAttacker(DLGAttacker):

    def __init__(self, fl_config: FLConfig, model: T5ForConditionalGenerationSplitModel):
        super().__init__(fl_config, model)
        num_blocks = model.config.num_decoder_layers - fl_config.split_point_2
        self.blocks = nn.ModuleList(
            [T5Block(model.decoder.config, has_relative_attention_bias=bool(i == 0 and fl_config.split_point_2 == 0))
             for i in range(num_blocks)]
        )
        self.final_layer_norm = T5LayerNorm(model.decoder.config.d_model, eps=model.decoder.config.layer_norm_epsilon)
        # copy the parameters
        for (nm1, param1), (nm2, param2) in zip(model.get_top_params(), self.named_parameters()):
            param2.data = param1.data.clone()
        self.lm_head = deepcopy(model.lm_head)

    def forward(self, decoder_hidden_states, encoder_inter):
        for block in self.blocks:
            x = block(decoder_hidden_states, encoder_hidden_states=encoder_inter)[0]
        x = self.final_layer_norm(x)
        return self.lm_head(x)