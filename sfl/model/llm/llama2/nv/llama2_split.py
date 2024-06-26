import logging
from typing import Optional, Union, Tuple, List

import torch
from transformers import LlamaModel, Cache, DynamicCache, StaticCache
from transformers.modeling_outputs import BaseModelOutputWithPast
from sfl.config import FLConfig
from sfl.model.llm.noise import DxPrivacy
from sfl.model.llm.split_model import SplitModel

logger = logging.getLogger(__name__)


class LLAMA2SplitModel(LlamaModel, SplitModel):
    """
    主模型，主要在FP过程中收集中间输出和梯度
    """

    def __init__(self, config):
        super().__init__(config)
        self.intermediate_fx = {}

    def config_sfl(self, config: FLConfig, *args, **kwargs):
        super(LLAMA2SplitModel, self).config_sfl(config, *args, **kwargs)
        self.perturbers['dxp'] = DxPrivacy(self.embed_tokens, self.config.vocab_size, self.fl_config.noise_scale_dxp)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time, and must specify either one"
            )

        if self.gradient_checkpointing and self.training and use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`."
            )
            use_cache = False

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        past_seen_tokens = 0
        if use_cache:  # kept for BC (cache positions)
            if not isinstance(past_key_values, StaticCache):
                past_key_values = DynamicCache.from_legacy_cache(past_key_values)
                past_seen_tokens = past_key_values.get_seq_length()

        if cache_position is None:
            if isinstance(past_key_values, StaticCache):
                raise ValueError("cache_position is a required argument when using StaticCache.")
            cache_position = torch.arange(
                past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
            )

        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)

        causal_mask = self._update_causal_mask(attention_mask, inputs_embeds, cache_position, past_seen_tokens)

        # embed positions
        """
        SFL: embedding后插入噪声
        """
        inputs_embeds = self.inject_after_embedding(inputs_embeds)
        hidden_states = inputs_embeds

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = None

        for idx,decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            if self.gradient_checkpointing and self.training:
                layer_outputs = self._gradient_checkpointing_func(
                    decoder_layer.__call__,
                    hidden_states,
                    causal_mask,
                    position_ids,
                    past_key_values,
                    output_attentions,
                    use_cache,
                    cache_position,
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    attention_mask=causal_mask,
                    position_ids=position_ids,
                    past_key_value=past_key_values,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    cache_position=cache_position,
                )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache = layer_outputs[2 if output_attentions else 1]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)
            interrupt, hidden_states = self.inject_between_blocks(hidden_states, idx)
            if interrupt is not None:
                return interrupt

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = None
        if use_cache:
            next_cache = (
                next_decoder_cache.to_legacy_cache() if isinstance(next_decoder_cache, Cache) else next_decoder_cache
            )
        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )


    # def forward(
    #         self,
    #         input_ids: torch.LongTensor = None,
    #         attention_mask: Optional[torch.Tensor] = None,
    #         position_ids: Optional[torch.LongTensor] = None,
    #         past_key_values: Optional[List[torch.FloatTensor]] = None,
    #         inputs_embeds: Optional[torch.FloatTensor] = None,
    #         use_cache: Optional[bool] = None,
    #         output_attentions: Optional[bool] = None,
    #         output_hidden_states: Optional[bool] = None,
    #         return_dict: Optional[bool] = None,
    # ) -> Union[Tuple, BaseModelOutputWithPast]:
    #     output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
    #     output_hidden_states = (
    #         output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
    #     )
    #     use_cache = use_cache if use_cache is not None else self.config.use_cache
    #
    #     return_dict = return_dict if return_dict is not None else self.config.use_return_dict
    #
    #     # retrieve input_ids and inputs_embeds
    #     if input_ids is not None and inputs_embeds is not None:
    #         raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")
    #     elif input_ids is not None:
    #         batch_size, seq_length = input_ids.shape
    #     elif inputs_embeds is not None:
    #         batch_size, seq_length, _ = inputs_embeds.shape
    #     else:
    #         raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")
    #
    #     seq_length_with_past = seq_length
    #     past_key_values_length = 0
    #
    #     if past_key_values is not None:
    #         past_key_values_length = past_key_values[0][0].shape[2]
    #         seq_length_with_past = seq_length_with_past + past_key_values_length
    #
    #     if position_ids is None:
    #         device = input_ids.device if input_ids is not None else inputs_embeds.device
    #         position_ids = torch.arange(
    #             past_key_values_length, seq_length + past_key_values_length, dtype=torch.long, device=device
    #         )
    #         position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
    #     else:
    #         position_ids = position_ids.view(-1, seq_length).long()
    #
    #     if inputs_embeds is None:
    #         inputs_embeds = self.embed_tokens(input_ids)
    #     # embed positions
    #     if attention_mask is None:
    #         attention_mask = torch.ones(
    #             (batch_size, seq_length_with_past), dtype=torch.bool, device=inputs_embeds.device
    #         )
    #     attention_mask = self._prepare_decoder_attention_mask(
    #         attention_mask, (batch_size, seq_length), inputs_embeds, past_key_values_length
    #     )
    #
    #     """
    #     SFL: embedding后插入噪声
    #     """
    #     inputs_embeds = self.inject_after_embedding(inputs_embeds)
    #     hidden_states = inputs_embeds
    #
    #     if self.gradient_checkpointing and self.training:
    #         if use_cache:
    #             logger.warning_once(
    #                 "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
    #             )
    #             use_cache = False
    #
    #     # decoder layers
    #     all_hidden_states = () if output_hidden_states else None
    #     all_self_attns = () if output_attentions else None
    #     next_decoder_cache = () if use_cache else None
    #
    #     for idx, decoder_layer in enumerate(self.layers):
    #         if output_hidden_states:
    #             all_hidden_states += (hidden_states,)
    #
    #         past_key_value = past_key_values[idx] if past_key_values is not None else None
    #
    #         if self.gradient_checkpointing and self.training:
    #
    #             def create_custom_forward(module):
    #                 def custom_forward(*inputs):
    #                     # None for past_key_value
    #                     return module(*inputs, output_attentions, None)
    #
    #                 return custom_forward
    #
    #             layer_outputs = torch.utils.checkpoint.checkpoint(
    #                 create_custom_forward(decoder_layer),
    #                 hidden_states,
    #                 attention_mask,
    #                 position_ids,
    #                 None,
    #             )
    #         else:
    #             layer_outputs = decoder_layer(
    #                 hidden_states,
    #                 attention_mask=attention_mask,
    #                 position_ids=position_ids,
    #                 past_key_value=past_key_value,
    #                 output_attentions=output_attentions,
    #                 use_cache=use_cache,
    #             )
    #
    #         hidden_states = layer_outputs[0]
    #
    #         if use_cache:
    #             next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)
    #
    #         if output_attentions:
    #             all_self_attns += (layer_outputs[1],)
    #
    #         interrupt, hidden_states = self.inject_between_blocks(hidden_states, idx)
    #         if interrupt is not None:
    #             return interrupt
    #
    #     hidden_states = self.norm(hidden_states)
    #
    #     # add hidden states from the last decoder layer
    #     if output_hidden_states:
    #         all_hidden_states += (hidden_states,)
    #
    #     next_cache = next_decoder_cache if use_cache else None
    #     if not return_dict:
    #         return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
    #     return BaseModelOutputWithPast(
    #         last_hidden_state=hidden_states,
    #         past_key_values=next_cache,
    #         hidden_states=all_hidden_states,
    #         attentions=all_self_attns,
    #     )
