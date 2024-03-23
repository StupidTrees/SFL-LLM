import random
from typing import Iterator

import torch
import wandb
from tqdm import tqdm
from transformers import AdamW

from sfl.config import FLConfig
from sfl.model.llm.split_model import SplitWrapperModel
from sfl.simulator.dataset import FedDataset
from sfl.simulator.param_keeper import InMemoryParameterKeeper
from sfl.simulator.strategy import FLStrategy
from sfl.utils.data import size_str, tensor_bytes
from sfl.utils.model import get_best_gpu


class SFLSimulator(object):
    """
    SFL实验模拟
    """

    def __init__(self, client_ids, strategy: FLStrategy, llm: SplitWrapperModel, tokenizer, dataset: FedDataset,
                 config: FLConfig, additional_param_keys=None, b2tr_hooks=None, args=None):
        self.args = args
        self.client_ids = client_ids
        self.strategy: FLStrategy = strategy
        self.strategy.simulator = self
        task_type = llm.task_type
        if args.task_type is not None:
            task_type = args.task_type
        self.strategy.task_type = task_type
        self.llm: SplitWrapperModel = llm
        self.tokenizer = tokenizer
        self.dataset = dataset
        self.config = config
        self.device = get_best_gpu() if torch.cuda.is_available() else 'cpu'
        self.parameter_keeper = InMemoryParameterKeeper(client_ids)
        self.llm.config_sfl(config, self.parameter_keeper, b2tr_hooks)
        self.communication_overhead_uplink = {}
        self.communication_overhead_downlink = {}
        # Step pointer
        self.current_global_round = 0
        self.local_epochs = {cid: 0 for cid in self.client_ids}
        self.local_steps = {cid: 0 for cid in self.client_ids}
        self.global_steps = {cid: 0 for cid in self.client_ids}

        if not self.llm.adapter_added:
            self.llm = self.llm.convert_to_lora_model()
            self.strategy.llm = llm
            # store pretrained parameters
        if additional_param_keys is None:
            additional_param_keys = ['mirror']
        for key in ['pretrained'] + additional_param_keys:
            self.parameter_keeper.store_other_params(key, 'trunk',
                                                     [p.detach().cpu() for nm, p in self.llm.get_trunk_params()])
            self.parameter_keeper.store_other_params(key, 'top',
                                                     [p.detach().cpu() for nm, p in self.llm.get_top_params()])
            self.parameter_keeper.store_other_params(key, 'bottom',
                                                     [p.detach().cpu() for nm, p in self.llm.get_bottom_params()])

        # special initialization
        # if config.top_and_bottom_from_scratch in ['True', 'Embedding']:
        #     self.llm.reset_params(self.llm.get_top_params(), config.top_and_bottom_from_scratch)
        #     self.llm.reset_params(self.llm.get_bottom_params(), config.top_and_bottom_from_scratch)
        if config.top_and_bottom_from_scratch == 'Noised':
            for nm, param in self.llm.get_top_params():
                scale = param.data.max() - param.data.min()
                param.data += torch.randn_like(param.data) * scale * 0.02
            for nm, param in self.llm.get_bottom_params():
                scale = param.data.max() - param.data.min()
                param.data += torch.randn_like(param.data) * scale * 0.02

    def pre_ft(self, data_loader, parts=None):
        if not hasattr(self.llm.config, 'quantization_config'):
            self.llm.to(self.device)
        self.llm.train()
        if parts is None:
            parts = ['top', 'bottom']
        # 不收集中间结果
        bk_ci = self.llm.fl_config.collect_intermediates
        self.llm.fl_config.collect_intermediates = False
        # 冻结Trunk
        frozen_params = []
        frozen_states = []
        for part in {'top', 'bottom', 'trunk'} - set(parts):
            if part == 'top':
                ls = self.llm.get_top_params()
            elif part == ' trunk':
                ls = self.llm.get_trunk_params()
            else:
                ls = self.llm.get_bottom_params()
            for nm, p in ls:
                frozen_states.append(p.requires_grad)
                p.requires_grad = False
                frozen_params.append(p)
        # 微调bottom和top
        tune = [p for p in self.llm.parameters() if p.requires_grad]
        optimizer = AdamW(tune, lr=1e-5)
        with tqdm(total=len(data_loader)) as pbar:
            for step, batch in enumerate(data_loader):
                optimizer.zero_grad()
                input_ids = batch['input_ids'].to(self.llm.device)
                attention_mask = batch['input_att_mask'].to(self.llm.device)
                outputs = self.llm(input_ids=input_ids, labels=input_ids, attention_mask=attention_mask)
                loss = outputs.loss
                pbar.set_description(f'Pre-FT Loss {loss.item():.3f}')
                loss.backward()
                optimizer.step()
                pbar.update(1)
        # 恢复
        for p, r in zip(frozen_params, frozen_states):
            p.requires_grad = r
        self.llm.fl_config.collect_intermediates = bk_ci

    def simulate(self):
        if not hasattr(self.llm.config, 'quantization_config'):
            self.llm.to(self.device)
        self.llm.train()
        # initialize server parameters
        self.parameter_keeper.store_server_params('server',
                                                  [p.detach().cpu() for nm, p in self.llm.get_trunk_params()])
        # initialize client parameters
        cm = ([p.detach().cpu() for nm, p in self.llm.get_top_params()],
              [p.detach().cpu() for nm, p in self.llm.get_bottom_params()])
        self.parameter_keeper.store_client_params(None, cm)
        loaders = {}
        iters = {}
        self.local_epochs = {cid: 0 for cid in self.client_ids}
        self.local_steps = {cid: 0 for cid in self.client_ids}
        self.global_steps = {cid: 0 for cid in self.client_ids}

        for i in range(self.config.global_round):
            self.current_global_round = i
            print(f'==================================Global Round {i}=================================')
            # sample clients
            sampled_clients = random.sample(self.client_ids, int(len(self.client_ids) * self.config.client_per_round))
            # sequentially train each client
            participation = set(sampled_clients)
            completed = set()
            while len(completed) < len(participation):
                for client_id in participation:
                    if client_id in completed:
                        continue
                    loaders.setdefault(client_id,
                                       self.dataset.get_dataloader(client_id, batch_size=self.config.batch_size,
                                                                   type=self.config.dataset_type))
                    iters.setdefault(client_id, [iter(loaders[client_id])])
                    itt = CircularDataLoaderIterator(iters[client_id], loaders[client_id], self.config.client_steps)
                    self._client_step(client_id, i, self.local_epochs[client_id], itt)
                    self.local_steps[client_id] += itt.iterated_num
                    self.global_steps[client_id] += itt.iterated_num
                    if itt.reached_end:
                        self.local_epochs[client_id] += 1
                        self.local_steps[client_id] = 0
                        if self.local_epochs[client_id] >= self.config.client_epoch:
                            completed.add(client_id)
                    print(self.global_steps[client_id], self.config.max_global_step)
                    if (self.global_steps[client_id] + 1) >= self.config.max_global_step > 0:
                        completed.add(client_id)

                    self.__summarize_communication(i, client_id)
                # aggregate server parameters
                self._server_step(i, sampled_clients)
            self.__summarize_communication(i, client_id=None)
            self._server_step(i, sampled_clients)
        self.__summarize_communication()

    def restored_forward(self, part: str = 'top', **kwargs):
        outputs = None
        if part == 'top':
            backup_params = [p.detach().cpu() for nm, p in self.llm.get_top_params()]
            self.llm.load_top_params(self.parameter_keeper.get_other_params('pretrained', 'top'))
            outputs = self.llm(**kwargs)
            self.llm.load_top_params(backup_params)
        elif part == 'bottom':
            backup_params = [p.detach().cpu() for nm, p in self.llm.get_bottom_params()]
            self.llm.load_bottom_params(self.parameter_keeper.get_other_params('pretrained', 'bottom'))
            outputs = self.llm(**kwargs)
            self.llm.load_bottom_params(backup_params)
        elif part == 'trunk':
            backup_params = [p.detach().cpu() for nm, p in self.llm.get_trunk_params()]
            self.llm.load_trunk_params(self.parameter_keeper.get_other_params('pretrained', 'trunk'))
            outputs = self.llm(**kwargs)
            self.llm.load_trunk_params(backup_params)
        return outputs

    def restored_run(self, func, parts=None, key: str = '', write_back=True, **kwargs):
        if key not in self.parameter_keeper.other_params:
            for part in self.parameter_keeper.other_params['pretrained']:
                self.parameter_keeper.store_other_params(key, part,
                                                         self.parameter_keeper.get_other_params('pretrained', part))
        if parts is None:
            parts = ['top', 'bottom']
        backup_params = {}
        for part in parts:
            if part == 'top':
                backup_params[part] = [p.detach().cpu() for nm, p in self.llm.get_top_params()]
                self.llm.load_top_params(self.parameter_keeper.get_other_params(key, part))
            elif part == 'bottom':
                backup_params[part] = [p.detach().cpu() for nm, p in self.llm.get_bottom_params()]
                self.llm.load_bottom_params(self.parameter_keeper.get_other_params(key, part))
            elif part == 'trunk':
                backup_params[part] = [p.detach().cpu() for nm, p in self.llm.get_trunk_params()]
                self.llm.load_trunk_params(self.parameter_keeper.get_other_params(key, part))
        func(**kwargs)
        for part in parts:
            updated_params = []
            if part == 'top':
                updated_params = [p.detach().cpu() for nm, p in self.llm.get_top_params()]
                self.llm.load_top_params(backup_params[part])
            elif part == 'bottom':
                updated_params = [p.detach().cpu() for nm, p in self.llm.get_bottom_params()]
                self.llm.load_bottom_params(backup_params[part])
            elif part == 'trunk':
                updated_params = [p.detach().cpu() for nm, p in self.llm.get_trunk_params()]
                self.llm.load_trunk_params(backup_params[part])
            if write_back:
                self.parameter_keeper.store_other_params(key, part, updated_params)

    def get_current_step(self, client_id, mini_step):
        global_step = self.global_steps[client_id] + mini_step
        local_step = self.local_steps[client_id] + mini_step
        return local_step, global_step

    def _client_step(self, client_id: str, global_round, local_epoch, iterator: Iterator):
        # load client parameters (bottom and top layers)
        cm = self.parameter_keeper.get_client_params(client_id)
        self.llm.load_top_params(cm[0])
        self.llm.load_bottom_params(cm[1])
        # load server parameters (trunk layers or adapters)
        self.llm.load_trunk_params(self.parameter_keeper.get_server_params('server'))
        # client step
        torch.cuda.empty_cache()
        self.strategy.client_step(client_id, global_round, local_epoch, self.llm, iterator, self.config)
        # store updated client parameters
        cm = ([p.cpu() for nm, p in self.llm.get_top_params()],
              [p.cpu() for nm, p in self.llm.get_bottom_params()])
        self.parameter_keeper.store_client_params(client_id, cm)
        # store updated server parameters
        self.parameter_keeper.store_server_params(client_id,
                                                  [p.detach().cpu() for nm, p in self.llm.get_trunk_params()])

    def _server_step(self, global_round, sample_clients):
        # aggregate server parameters
        params = {}
        for client_id in sample_clients:
            params[client_id] = self.parameter_keeper.get_server_params(client_id)
        print("SERVER:", "AGGREGATION")
        self.parameter_keeper.store_server_params('server', self.strategy.aggregation_step(global_round, params))

    def _client_one_step_done(self, client_id, mini_step, batch, logs=None):
        """
        统计前传、反传的中间数据量
        """
        local_step, global_step = self.get_current_step(client_id, mini_step)
        local_epoch = self.local_epochs[client_id]

        b2tr_inter, tr2t_inter, all_inters = self.llm.get_all_inter()
        self.communication_overhead_uplink.setdefault(self.current_global_round, {})
        self.communication_overhead_uplink[self.current_global_round].setdefault(client_id, {})
        self.communication_overhead_uplink[self.current_global_round][client_id].setdefault(local_epoch, 0)

        self.communication_overhead_downlink.setdefault(self.current_global_round, {})
        self.communication_overhead_downlink[self.current_global_round].setdefault(client_id, {})
        self.communication_overhead_downlink[self.current_global_round][client_id].setdefault(local_epoch, 0)
        if self.config.collect_intermediates:
            self.communication_overhead_downlink[self.current_global_round][client_id][local_epoch] += tensor_bytes(
                b2tr_inter.grad) + tensor_bytes(tr2t_inter.fx)
            self.communication_overhead_uplink[self.current_global_round][client_id][local_epoch] += tensor_bytes(
                tr2t_inter.grad) + tensor_bytes(b2tr_inter.fx)

        # 进行log

        if logs is None:
            logs = {}
        report = {'global_step': global_step, f'client{client_id}_global_round': self.current_global_round,
                  f'client{client_id}_local_epoch': local_epoch,
                  f'client{client_id}_local_step': local_step}
        self.strategy.callback_intermediate_result(self.current_global_round,
                                                   client_id,
                                                   local_epoch,
                                                   local_step,
                                                   b2tr_inter, tr2t_inter, all_inters,
                                                   batch, logs)
        if (local_step + 1) % self.config.client_evaluate_freq == 0:
            self.strategy.client_evaluate(self.current_global_round, client_id, logs)
        for k, v in logs.items():
            report[f'client{client_id}_{k}'] = v
        if self.args and not self.args.log_to_wandb:
            return
        wandb.log(report)

    def __summarize_communication(self, global_round=None, client_id=None):
        if global_round is None:
            total_uplink = 0
            total_downlink = 0
            for gr in self.communication_overhead_uplink:
                for cid in self.communication_overhead_uplink[gr]:
                    total_uplink += sum(self.communication_overhead_uplink[gr][cid].values())
                    total_downlink += sum(self.communication_overhead_downlink[gr][cid].values())
            print(f'FL communication overhead: uplink={size_str(total_uplink)}, downlink={size_str(total_downlink)}')

        elif client_id is None:
            total_uplink = 0
            total_downlink = 0
            for cid in self.communication_overhead_uplink[global_round]:
                total_uplink += sum(self.communication_overhead_uplink[global_round][cid].values())
                total_downlink += sum(self.communication_overhead_downlink[global_round][cid].values())
            print(
                f'Global Round {global_round} communication overhead: uplink={size_str(total_uplink)}, downlink={size_str(total_downlink)}')
        else:
            print(
                f'Client {client_id} communication overhead: uplink:{size_str(sum(self.communication_overhead_uplink[global_round][client_id].values()))},'
                f' downlink:{size_str(sum(self.communication_overhead_downlink[global_round][client_id].values()))}')


class CircularDataLoaderIterator:
    def __init__(self, iters, data_loader, max_step):
        self.iters = iters
        self.data_loader = data_loader
        self.count = 0
        self.max_step = max_step
        self.reached_end = False
        self.iterated_num = 0

    def __iter__(self):
        return self

    def __next__(self):
        try:
            batch_data = next(self.iters[0])
            self.count += 1
            self.iterated_num = self.count
            if self.count > self.max_step:
                self.iterated_num = self.count - 1
                raise StopIteration
            return batch_data
        except StopIteration:
            if self.count <= self.max_step:
                self.iterated_num = self.count
                self.reached_end = True
                self.iters[0] = iter(self.data_loader)
                batch_data = next(self.iters[0])
                self.count += 1
                return batch_data
            else:
                raise StopIteration