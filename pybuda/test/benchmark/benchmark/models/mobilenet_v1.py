# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC

# SPDX-License-Identifier: Apache-2.0
import os
import pybuda
import torch

from ..common import benchmark_model
from pybuda.config import _get_global_compiler_config
from transformers import AutoModelForImageClassification


@benchmark_model(configs=["192", "224"])
def mobilenet_v1(training: bool, config: str, microbatch: int, devtype: str, arch: str, data_type: str):
    
    compiler_cfg = _get_global_compiler_config()
    compiler_cfg.enable_t_streaming = True
    compiler_cfg.enable_auto_transposing_placement = True

    if compiler_cfg.balancer_policy == "default":
        compiler_cfg.balancer_policy = "Ribbon"
        os.environ["PYBUDA_RIBBON2"] = "1"

    os.environ["PYBUDA_SUPRESS_T_FACTOR_MM"] = "8"

    # These are about to be enabled by default.
    #
    os.environ["PYBUDA_TEMP_ENABLE_NEW_FUSED_ESTIMATES"] = "1"
    if data_type != "Bfp8_b":
        os.environ["PYBUDA_TEMP_ENABLE_NEW_SPARSE_ESTIMATES"] = "1"

    if data_type == "Bfp8_b":
        # tenstorrent/pybuda#2228
        os.environ["PYBUDA_LEGACY_KERNEL_BROADCAST"] = "1"
        pybuda.config.configure_mixed_precision(name_regex="input.*add.*", output_df=pybuda.DataFormat.Float16_b)
        pybuda.config.configure_mixed_precision(op_type="add", output_df=pybuda.DataFormat.Float16_b)
        pybuda.config.configure_mixed_precision(op_type="depthwise", output_df=pybuda.DataFormat.Float16_b)

    # Set model parameters based on chosen task and model configuration
    model_name = ""
    if config == "192":
        model_name = "google/mobilenet_v1_0.75_192"
        img_res = 192
    elif config == "224":
        model_name = "google/mobilenet_v1_1.0_224"
        img_res = 224
    else:
        raise RuntimeError("Unknown config")

    # Load model
    model = AutoModelForImageClassification.from_pretrained(model_name)

    # Configure model mode for training or evaluation
    if training:
        model.train()
    else:
        model.eval()

    modules = {"tt": pybuda.PyTorchModule(f"pt_mobilenet_v1_{config}", model)}

    input_shape = (microbatch, 3, img_res, img_res)
    inputs = [torch.rand(*input_shape)]
    targets = tuple()

    # Add loss function, if training
    if training:
        model["cpu-loss"] = pybuda.PyTorchModule("l1loss", torch.nn.L1Loss())
        targets = [torch.rand(1, 100)]

    return modules, inputs, targets, {}
