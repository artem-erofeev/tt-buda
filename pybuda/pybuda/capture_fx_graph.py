# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC

# SPDX-License-Identifier: Apache-2.0

from loguru import logger

import torch

from pybuda.config import _get_global_compiler_config
import pybuda
from pybuda.tensor import to_buda_tensors, to_pt_tensors
from pybuda.tvm_utils import flatten_inputs

from pybuda._C.graph import Graph, create_op_node, create_data_edge, create_parameter_input, create_activation_input, create_output, create_constant_input, create_target_input, add_partial_datacopy_edge, RuntimeTensorTransform, RuntimeTensorTransformType, Shape, OpType

from pybuda.tensor import pytorch_dtype_to_buda_dataformat
import os
import sys
import math

from typing import List
from pybuda.tvm_to_python import Operation
from pybuda.python_codegen import PyTorchWriter, PyBudaWriter, PythonWriter

class PyBudaNode:
    def __init__(self, op: OpType, args: List[torch.fx.node.Node]):
        self.op = op
        self.args = args
        self.shape = None
        self.dtype = None
        self.wrap_tuple = None

def process_dummy_no_attr(node, pybuda_op_name):
    return PyBudaNode(OpType(pybuda_op_name, []), node.args)

def process_dummy_attr_in_args(node, pybuda_op_name):
    attrs = node.args[1] if len(node.args) == 2 else node.args[1:]
    if not isinstance(attrs, (list, tuple)):
        attrs = [attrs, ]
    return PyBudaNode(OpType(pybuda_op_name, attrs), [node.args[0], ])

def process_expand(node, pybuda_op_name):
    return PyBudaNode(OpType(pybuda_op_name, []), [node.args[0], ])

def process_flatten(node, pybuda_op_name):
    return PyBudaNode(OpType(pybuda_op_name, [-1, ]), [node.args[0], ])

def process_gelu(node, pybuda_op_name):
    return PyBudaNode(OpType(pybuda_op_name, ["none", ]), node.args)

def process_getitem(node, pybuda_op_name):
    breakpoint()
    num_dims = sum([(isinstance(dim, slice) and (dim.start is not None or dim.stop is not None)) or (not isinstance(dim, slice) and dim is not None) for dim in node.args[1]])
    if num_dims == 0:
        return PyBudaNode(OpType("nop", []), [node.args[0], ])
    assert num_dims <= 1, "TODO: Support multi axis getitem"
    for dim, slice_index in enumerate(node.args[1]):
        if isinstance(slice_index, slice) and slice_index.start is None and slice_index.stop is None:
            continue
        if isinstance(slice_index, int):
            start = slice_index
            stop = None
            stride = 1
        else:
            start = slice_index.start
            stop = slice_index.stop
            if slice_index.step is not None:
                stride = slice_index.step
            else:
                stride = 1

    if stop is None:
        stop = start + 1
    if stop < 0:
        stop += node.args[0].meta['tensor_meta'].shape[dim]
    
    return PyBudaNode(OpType(pybuda_op_name, [dim, start, stop, stride]), [node.args[0], ])

def process_transpose(node, pybuda_op_name):
    torch_op_name = node.target.__name__
    if torch_op_name == "permute":
        dim0 = None
        dim1 = None
        for i, arg in enumerate(node.args[1]):
            if arg != i:
                if dim0 is None:
                    dim0 = i
                elif dim1 is None:
                    dim1 = i
                else:
                    assert False, "Multi axis permute needs to be added to pybuda"

    elif torch_op_name == "transpose":
        dim0 = node.args[1]
        dim1 = node.args[2]
    
    dims = len(node.args[0].meta['tensor_meta'].shape)
    if dim0 > 0:
        dim0 -= dims
    if dim1 > 0:
        dim1 -= dims
    if dim0 > dim1:
        dim0, dim1 = dim1, dim0

    named_attrs = {"dim0": dim0, "dim1": dim1, "z_dim_slice": -1}

    return PyBudaNode(OpType(pybuda_op_name, named_attrs=named_attrs), [node.args[0], ])

def process_softmax(node, pybuda_op_name):
    if len(node.args) == 1:
        assert "dim" in node.kwargs, "dim must be specified"
        dim = node.kwargs["dim"]
    else:
        dim = node.args[1]
    
    if dim >= 0:
        dim -= len(node.args[0].meta['tensor_meta'].shape)
    stable = 1
    attrs = [dim, stable]
    return PyBudaNode(OpType(pybuda_op_name, attrs), [node.args[0], ])

def process_matmul(node, pybuda_op_name):
    assert len(node.args) == 2 or len(node.args) == 3
    if len(node.args) == 3:
        # Torch addmm inputs are bias, LHS, RHS
        args = [node.args[1], node.args[2], node.args[0]]
    else:
        args = node.args
    
    return PyBudaNode(OpType(pybuda_op_name, []), args)

def process_embedding(node, pybuda_op_name):
    assert len(node.args) == 2 or len(node.args) == 3

    #TODO Handle padding index (arg 2)
    args = [node.args[0], node.args[1]]
    return PyBudaNode(OpType(pybuda_op_name, []), args)

def process_layernorm(node, pybuda_op_name):
    assert len(node.args) == 5
    dim = -1
    epsilon = node.args[4]
    attrs = [dim, epsilon]

    args = [node.args[0], node.args[2], node.args[3]]
    pybuda_node = PyBudaNode(OpType(pybuda_op_name, attrs), args)
    pybuda_node.shape = node.meta['tensor_meta'][0].shape
    pybuda_node.dtype = pytorch_dtype_to_buda_dataformat(node.meta['tensor_meta'][0].dtype)
    pybuda_node.wrap_tuple = True
    return pybuda_node

def process_select(node, pybuda_op_name):
    assert len(node.args) == 3

    dim = node.args[1]
    if dim >= 0:
        dim -= len(node.args[0].meta['tensor_meta'].shape)
    index = node.args[2]
    attrs = [dim, index, index+1, 1]
    args = [node.args[0], ]
    return PyBudaNode(OpType(pybuda_op_name, attrs), args)

def process_slice(node, pybuda_op_name):
    assert len(node.args) == 4

    dim = node.args[1]
    start = node.args[2]
    end = node.args[3]
    if dim >= 0:
        dim -= len(node.args[0].meta['tensor_meta'].shape)
    if start == 0 and end == sys.maxsize:
        pybuda_node = PyBudaNode(OpType("nop", []), [node.args[0], ])
    else:
        stride = 1
        attrs = [dim, start, end, stride]
        args = [node.args[0], ]
        pybuda_node = PyBudaNode(OpType(pybuda_op_name, attrs), args)
    return pybuda_node

def process_usqueeze(node, pybuda_op_name):
    assert len(node.args) == 2
    dim = node.args[1]
    input_ndim = len(node.meta['tensor_meta'].shape)

    if dim >= 0:
        dim -= input_ndim
    
    attrs = [dim, input_ndim]
    return PyBudaNode(OpType(pybuda_op_name, attrs), [node.args[0], ])

def process_reshape(node, pybuda_op_name):
    attrs = node.args[1].copy() if len(node.args) == 2 else node.args[1:].copy()
    if not isinstance(attrs, (list, tuple)):
        attrs = [attrs, ]

    input_volume = 1
    for dim in node.args[0].meta['tensor_meta'].shape:
        input_volume *= dim

    blank_index = None
    reshape_volume = 1
    for i, dim in enumerate(attrs):
        if dim == -1:
            assert blank_index is None, "Only one dimension can be -1"
            blank_index = i
        else:
            reshape_volume *= dim
    
    if blank_index is not None:
        attrs[blank_index] = input_volume//reshape_volume

    input_volume = node.args[0].meta['tensor_meta'].shape[0]
    return PyBudaNode(OpType(pybuda_op_name, attrs), [node.args[0], ])

def process_power(node, pybuda_op_name):
    if isinstance(node.args[1], int) or isinstance(node.args[1], float) and math.isclose(node.args[1] / int(node.args[1]), 1.0):
        attrs = [int(node.args[1]), ]
        pybuda_node = PyBudaNode(OpType("pow", attrs), [node.args[0], ])
    else:
        pybuda_node = PyBudaNode(OpType("power", []), node.args)
    return pybuda_node

def process_cat(node, pybuda_op_name):
    dim = node.args[1]
    if dim >= 0:
        dim -= len(node.meta['tensor_meta'].shape)
    pybuda_node = PyBudaNode(OpType(pybuda_op_name, [dim, ]), node.args[0])
    return pybuda_node


dynamo_to_pybuda_function = {
    "_softmax"                      : (process_softmax, "softmax"),
    "add"                           : (process_dummy_no_attr, "add"),
    "addmm"                         : (process_matmul, "matmul"),
    "bmm"                           : (process_matmul, "matmul"),
    "cat"                           : (process_cat, "concatenate"),
    "clone"                         : (process_dummy_no_attr, "nop"),
    "contiguous"                    : (process_dummy_no_attr, "nop"),
    "div"                           : (process_matmul, "divide"),
    "embedding"                     : (process_embedding, "embedding"),
    "expand"                        : (process_expand, "nop"),
    "flatten"                       : (process_flatten, "reshape"),
    "gelu"                          : (process_gelu, "gelu"),
    "getitem"                       : (process_getitem, "index"),
    "iadd"                          : (process_dummy_no_attr, "add"),
    "matmul"                        : (process_dummy_no_attr, "matmul"),
    "mm"                            : (process_matmul, "matmul"),
    "mul"                           : (process_dummy_no_attr, "multiply"),
    "native_layer_norm"             : (process_layernorm, "layernorm"),
    "permute"                       : (process_transpose, "transpose"),
    "select"                        : (process_select, "index"),
    "slice"                         : (process_slice, "index"),
    "softmax"                       : (process_softmax, "softmax"),
    "sub"                           : (process_dummy_no_attr, "subtract"),
    "tanh"                          : (process_dummy_no_attr, "tanh"),
    "to"                            : (process_dummy_no_attr, "nop"), #TODO
    "_to_copy"                      : (process_dummy_no_attr, "nop"), #TODO
    "transpose"                     : (process_transpose, "transpose"),
    "truediv"                       : (process_dummy_no_attr, "divide"),
    "unsqueeze"                     : (process_usqueeze, "unsqueeze"),
    "view"                          : (process_reshape, "reshape"),
    "where"                         : (process_dummy_no_attr, "where"),
    "pow"                           : (process_power, ""),
}

torch_constant_ops = {
    "ones"                           : torch.ones,
    "zeros"                          : torch.zeros,
    "arange"                         : torch.arange,
    "full"                           : torch.full,
}

# graph = None
node_to_id = {}
param_to_id = {}
const_to_id = {}
id_to_intermed = {}

def get_pybuda_node(torch_op_name, node):
    if torch_op_name in dynamo_to_pybuda_function:
        return dynamo_to_pybuda_function[torch_op_name][0](node, dynamo_to_pybuda_function[torch_op_name][1])
    else:
        print(f"Unsupported op {torch_op_name}")
        breakpoint()
        assert False, f"Unsupported op {torch_op_name}"

# Check to see if subgraph is already on device
def is_on_device(subgraph_idx: int):
    pass

# Remove all nodes associated with subgraph
def remove_subgraph(subgraph_idx: int):
    pass

def add_op(graph, node, name, pybuda_node, subgraph_idx):
    global node_to_id
    shape = node.meta['tensor_meta'].shape if pybuda_node.shape is None else pybuda_node.shape
    dtype = pytorch_dtype_to_buda_dataformat(node.meta['tensor_meta'].dtype) if pybuda_node.dtype is None else pybuda_node.dtype

    add_constants_if_necessary(graph, pybuda_node.args, subgraph_idx)
    nid = create_op_node(
            graph,
            f"{name}_{subgraph_idx}",
            pybuda_node.op,
            [int(dim) for dim in shape],
            pytorch_dtype_to_buda_dataformat(dtype),
            subgraph_idx,
            {})
    
    for i, input_node in enumerate(pybuda_node.args):
        create_data_edge(graph, node_to_id[input_node], 0, nid, i, [])

    eval_args = [id_to_intermed[node_to_id[arg]] if isinstance(arg, torch.fx.node.Node) else arg for arg in node.args]
    for idx, arg in enumerate(eval_args):
        if isinstance(arg, (list, tuple)):
            eval_args[idx] = [id_to_intermed[node_to_id[a]] if isinstance(a, torch.fx.node.Node) else a for a in arg]
    kwargs = {k:v for k, v in node.kwargs.items() if k != "device"}
    id_to_intermed[nid] = node.target(*eval_args, **kwargs)
    if (pybuda_node.wrap_tuple):
        nid = (nid,)
    return nid

def add_input(graph, node, subgraph_idx, module_inputs):
    nid = create_activation_input(
            graph,
            f"{node.name}_{subgraph_idx}",
            [int(dim) for dim in node.meta['tensor_meta'].shape],
            node.meta["tensor_meta"].requires_grad,
            pytorch_dtype_to_buda_dataformat(node.meta["tensor_meta"].dtype),
            subgraph_idx)
    module_inputs.append(nid)
    return nid
    

def add_constant(graph, name, tensor, subgraph_idx):
    if tensor in const_to_id:
        return const_to_id[tensor]
    nid = create_constant_input(
            graph, 
            f"{name}_{subgraph_idx}",
            tensor,
            [int(dim) for dim in tensor.shape],
            pytorch_dtype_to_buda_dataformat(tensor.dtype),
            subgraph_idx)
    const_to_id[tensor] = nid
    return nid

def add_param(graph, name, torch_param, subgraph_idx):
    if name in param_to_id:
        return param_to_id[name]
    nid = create_parameter_input(
            graph, 
            name,
            [int(dim) for dim in torch_param.shape],
            torch_param.requires_grad,
            pytorch_dtype_to_buda_dataformat(torch_param.dtype),
            subgraph_idx)
    param_to_id[name] = nid
    return nid

def add_outputs(graph, node, subgraph_idx, output_nids, output_requires_grad, output_tensors):
    global node_to_id
    for index, meta in enumerate(node.meta['tensor_meta']):
        arg = node.args[0][index]
        nid = create_output(
                graph, 
                node.name + "_" + arg.name + "_" + str(subgraph_idx),
                [int(dim) for dim in meta.shape],
                pytorch_dtype_to_buda_dataformat(meta.dtype),
                False,  #TODO Loss output
                subgraph_idx)
        create_data_edge(graph, node_to_id[arg], 0, nid, index, [])
        output_nids.append(nid)
        output_requires_grad.append(meta.requires_grad)
        output_tensors.append(id_to_intermed[node_to_id[arg]])

def add_constants_if_necessary(graph, ops, subgraph_idx):
    global node_to_id
    for op in ops:
        if isinstance(op, (float, int)):
            if op in node_to_id:
                continue
            tensor = torch.ones([1]) * op
            node_to_id[op] = add_constant(graph, f"{op}", tensor, subgraph_idx)
            id_to_intermed[node_to_id[op]] = tensor

def append_to_graph(graph, module, aten_module, rand_atan_inputs, activations, subgraph_idx):
    torch.fx.passes.shape_prop.ShapeProp(aten_module).propagate(*rand_atan_inputs)
    # aten_module.graph.print_tabular()

    module_inputs = []
    output_nids = []
    output_requires_grad = []
    output_tensors = []

    def process_function(node):
        global node_to_id
        op_name = node.target.__name__
        if op_name in torch_constant_ops:
            kwargs = {k:v for k, v in node.kwargs.items() if k != "device"}
            tensor = torch_constant_ops[op_name](*node.args, **kwargs)
            if len(tensor.shape) == 0:
                tensor = tensor.unsqueeze(0)
            node_to_id[node] = add_constant(graph, node.name, tensor.float(), subgraph_idx)
            id_to_intermed[node_to_id[node]] = tensor
        elif op_name == "getitem":
            assert isinstance(node_to_id[node.args[0]], (list, tuple))
            node_to_id[node] = node_to_id[node.args[0]][node.args[1]]
            id_to_intermed[node_to_id[node]] = id_to_intermed[node_to_id[node]][node.args[1]]
        else:
            pybuda_node = get_pybuda_node(op_name, node)
            node_to_id[node] = add_op(graph, node, node.name, pybuda_node, subgraph_idx)

    params = list(module.named_parameters(remove_duplicate=False)) + list(module.named_buffers(remove_duplicate=False))
    assert len(params) == len(torch._guards.TracingContext.get().params_flat)

    for index, node in enumerate(aten_module.graph.nodes):
        if index < len(params):
            # params are located first in the args list
            assert node.op == "placeholder"
            assert node.meta['val'].size() == rand_atan_inputs[index].shape
            node_to_id[node] = add_param(graph, params[index][0], params[index][1].data, subgraph_idx)
            id_to_intermed[node_to_id[node]] = params[index][1].data
            continue
        if node.op == "placeholder":
            node_to_id[node] = add_input(graph, node, subgraph_idx, module_inputs)
            id_to_intermed[node_to_id[node]] = activations[index - len(params)]
        elif node.op == "get_attr":
            assert False #TODO
            node_to_id[node] = add_param(graph, node.target, module.state_dict()[node.target], subgraph_idx)
        elif node.op == "call_function":
            process_function(node)
        elif node.op == "output":
            add_outputs(graph, node, subgraph_idx, output_nids, output_requires_grad, output_tensors)
        else:
            assert False, f"Unsupported op {node.op}"


    graph.register_module_inputs(module_inputs)
    graph.register_module_outputs(output_nids, output_requires_grad)
    return graph, id_to_intermed, output_tensors
