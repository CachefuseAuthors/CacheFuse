# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/v4.28.0/src/transformers/models/llama/modeling_llama.py
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Inference-only LLaMA model compatible with HuggingFace weights."""
from typing import Any, Dict, List, Optional, Tuple

import torch
from torch import nn
import torch.distributed
from transformers import LlamaConfig

from vllm.config import LoRAConfig
from vllm.model_executor.input_metadata import InputMetadata
from vllm.model_executor.layers.activation import SiluAndMul
from vllm.model_executor.layers.attention import Attention
from vllm.model_executor.layers.layernorm import RMSNorm
from vllm.model_executor.layers.linear import (LinearMethodBase,
                                               MergedColumnParallelLinear,
                                               QKVParallelLinear,
                                               RowParallelLinear)
from vllm.model_executor.layers.rotary_embedding import get_rope
from vllm.model_executor.layers.sampler import Sampler
from vllm.model_executor.layers.vocab_parallel_embedding import (
    VocabParallelEmbedding, ParallelLMHead, DEFAULT_VOCAB_PADDING_SIZE)
from vllm.model_executor.parallel_utils.parallel_state import (
    get_tensor_model_parallel_world_size)
from vllm.model_executor.sampling_metadata import SamplingMetadata
from vllm.model_executor.weight_utils import (default_weight_loader,
                                              hf_model_weights_iterator)
from vllm.sequence import SamplerOutput

from torch.profiler import profile, record_function, ProfilerActivity
import time
import threading
import numpy as np
from multiprocessing import Process
import queue

KVCache = Tuple[torch.Tensor, torch.Tensor]


class LlamaMLP(nn.Module):

    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
        hidden_act: str,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.gate_up_proj = MergedColumnParallelLinear(
            hidden_size, [intermediate_size] * 2,
            bias=False,
            linear_method=linear_method)
        self.down_proj = RowParallelLinear(intermediate_size,
                                           hidden_size,
                                           bias=False,
                                           linear_method=linear_method)
        if hidden_act != "silu":
            raise ValueError(f"Unsupported activation: {hidden_act}. "
                             "Only silu is supported for now.")
        self.act_fn = SiluAndMul()

    def forward(self, x):
        gate_up, _ = self.gate_up_proj(x)
        x = self.act_fn(gate_up)
        x, _ = self.down_proj(x)
        return x


class LlamaAttention(nn.Module):

    def __init__(
        self,
        hidden_size: int,
        num_heads: int,
        num_kv_heads: int,
        rope_theta: float = 10000,
        rope_scaling: Optional[Dict[str, Any]] = None,
        max_position_embeddings: int = 8192,
        linear_method: Optional[LinearMethodBase] = None,
        bias: bool = False,
        sliding_window: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.hidden_size = hidden_size
        tp_size = get_tensor_model_parallel_world_size()
        self.total_num_heads = num_heads
        assert self.total_num_heads % tp_size == 0
        self.num_heads = self.total_num_heads // tp_size
        self.total_num_kv_heads = num_kv_heads
        if self.total_num_kv_heads >= tp_size:
            # Number of KV heads is greater than TP size, so we partition
            # the KV heads across multiple tensor parallel GPUs.
            assert self.total_num_kv_heads % tp_size == 0
        else:
            # Number of KV heads is less than TP size, so we replicate
            # the KV heads across multiple tensor parallel GPUs.
            assert tp_size % self.total_num_kv_heads == 0
        self.num_kv_heads = max(1, self.total_num_kv_heads // tp_size)
        self.head_dim = hidden_size // self.total_num_heads
        self.q_size = self.num_heads * self.head_dim
        self.kv_size = self.num_kv_heads * self.head_dim
        self.scaling = self.head_dim**-0.5
        self.rope_theta = rope_theta
        self.max_position_embeddings = max_position_embeddings

        self.qkv_proj = QKVParallelLinear(
            hidden_size,
            self.head_dim,
            self.total_num_heads,
            self.total_num_kv_heads,
            bias=bias,
            linear_method=linear_method,
        )
        self.o_proj = RowParallelLinear(
            self.total_num_heads * self.head_dim,
            hidden_size,
            bias=bias,
            linear_method=linear_method,
        )

        self.rotary_emb = get_rope(
            self.head_dim,
            rotary_dim=self.head_dim,
            max_position=max_position_embeddings,
            base=rope_theta,
            rope_scaling=rope_scaling,
        )
        self.attn = Attention(self.num_heads,
                              self.head_dim,
                              self.scaling,
                              num_kv_heads=self.num_kv_heads,
                              sliding_window=sliding_window)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        status: int,
        cache_fuse_metadata: dict,
        cache_load_metadata: dict
    ) -> torch.Tensor:
        qkv, _ = self.qkv_proj(hidden_states)
        
        q, k, v = qkv.split([self.q_size, self.kv_size, self.kv_size], dim=-1)
        q, k = self.rotary_emb(positions, q, k)
        k_cache, v_cache = kv_cache
        attn_output = self.attn(q, k, v, k_cache, v_cache, input_metadata, 
                                status, cache_fuse_metadata, cache_load_metadata)
        output, _ = self.o_proj(attn_output)
        return output


class LlamaDecoderLayer(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.hidden_size = config.hidden_size
        rope_theta = getattr(config, "rope_theta", 10000)
        rope_scaling = getattr(config, "rope_scaling", None)
        max_position_embeddings = getattr(config, "max_position_embeddings",
                                          8192)
        sliding_window = getattr(config, "sliding_window", None)
        self.self_attn = LlamaAttention(
            hidden_size=self.hidden_size,
            num_heads=config.num_attention_heads,
            num_kv_heads=getattr(config, "num_key_value_heads",
                                 config.num_attention_heads),
            rope_theta=rope_theta,
            rope_scaling=rope_scaling,
            max_position_embeddings=max_position_embeddings,
            linear_method=linear_method,
            bias=getattr(config, "bias", False),
            sliding_window=sliding_window,
        )
        self.mlp = LlamaMLP(
            hidden_size=self.hidden_size,
            intermediate_size=config.intermediate_size,
            hidden_act=config.hidden_act,
            linear_method=linear_method,
        )
        self.input_layernorm = RMSNorm(config.hidden_size,
                                       eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size,
                                                eps=config.rms_norm_eps)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        residual: Optional[torch.Tensor],
        status: int,
        cache_fuse_metadata: dict,
        cache_load_metadata: dict
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # Self Attention
        
        #if hidden_states.shape[1]>200:
        #    torch.cuda.synchronize()
        #    start = torch.cuda.Event(enable_timing=True)
        #    end = torch.cuda.Event(enable_timing=True)
        #    start.record()
        #if hidden_states.shape[1]>20:
        #    torch.cuda.synchronize()
        #    start = torch.cuda.Event(enable_timing=True)
        #    end = torch.cuda.Event(enable_timing=True)
        #    start.record()
        if residual is None:
            residual = hidden_states
            hidden_states = self.input_layernorm(hidden_states)
        else:
            hidden_states, residual = self.input_layernorm(
                hidden_states, residual)
        hidden_states = self.self_attn(
            positions=positions,
            hidden_states=hidden_states,
            kv_cache=kv_cache,
            input_metadata=input_metadata,
            status=status,
            cache_fuse_metadata=cache_fuse_metadata,
            cache_load_metadata = cache_load_metadata
        )
        
        if status==1:
            residual = residual[:, cache_fuse_metadata["imp_token_indices"]]
        #if hidden_states.shape[1]>20:
        #    end.record()
        #    torch.cuda.synchronize()
        #    temp_time = start.elapsed_time(end)
        #    print(f"Attention time:{temp_time}")
            
        #if hidden_states.shape[1]>20:
        #    torch.cuda.synchronize()
        #    start = torch.cuda.Event(enable_timing=True)
        #    end = torch.cuda.Event(enable_timing=True)
        #    start.record()
        # Fully Connected
        hidden_states, residual = self.post_attention_layernorm(
            hidden_states, residual)
        hidden_states = self.mlp(hidden_states)
        #if hidden_states.shape[1]>20:
        #    end.record()
        #    torch.cuda.synchronize()
        #    temp_time = start.elapsed_time(end)
        #    print(f"MLP time:{temp_time}")
        return hidden_states, residual

@torch.inference_mode
def load_worker(queue):
    #load_stream = torch.cuda.Stream()
    
    
    while True:
        item = queue.get()
        if item is None:
            queue.task_done()
            return
        cpu_k, cpu_v, gpu_k, gpu_v, load_stream,event = item
        with torch.cuda.stream(load_stream):
            disk_k = torch.load("/local/jiayi3/temp_k_test_0.pt")
            #disk_v = torch.load("/local/jiayi3/temp_v.pt")
            cpu_k.copy_(disk_k)
            #cpu_v.copy_(disk_v, non_blocking=True)
            gpu_k.copy_(cpu_k, non_blocking=True)
            #gpu_v.copy_(cpu_v, non_blocking=True)
        event.set()
        
        
        queue.task_done()
    


class LlamaModel(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        linear_method: Optional[LinearMethodBase] = None,
        lora_config: Optional[LoRAConfig] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.padding_idx = config.pad_token_id
        lora_vocab = (lora_config.lora_extra_vocab_size *
                      (lora_config.max_loras or 1)) if lora_config else 0
        self.vocab_size = config.vocab_size + lora_vocab
        self.org_vocab_size = config.vocab_size
        self.embed_tokens = VocabParallelEmbedding(
            self.vocab_size,
            config.hidden_size,
            org_num_embeddings=config.vocab_size,
        )
        self.layers = nn.ModuleList([
            LlamaDecoderLayer(config, linear_method)
            for _ in range(config.num_hidden_layers)
        ])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        
        # FIXME(Jiayi): needs to be dynamic
        # FIXME(Jiayi): currently only support `batch_size=1`
        # batching for prefill (e.g., (prompt_with_reuse, prompt_with_no_reuse)) will
        # dillute our improvement sometimes 
        self.cache_fuse_metadata = {"check_layers":[1],
                                    "check": True,
                                    "recomp_ratios":[0.77],
                                    "recomp_ratio":0.77,
                                    "load_indices":[],
                                    "recomp_indices":[],
                                    "original_slot_mapping":None,
                                    "our_slot_mapping_for_check":None,
                                    "our_slot_mapping":None,
                                    "kv_cache_dtype": None,
                                    "attn_bias": None,
                                    "imp_token_indices": None,
                                    "org_seq_len": None,
                                    "pre_mask":None}
                                    #"batch_indices":[0]}
        #self.cache_load_metadata = { "loader" :  None,
        #                            "hash": "kv_temp"}
        self.loader = None
        
        #self.load_stream_test = torch.cuda.Stream()
        self.load_streams = [torch.cuda.Stream() for i in range(len(self.layers)-1)]
        self.load_events = [threading.Event() for i in range(len(self.layers)-1)]
        num_thread = 1
        self.load_queue = queue.Queue()
        self.load_threads = [
            threading.Thread(
                target=load_worker, args=(self.load_queue,)
            ) for i in range(num_thread)
        ]
        for t in self.load_threads:
            t.start()
        test_shape = (4002,8,128)
        self.key_buf_pin = torch.rand(test_shape, dtype=torch.float16).cpu().pin_memory()
        self.value_buf_pin = torch.rand(test_shape, dtype=torch.float16).cpu().pin_memory()
        self.key_buf = torch.rand(test_shape, dtype=torch.float16).cuda()
        self.value_buf = torch.rand(test_shape, dtype=torch.float16).cuda()
        
    def submit_load(self, *args):
        self.load_queue.put_nowait(args)
    
    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_fuse_metadata=None,
        sampling_metadata=None
    ) -> torch.Tensor:
        #Jiayi: input_ids shape: [bsz, input_len]
        #print(f"\033[33mHere, the input ids shape is {input_ids.shape}\033[0m")
        hidden_states = self.embed_tokens(input_ids)
        #Jiayi: hidden_states shape: [bsz, input_len, hidden_dim]
        #print(f"\033[32mHere, the hidden states shape is {hidden_states.shape}\033[0m")
        #if kv_caches[0][0] is not None:
        #    print(f"\033[32mHere, the KV cache shape {kv_caches[0][0].shape}\033[0m")
        #else:
        #    print("\033[31mThis time we don't have any KV caches\033[0m")
        residual = None
        
        #FIXME(Jiayi): This is a hack to make it run
        #if cache_fuse_metadata==None:
        #    cache_fuse_metadata=self.cache_fuse_metadata
        
        flag=None
        
        #HACK(Jiayi): This is a hack to measure accurate time
        if input_ids.shape[1]>1000 and input_ids.shape[0]==1 and input_metadata.is_prompt:
                      
            flag=True
            self.cache_fuse_metadata = {"check_layers":[1],
                                    "check": True,
                                    "recomp_ratios":[0.17],
                                    "recomp_ratio":0.17,
                                    "load_indices":[],
                                    "recomp_indices":[],
                                    "original_slot_mapping":None,
                                    "our_slot_mapping_for_check":None,
                                    "our_slot_mapping":None,
                                    "kv_cache_dtype": None,
                                    "attn_bias": None,
                                    "imp_token_indices": None,
                                    "org_seq_len": None,
                                    "pre_mask":None,
                                    "key_value_shape":(hidden_states.shape[1], 8, 128), #Note this is for mis7b; 70B?
                                    "stream_activate":True}

            input_metadata.attn_bias = None #Jiayi: delete attn_bias from last inference and 
            torch.cuda.synchronize()
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            start.record()
        else:
            self.cache_fuse_metadata = {"check_layers":[1],
                                    "check": False,
                                    "recomp_ratios":[0.15],
                                    "recomp_ratio":0.15,
                                    "load_indices":[],
                                    "recomp_indices":[],
                                    "original_slot_mapping":None,
                                    "our_slot_mapping_for_check":None,
                                    "our_slot_mapping":None,
                                    "kv_cache_dtype": None,
                                    "attn_bias": None,
                                    "imp_token_indices": None,
                                    "org_seq_len": None,
                                    "pre_mask":None,
                                    "key_value_shape":None,
                                    "stream_activate":False}
            
        
        #import pdb
        #pdb.set_trace()
        
        if input_metadata.is_prompt:
            #import pdb
            #pdb.set_trace()
            temp_status = 0 # full recomp
            self.cache_fuse_metadata["org_seq_len"] = input_ids.shape[1]
            
            #FIXME(Jiayi): fix this clone for faster time
            self.cache_fuse_metadata["our_slot_mapping"] = input_metadata.slot_mapping.clone()
        else:
            temp_status = -1 # decode
        '''
        if flag and self.cache_fuse_metadata["stream_activate"]:
            key_fake = torch.rand(self.cache_fuse_metadata["key_value_shape"], dtype=hidden_states.dtype).cpu()
            value_fake = torch.rand(self.cache_fuse_metadata["key_value_shape"], dtype=hidden_states.dtype).cpu()
            key_buf_pin = torch.rand(self.cache_fuse_metadata["key_value_shape"], dtype=hidden_states.dtype).pin_memory()
            value_buf_pin = torch.rand(self.cache_fuse_metadata["key_value_shape"], dtype=hidden_states.dtype).pin_memory()
            key_buf = torch.rand(self.cache_fuse_metadata["key_value_shape"], dtype=hidden_states.dtype).to(hidden_states.device)
            value_buf = torch.rand(self.cache_fuse_metadata["key_value_shape"], dtype=hidden_states.dtype).to(hidden_states.device)
            path_k = "/local/jiayi3/temp_k.pt"
            path_v = "/local/jiayi3/temp_v.pt"
            torch.save(key_fake, path_k)
            torch.save(value_fake, path_v)
            b,s,h = self.cache_fuse_metadata["key_value_shape"]
            indices = (slice(0, b), slice(0, s), slice(0, h))
            #np.lib.format.open_memmap(path_k, mode="w+", shape=((b,s,h)), dtype=np.float16)
            disk_k = path_k
            #np.lib.format.open_memmap(path_v, mode="w+", shape=((b,s,h)), dtype=np.float16)
            disk_v = path_v
            #disk_k_tensor = torch.from_numpy(np.lib.format.open_memmap(disk_k))
            #disk_v_tensor = torch.from_numpy(np.lib.format.open_memmap(disk_v))
            #disk_k_tensor.copy_(key_fake)
            #disk_v_tensor.copy_(value_fake)
            
            disk_k_tensor = disk_k
            disk_v_tensor = disk_v
        '''
            
        if self.cache_fuse_metadata["stream_activate"]:
            for i in range(len(self.layers)-1):
                self.submit_load(self.key_buf_pin,self.value_buf_pin,self.key_buf,self.value_buf,self.load_streams[i], self.load_events[i])

        check_layer_idx = 0
        if flag:
            #time.sleep(1)
            #torch.cuda.synchronize()
            #with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
            for i in range(len(self.layers)):
                if self.cache_fuse_metadata["check"]:
                    if i in self.cache_fuse_metadata["check_layers"]:
                        temp_status = 1 # check this layer
                        self.cache_fuse_metadata["check_layer"] = self.cache_fuse_metadata["check_layers"][check_layer_idx]
                        check_layer_idx += 1
                    elif i > self.cache_fuse_metadata["check_layers"][0]:
                        temp_status = 2 # after check
                
                #if cache_fuse_metadata["check"]:
                #    import pdb
                #    pdb.set_trace()
                
                #if i == 1 get prefetch result. plan for actual loading.
                #Load kv into temprary tensors for Jiayi to use

                layer = self.layers[i]
                
                #FIXME(Jiayi): add double buffer to overcome data races
                #FIXME(Jiayi): use both cuda streams and multiprocessing to do pipeining (multithreading cause issues in ray)
                #FIXME(Jiayi): fetch_ky_layer should also include dtype? (type conversion should be postoned for efficiency)
                #if self.cache_fuse_metadata["stream_activate"] and i < len(self.layers)-1:
                #    self.submit_load(self.key_buf_pin,self.value_buf_pin,self.key_buf,self.value_buf,self.load_streams[i])
                        #key_buf.copy_(key_buf_pin, non_blocking=True)
                        #value_buf.copy_(value_buf_pin, non_blocking=True)
                        #key_buffed = self.cache_load_metadata['loader'].fetch_kv_layer(hash=self.cache_load_metadata['key_hash'],
                        #                                                            layer_idx=i+1,
                        #                                                            device=hidden_states.device)
                        #value_buffed = self.cache_load_metadata['loader'].fetch_kv_layer(hash=self.cache_load_metadata['key_hash'],
                        #                                                                layer_idx=i+1,
                        #                                                                device=hidden_states.device)
                
                #if input_ids.shape[1]>3800 and input_metadata.is_prompt:
                #    torch.cuda.synchronize()
                #    start = torch.cuda.Event(enable_timing=True)
                #    end = torch.cuda.Event(enable_timing=True)
                #    start.record()
                hidden_states, residual = layer(
                    positions, #FIXME(Jiayi): positions need to be changed
                    hidden_states,
                    kv_caches[i],
                    input_metadata,
                    residual,
                    status = temp_status,
                    cache_fuse_metadata = self.cache_fuse_metadata,
                    cache_load_metadata = None,
                    #cache_load_metadata = self.cache_load_metadata
                )
                #if input_ids.shape[1]>3800 and input_metadata.is_prompt:
                #    end.record()
                #    torch.cuda.synchronize()
                #    temp_time = start.elapsed_time(end)
                #    print(f"Layer{i}: {temp_time}")

                if temp_status==1:
                    positions = positions[:,self.cache_fuse_metadata["imp_token_indices"]]
                
                #Jiayi: sunchronize loading and recomputation
                if self.cache_fuse_metadata["stream_activate"] and i < len(self.layers)-1:
                    #print(f"Before: {self.load_queue.qsize()}")
                    print(self.load_events[i].is_set())
                    while (not self.load_events[i].is_set()):
                        time.sleep(0.001)
                        pass
                    #self.load_queue.join()
                    torch.cuda.current_stream().wait_stream(self.load_streams[i])
                    print(self.load_events[i].is_set())
                    print(f"After: {self.load_queue.qsize()}")
                    #torch.cuda.synchronize()
                    #time.sleep(0.05)
                    
                #    key_temp.copy_(key_buffed)
                #    value_temp.copy_(value_buffed)
            hidden_states, _ = self.norm(hidden_states, residual)
            #if self.cache_fuse_metadata["stream_activate"]:
            #    self.load_queue.join()
            if flag:
                for i in range(31):
                    self.load_events[i].clear()
                end.record()
                torch.cuda.synchronize()
                temp_time = start.elapsed_time(end)
                print(temp_time)
                #print(cache_fuse_metadata)
                #import pdb
                #pdb.set_trace()
                #sampling_metadata.selected_token_indices[0]= len(self.cache_fuse_metadata["imp_token_indices"])-1
                #sampling_metadata.prompt_lens[0] = len(self.cache_fuse_metadata["imp_token_indices"])
            #prof.export_chrome_trace("/dataheart/jiayi3/profile/trace.json")
        else:
            for i in range(len(self.layers)):
                if self.cache_fuse_metadata["check"]:
                    if i in self.cache_fuse_metadata["check_layers"]:
                        temp_status = 1 # check this layer
                        self.cache_fuse_metadata["check_layer"] = self.cache_fuse_metadata["check_layers"][check_layer_idx]
                        check_layer_idx += 1
                    elif i > self.cache_fuse_metadata["check_layers"][0]:
                        temp_status = 2 # after check
                

                layer = self.layers[i]
                
                hidden_states, residual = layer(
                    positions, #FIXME(Jiayi): positions need to be changed
                    hidden_states,
                    kv_caches[i],
                    input_metadata,
                    residual,
                    status = temp_status,
                    cache_fuse_metadata = self.cache_fuse_metadata,
                    cache_load_metadata = None,
                    #cache_load_metadata = self.cache_load_metadata
                )

                if temp_status==1:
                    positions = positions[:,self.cache_fuse_metadata["imp_token_indices"]]
                
            hidden_states, _ = self.norm(hidden_states, residual)
            
        return hidden_states, sampling_metadata


class LlamaForCausalLM(nn.Module):
    packed_modules_mapping = {
        "qkv_proj": [
            "q_proj",
            "k_proj",
            "v_proj",
        ],
        "gate_up_proj": [
            "gate_proj",
            "up_proj",
        ],
    }

    # LoRA specific attributes
    supported_lora_modules = [
        "qkv_proj",
        "o_proj",
        "gate_up_proj",
        "down_proj",
        "embed_tokens",
        "lm_head",
    ]
    embedding_modules = {
        "embed_tokens": "input_embeddings",
        "lm_head": "output_embeddings",
    }
    embedding_padding_modules = ["lm_head"]

    def __init__(
        self,
        config: LlamaConfig,
        linear_method: Optional[LinearMethodBase] = None,
        lora_config: Optional[LoRAConfig] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.linear_method = linear_method
        self.model = LlamaModel(config, linear_method, lora_config=lora_config)
        self.unpadded_vocab_size = config.vocab_size
        if lora_config:
            self.unpadded_vocab_size += lora_config.lora_extra_vocab_size
        self.lm_head = ParallelLMHead(
            self.unpadded_vocab_size,
            config.hidden_size,
            org_num_embeddings=config.vocab_size,
            padding_size=DEFAULT_VOCAB_PADDING_SIZE
            # We need bigger padding if using lora for kernel
            # compatibility
            if not lora_config else lora_config.lora_vocab_padding_size,
        )
        self.sampler = Sampler(self.unpadded_vocab_size, config.vocab_size)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_fuse_metadata=None,
        sampling_metadata=None
    ) -> torch.Tensor:
        hidden_states, sampling_metadata = self.model(input_ids, positions, kv_caches,
                                   input_metadata, 
                                   cache_fuse_metadata, 
                                   sampling_metadata)
        return hidden_states, sampling_metadata

    #HACK(Jiayi): sampler hacked
    def sample(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(self.lm_head.weight, hidden_states,
                                   sampling_metadata)
        return next_tokens

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ("qkv_proj", "q_proj", "q"),
            ("qkv_proj", "k_proj", "k"),
            ("qkv_proj", "v_proj", "v"),
            ("gate_up_proj", "gate_proj", 0),
            ("gate_up_proj", "up_proj", 1),
        ]
        params_dict = dict(self.named_parameters())
        for name, loaded_weight in hf_model_weights_iterator(
                model_name_or_path, cache_dir, load_format, revision):
            if "rotary_emb.inv_freq" in name:
                continue
            if ("rotary_emb.cos_cached" in name
                    or "rotary_emb.sin_cached" in name):
                # Models trained using ColossalAI may include these tensors in
                # the checkpoint. Skip them.
                continue
            for (param_name, weight_name, shard_id) in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                weight_loader(param, loaded_weight)
