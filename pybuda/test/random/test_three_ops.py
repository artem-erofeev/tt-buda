# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC

# SPDX-License-Identifier: Apache-2.0
# Randomize 3 ops in a fork-join setup ( A -> B, C, B -> C )

import torch
import pybuda
import random
from pybuda.verify import verify_module, VerifyConfig, TestKind
    
class ThreeOpModel(torch.nn.Module):
    def __init__(self, rng, cols1, cols2):
        super(ThreeOpModel, self).__init__()
        self.rng = rng
        
        self.op1 = self.rng.choice(["matmul", "conv2d"])
        self.op2 = self.rng.choice(["sqrt", "tanh", "add"])
        self.op3 = self.rng.choice(["matmul", "eltwise"])

        self.fc1 = torch.nn.Linear(cols1, cols2)
        self.conv1 = torch.nn.Conv2d(cols1, cols2, kernel_size=3, stride=1, padding=1)

    def forward(self, act):

        if self.op1 == "matmul":
            a = self.fc1(act)
        elif self.op1 == "conv2d":
            a = self.conv1(act)
        else:
            raise Exception("Unknown op1")

        if self.op2 == "sqrt":
            b = torch.sqrt(a)
        elif self.op2 == "tanh":
            b = torch.tanh(a)
        elif self.op2 == "add":
            b = a + 1
        else:
            raise Exception("Unknown op2")

        if self.op3 == "matmul":
            c = torch.matmul(a, torch.transpose(b, 1, 2))
        elif self.op3 == "eltwise":
            c = a + b
        else:
            raise Exception("Unknown op3")

        return c


def test_three_ops(test_index, random_seeds, test_device):
    rng = random.Random(random_seeds[test_index])
    rows = rng.randint(16, 512)
    cols1 = rng.randint(16, 512)
    cols2 = rng.randint(16, 512)
    microbatch_size = rng.randint(1, 8)

    model = ThreeOpModel(rng, cols1, cols2)
    input_shape = (microbatch_size, rows, cols1) if model.op1 == "matmul" else (microbatch_size, cols1, rows, 32)

    verify_module(pybuda.PyTorchModule(f"three_op_model_{test_index}", model), [input_shape],
            VerifyConfig(test_kind=TestKind.INFERENCE, devtype=test_device.devtype, arch=test_device.arch))
                
