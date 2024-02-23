# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.

import argparse
import os
from dataclasses import dataclass, field
from timeit import default_timer as timer
from typing import Any, Dict, List, Union

import numpy as np

# torch imports
import torch
import torch.nn.functional as F
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.sharded_grad_scaler import ShardedGradScaler

from torchtrain.checkpoint import CheckpointManager, IntervalType
from torchtrain.config_manager import JobConfig

# torchtrain related
from torchtrain.datasets import create_tokenizer, dataloader_fn
from torchtrain.logging_utils import init_logger, rank0_log
from torchtrain.lr_scheduling import get_lr_scheduler
from torchtrain.metrics import build_metric_logger, get_num_params, GPUMemoryMonitor

from torchtrain.models import model_name_to_cls, model_name_to_tokenizer, models_config
from torchtrain.parallelisms import models_parallelize_fns, ParallelDims

from torchtrain.profiling import maybe_run_profiler
from torchtrain.utils import dist_max, dist_mean


@dataclass
class TrainState:
    step: int = 0
    current_loss: float = -1
    losses: List[float] = field(default_factory=list)

    def state_dict(self) -> Dict[str, Any]:
        return {
            "step": torch.tensor(self.step, dtype=torch.int32),
            "current_loss": torch.tensor(self.current_loss, dtype=torch.float32),
            "losses": torch.tensor(self.current_loss, dtype=torch.float32),
        }

    def load_state_dict(self, state_dict) -> None:
        self.step = state_dict["step"].item()
        self.current_loss = state_dict["current_loss"].item()
        self.losses = state_dict["losses"].tolist()


def build_optimizer(model, job_config: JobConfig):
    # build optimizer
    name = job_config.optimizer.name
    lr = job_config.optimizer.lr
    if name == "Adam":
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    elif name == "AdamW":
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    else:
        raise NotImplementedError(f"optimizer {name} not added")

    return optimizer


def build_grad_scaler(model):
    # apply gradient scaling if mixed precision training is enabled with fp16 param dtype
    if model.mixed_precision.param_dtype == torch.float16:
        enable_grad_scaling = True
        rank0_log("Enabling gradient scaling for mixed precision training.")
    else:
        enable_grad_scaling = False
        rank0_log("Gradient scaling not enabled.")

    return ShardedGradScaler(enabled=enable_grad_scaling)


def main(job_config: JobConfig):
    init_logger()
    # init world mesh
    world_size = int(os.environ["WORLD_SIZE"])
    parallel_dims = ParallelDims(
        dp=job_config.training.data_parallel_degree,
        sp=job_config.training.sequence_parallel_degree,
        pp=job_config.training.pipeline_parallel_degree,
        world_size=world_size
    )
    world_mesh = parallel_dims.build_mesh(device_type="cuda")

    model_name = job_config.model.name
    rank0_log(f"Building {model_name}")
    # build tokenizer
    tokenizer_type = model_name_to_tokenizer[model_name]
    tokenizer = create_tokenizer(tokenizer_type, job_config.model.tokenizer_path)

    # build dataloader
    # need dp world size and rank
    # TODO: dp might not always be 0 so we need to handle that more carefully
    dp_degree = world_mesh.size(0)
    dp_rank = world_mesh.get_local_rank(0)
    build_dataloader_fn = dataloader_fn[job_config.training.dataset]
    data_loader = build_dataloader_fn(
        tokenizer,
        job_config.training.batch_size,
        job_config.training.seq_len,
        dp_degree,
        dp_rank,
    )

    # build model
    # TODO: add meta initialization
    model_cls = model_name_to_cls[model_name]
    model_config = models_config[model_name][job_config.model.config]
    model_config.vocab_size = tokenizer.n_words

    model = model_cls.from_model_args(model_config)

    # log model size
    model_param_count = get_num_params(model)
    rank0_log(
        f"Model {model_name} {job_config.model.config} size: {model_param_count:,} total parameters"
    )
    gpu_metrics = GPUMemoryMonitor("cuda")
    rank0_log(f"GPU memory usage: {gpu_metrics}")

    # apply PTD parallelisms + AC
    model = models_parallelize_fns[model_name](model, world_mesh, parallel_dims, job_config)

    # to use FSDP-customized gradient scaler and gradient clipping solutions
    assert isinstance(model, FSDP)

    # build optimizer after apply parallelisms to the model
    optimizer = build_optimizer(model, job_config)
    scheduler = get_lr_scheduler(optimizer, job_config)

    scaler = build_grad_scaler(model)

    metric_logger = build_metric_logger()

    # torch.compile model for improved performance
    if job_config.training.compile:
        rank0_log(f"Compiling model {model_name} with torch.compile...")
        model = torch.compile(
            model,
        )

    train_state = TrainState()

    # train loop
    model.train()

    checkpoint = CheckpointManager(
        model=model,
        optimizer=optimizer,
        states={"train_state": train_state},
        folder=job_config.training.checkpoint_folder,
        interval_type=(
            IntervalType.SECONDS
            if job_config.training.checkpoint_interval_type == "seconds"
            else IntervalType.STEPS
        ),
        interval=job_config.training.checkpoint_interval,
    )
    checkpoint.load()

    with maybe_run_profiler() as torch_profiler:
        checkpoint.reset()
        # variables used to keep info for metrics logging
        losses_since_last_log: List[float] = []
        nwords_since_last_log = 0
        time_last_log = timer()
        while train_state.step < job_config.training.steps or job_config.training.steps == -1:
            train_state.step += 1
            # get batch
            batch = next(iter(data_loader))
            input_ids, labels = batch
            input_ids = input_ids.cuda()
            labels = labels.cuda()
            nwords_since_last_log += labels.numel()

            optimizer.zero_grad()

            # forward
            pred = model(input_ids)
            tok_loss = F.cross_entropy(
                pred.flatten(0, 1), labels.flatten(0, 1), reduction="none"
            )
            loss = tok_loss.mean()

            # backward on scaled loss to create scaled gradients
            scaler.scale(loss).backward()

            # clip gradients (after unscaling gradients of the optimizer's params)
            scaler.unscale_(optimizer)
            model.clip_grad_norm_(job_config.training.max_norm)

            # optimizer step
            # If gradients don't contain infs/NaNs, optimizer.step() is then called;
            # otherwise, optimizer.step() is skipped.
            scaler.step(optimizer)

            # updates the scale for next iteration
            scaler.update()

            # if profiler is active
            if torch_profiler:
                torch_profiler.step()

            train_state.current_loss = loss.item()
            train_state.losses.append(train_state.current_loss)
            losses_since_last_log.append(train_state.current_loss)

            # log metrics
            if (train_state.step - 1) % job_config.metrics.log_freq == 0:
                avg_loss, max_loss = np.mean(losses_since_last_log), np.max(
                    losses_since_last_log
                )
                global_avg_loss, global_max_loss = dist_mean(
                    avg_loss, world_mesh
                ), dist_max(max_loss, world_mesh)

                time_delta = timer() - time_last_log
                wps = nwords_since_last_log / (
                    time_delta * parallel_dims.model_parallel_size
                )

                gpu_mem_stats = gpu_metrics.get_current_stats(return_data=True)

                metrics = {
                    "loss_metrics/global_avg_loss": global_avg_loss,
                    "loss_metrics/global_max_loss": global_max_loss,
                    "wps": wps,
                    "memory_current/active(%)": gpu_mem_stats.active_curr,
                    "memory_current/allocated(%)": gpu_mem_stats.allocated_curr,
                    "memory_current/reserved(%)": gpu_mem_stats.reserved_curr,
                    "memory_peak/active(%)": gpu_mem_stats.active_peak,
                    "memory_peak/allocated(%)": gpu_mem_stats.allocated_peak,
                    "memory_peak/reserved(%)": gpu_mem_stats.reserved_peak,
                }
                metric_logger.log(metrics, step=train_state.step)

                losses_since_last_log.clear()
                nwords_since_last_log = 0
                time_last_log = timer()

            rank0_log(
                f"step: {train_state.step}, current loss: {train_state.current_loss}, lr: {scheduler.get_last_lr()}"
            )
            scheduler.step()

            checkpoint.save(train_state.step, force=(train_state.step == job_config.training.steps))

    metric_logger.close()
    rank0_log(f"{gpu_metrics.get_current_stats()}")

if __name__ == "__main__":
    config = JobConfig()
    main(config)
