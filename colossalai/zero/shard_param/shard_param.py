from enum import Enum
from optparse import Option
import torch
from colossalai.zero.sharded_model._zero3_utils import get_shard
from colossalai.context.parallel_mode import ParallelMode
from colossalai.core import global_context as gpc
import torch.distributed as dist

class TensorType(Enum):
    GRAD = 1
    DATA = 2

class ShardParam(object):
    r"""
    A wrapper to torch.nn.Parameter. Shard a param
    on different processes.
    """
    def __init__(self,
            param: torch.nn.Parameter,
            tensor_type: TensorType = TensorType.DATA,
            process_group = None,
        ) -> None:
        self.process_group = process_group or gpc.get_group(ParallelMode.DATA)
        self.world_size = dist.get_world_size(self.process_group)
        self.local_rank = dist.get_rank(self.process_group)
        self._param_payload = param.data if tensor_type == TensorType.DATA else param.grad
        self._payload_numel = None
        self._origin_shape = param.shape
        self._origin_numel = param.numel()
        self.is_shared = False
    
    def payload(self, target_device : torch.device):
        return self._param_payload.to(target_device)

    def shard(self):
        r"""
        Distributed the payload of param to all processes.
        """
        if self.is_shared:
            return
        self._param_payload, _ = get_shard(self._param_payload, self.local_rank, self.world_size)
        self.is_shared = True
    
    def gather(self):
        r"""
        Collect the payload of param from different processes to process of local rank.
        """
        if not self.is_shared:
            return
        
        buffer_list = []
        payload_numel = self._param_payload.numel()
        for i in range(self.world_size):
            if i == self.local_rank:
                buffer_list.append(self._param_payload.cuda())
            else:
                buffer_list.append(torch.zeros(payload_numel).cuda())
        
        torch.distributed.all_gather(buffer_list, buffer_list[self.local_rank], group=self.process_group, async_op=False)
        print(buffer_list)
        self._param_payload = torch.narrow(torch.cat(buffer_list), 0, 0, self._origin_numel).view(self._origin_shape)
        self.is_shared = False
