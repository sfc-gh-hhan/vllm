from collections import namedtuple
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import torch.distributed
from torch.distributed import ProcessGroup

from .parallel_state import (get_pipeline_model_parallel_group,
                             get_pipeline_model_parallel_next_rank,
                             get_pipeline_model_parallel_prev_rank,
                             get_pp_communication_method,
                             get_tensor_model_parallel_group,
                             get_tensor_model_parallel_rank,
                             get_tensor_model_parallel_world_size,
                             is_pynccl_enabled_for_all_reduce)


def tensor_model_parallel_all_reduce(input_: torch.Tensor) -> torch.Tensor:
    """All-reduce the input tensor across model parallel group.

    NOTE: This operation will be applied in-place on the input tensor if
    disable_custom_all_reduce is set to True. Otherwise, this operation may or
    may not be applied in place depending on whether custom all reduce is
    invoked for a particular tensor, which further depends on the tensor size
    and GPU topology.

    TLDR: always assume this function modifies its input, but use the return
    value as the output.
    """
    from vllm.distributed.device_communicators import pynccl_utils
    from vllm.distributed.device_communicators.custom_all_reduce import (
        custom_all_reduce)

    # Bypass the function if we are using only 1 GPU.
    if get_tensor_model_parallel_world_size() == 1:
        return input_
    out = custom_all_reduce(input_)
    if out is not None:
        return out
    if is_pynccl_enabled_for_all_reduce():
        # TODO: support multiple parallel groups.
        pynccl_utils.all_reduce(input_)
    else:
        torch.distributed.all_reduce(input_,
                                     group=get_tensor_model_parallel_group())
    return input_


def tensor_model_parallel_all_gather(input_: torch.Tensor,
                                     dim: int = -1) -> torch.Tensor:
    """All-gather the input tensor across model parallel group."""
    world_size = get_tensor_model_parallel_world_size()
    # Bypass the function if we are using only 1 GPU.
    if world_size == 1:
        return input_
    assert -input_.dim() <= dim < input_.dim(), (
        f"Invalid dim ({dim}) for input tensor with shape {input_.size()}")
    if dim < 0:
        # Convert negative dim to positive.
        dim += input_.dim()
    input_size = input_.size()
    # Allocate output tensor.
    output_tensor = torch.empty((world_size, ) + input_size,
                                dtype=input_.dtype,
                                device=input_.device)
    # All-gather.
    torch.distributed.all_gather_into_tensor(
        output_tensor, input_, group=get_tensor_model_parallel_group())
    # Reshape
    output_tensor = output_tensor.movedim(0, dim)
    output_tensor = output_tensor.reshape(input_size[:dim] +
                                          (world_size * input_size[dim], ) +
                                          input_size[dim + 1:])
    return output_tensor


def tensor_model_parallel_gather(input_: torch.Tensor,
                                 dst: int = 0,
                                 dim: int = -1) -> torch.Tensor:
    """Gather the input tensor across model parallel group.

    NOTE: We assume that the input tensor is on the same device across
    all the ranks.
    """
    world_size = get_tensor_model_parallel_world_size()
    # Bypass the function if we are using only 1 GPU.
    if world_size == 1:
        return input_
    assert -input_.dim() <= dim < input_.dim(), (
        f"Invalid dim ({dim}) for input tensor with shape {input_.size()}")
    if dim < 0:
        # Convert negative dim to positive.
        dim += input_.dim()
    # Allocate output tensor.
    if torch.distributed.get_rank() == dst:
        gather_list = [torch.empty_like(input_) for _ in range(world_size)]
    else:
        gather_list = None
    # Gather.
    torch.distributed.gather(input_,
                             gather_list,
                             dst=dst,
                             group=get_tensor_model_parallel_group())
    if torch.distributed.get_rank() == dst:
        output_tensor = torch.cat(gather_list, dim=dim)
    else:
        output_tensor = None
    return output_tensor


def broadcast(input_: torch.Tensor,
              src: int = 0,
              group: Optional[ProcessGroup] = None):
    """Broadcast the input tensor."""
    group = group or torch.distributed.group.WORLD
    ranks = torch.distributed.get_process_group_ranks(group)
    assert src in ranks, f"Invalid src rank ({src})"

    # Bypass the function if we are using only 1 GPU.
    world_size = torch.distributed.get_world_size(group=group)
    if world_size == 1:
        return input_
    # Broadcast.
    torch.distributed.broadcast(input_, src=src, group=group)
    return input_


def broadcast_object_list(obj_list: List[Any],
                          src: int = 0,
                          group: Optional[ProcessGroup] = None):
    """Broadcast the input object list."""
    group = group or torch.distributed.group.WORLD
    ranks = torch.distributed.get_process_group_ranks(group)
    assert src in ranks, f"Invalid src rank ({src})"

    # Bypass the function if we are using only 1 GPU.
    world_size = torch.distributed.get_world_size(group=group)
    if world_size == 1:
        return obj_list
    # Broadcast.
    torch.distributed.broadcast_object_list(obj_list, src=src, group=group)
    return obj_list


TensorMetadata = namedtuple("TensorMetadata", ["dtype", "size"])


def broadcast_tensor_dict(
    tensor_dict: Optional[Dict[Any, Union[torch.Tensor, Any]]] = None,
    src: int = 0,
    group: Optional[ProcessGroup] = None,
) -> Optional[Dict[Any, Union[torch.Tensor, Any]]]:
    """Broadcast the input tensor dictionary."""
    group = group or torch.distributed.group.WORLD
    ranks = torch.distributed.get_process_group_ranks(group)
    assert src in ranks, f"Invalid src rank ({src})"

    # Bypass the function if we are using only 1 GPU.
    world_size = torch.distributed.get_world_size(group=group)
    if world_size == 1:
        return tensor_dict

    rank = torch.distributed.get_rank()
    if rank == src:
        metadata_list: List[Tuple[Any, Any]] = []
        assert isinstance(
            tensor_dict,
            dict), (f"Expecting a dictionary, got {type(tensor_dict)}")
        for key, value in tensor_dict.items():
            if isinstance(value, torch.Tensor):
                assert value.is_cuda, (
                    f"Tensor {key}: {value} is not on cuda. Currently we only "
                    f"support broadcasting tensors on cuda.")
                metadata_list.append(
                    (key, TensorMetadata(value.dtype, value.size())))
            else:
                metadata_list.append((key, value))
        torch.distributed.broadcast_object_list([metadata_list],
                                                src=src,
                                                group=group)
        async_handles = []
        for key, value in metadata_list:
            if isinstance(value, TensorMetadata):
                tensor = tensor_dict[key]
                async_handles.append(
                    torch.distributed.broadcast(tensor,
                                                src=src,
                                                group=group,
                                                async_op=True))
        for async_handle in async_handles:
            async_handle.wait()

    else:
        recv_metadata_list = [None]
        torch.distributed.broadcast_object_list(recv_metadata_list,
                                                src=src,
                                                group=group)
        assert recv_metadata_list[0] is not None
        tensor_dict = {}
        async_handles = []
        for key, value in recv_metadata_list[0]:
            if isinstance(value, TensorMetadata):
                tensor = torch.empty(value.size,
                                     dtype=value.dtype,
                                     device="cuda")
                async_handle = torch.distributed.broadcast(tensor,
                                                           src=src,
                                                           async_op=True,
                                                           group=group)
                async_handles.append(async_handle)
                tensor_dict[key] = tensor
            else:
                tensor_dict[key] = value
        for async_handle in async_handles:
            async_handle.wait()
    return tensor_dict


def send_next_rank(tensors: List[torch.Tensor]) -> None:
    """Send the tensors to the next pipeline model parallel rank."""
    combined_tensor = torch.cat(tensors, dim=0)

    communication_method = get_pp_communication_method()
    if communication_method == "signal":
        torch.distributed.send(combined_tensor.reshape(-1)[0],
                               get_pipeline_model_parallel_next_rank(),
                               get_pipeline_model_parallel_group())
    elif communication_method == "allgather":
        _send_next_rank_sliced(combined_tensor)
    else:
        assert communication_method == "send_recv"
        torch.distributed.send(combined_tensor,
                               get_pipeline_model_parallel_next_rank(),
                               get_pipeline_model_parallel_group())


def _send_next_rank_sliced(tensor: torch.Tensor):
    tensor_parallel_rank = get_tensor_model_parallel_rank()
    # reshape to (tp_size, size // tp_size), then simply send (size // tp_size)
    tensor = tensor.reshape(get_tensor_model_parallel_world_size(), -1)
    sliced = tensor[tensor_parallel_rank]
    torch.distributed.send(sliced,
                           get_pipeline_model_parallel_next_rank(),
                           get_pipeline_model_parallel_group())


def recv_prev_rank(num_tensors: int, sizes: torch.Size, dtype: torch.dtype,
                   device: torch.device) -> List[torch.Tensor]:
    sizes = list(sizes)
    """Receive tensors from the previous pipeline model parallel rank."""
    combined_tensor = torch.empty([sizes[0] * num_tensors] + sizes[1:],
                                  dtype=dtype,
                                  device=device)

    communication_method = get_pp_communication_method()
    if communication_method == "signal":
        combined_tensor = torch.ones([sizes[0] * num_tensors] + sizes[1:],
                                     dtype=dtype,
                                     device=device)
        torch.distributed.recv(combined_tensor.reshape(-1)[0],
                               get_pipeline_model_parallel_prev_rank(),
                               get_pipeline_model_parallel_group())

    elif communication_method == "allgather":
        combined_tensor = _recv_next_rank_sliced(combined_tensor)
    else:
        assert communication_method == "send_recv"
        torch.distributed.recv(combined_tensor,
                               get_pipeline_model_parallel_prev_rank(),
                               get_pipeline_model_parallel_group())

    return torch.chunk(combined_tensor, num_tensors, dim=0)

def _recv_next_rank_sliced(tensor: torch.Tensor):
    prev_shape = tensor.shape
    # Step 1: receive a slice
    tensor_parallel_rank = get_tensor_model_parallel_rank()
    # reshape to (tp_size, size // tp_size), then simply recv (size // tp_size)
    tensor = tensor.reshape(get_tensor_model_parallel_world_size(), -1)

    # torch.distributed.recv(tensor[tensor_parallel_rank],
    sliced = torch.empty_like(tensor[tensor_parallel_rank])
    torch.distributed.recv(sliced,
                           get_pipeline_model_parallel_prev_rank(),
                           get_pipeline_model_parallel_group())
    # Step 2: AllGather for all slices
    torch.distributed.all_gather_into_tensor(
        tensor, sliced, group=get_tensor_model_parallel_group()
    )
    return tensor.reshape(prev_shape)
