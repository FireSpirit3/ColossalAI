import torch
from typing import List, Optional
from colossalai.logging import get_dist_logger
from colossalai.context.singleton_meta import SingletonMeta


class PyTorchProcessGroupDict(metaclass=SingletonMeta):

    def __init__(self):
        # distributed settings
        self.dict = {}

    def get(self, rank_list: List[int], backend: str = 'nccl'):
        """Reuse Pytorch ProcessGroup when such a group is initialized
        """
        rank_tuple = tuple(rank_list)
        # we need to convert the passed list to a tuple
        # since List is unhashable
        pg_key = (backend, rank_tuple)

        if pg_key not in self.dict:

            self.logger = get_dist_logger('ProcessGroup')
            self.logger.info(f'NCCL initialize ProcessGroup on {rank_list}', ranks=[0])
            self.dict[pg_key] = torch.distributed.new_group(ranks=rank_list, backend=backend)
        return self.dict[pg_key]


PYTORCHPGDICT_ = PyTorchProcessGroupDict()


class ProcessGroup:
    """
    Process Group contains group partition for Tensor Parallel and Data Parallel.
    NOTE, the ProcessGroup must be used after torch.distributed.initialize()
    args:
        rank: the global rank of the current process.
        ranks: List[int], a list of rank id belongings to this process group.
        backend: str, the backend of the process group.
        tp_degree: Optional[int], tensor parallelism degree, default None means 1
        dp_degree: Optional[int], data parallelism degree, default None means len(ranks)
    """

    def __init__(self,
                 rank: Optional[int] = None,
                 ranks: Optional[List[int]] = None,
                 tp_degree: Optional[int] = None,
                 dp_degree: Optional[int] = None) -> None:
        if not torch.distributed.is_initialized():
            self.is_init = False
            return

        assert torch.distributed.is_initialized(), f"ProcessGroup must be used after distributed initialized"
        if rank is None:
            self._rank = torch.distributed.get_rank()
        else:
            self._rank = rank

        if ranks is None:
            self._rank_list = list(range(torch.distributed.get_world_size()))
        else:
            self._rank_list = ranks
            self._rank_list.sort()    # ensure that the list is in order

        self._world_size = len(self._rank_list)

        if dp_degree is None and tp_degree is None:
            self._dp_degree = self._world_size
            self._tp_degree = 1
        elif dp_degree and not tp_degree:
            self._dp_degree = dp_degree
            assert self._world_size % self._dp_degree == 0, f"DP degree {dp_degree} should be divisible by {self._world_size} hen DP degree is None"
            self._tp_degree = self._world_size // dp_degree
        elif not dp_degree and tp_degree:
            self._tp_degree = tp_degree
            assert self._world_size % self._tp_degree == 0, f"TP degree {tp_degree} should be divisible by {self._world_size} when DP degree is None"
            self._dp_degree = self._world_size // tp_degree
        else:
            self._dp_degree = dp_degree
            self._tp_degree = tp_degree
            assert self._dp_degree * self._tp_degree == self._world_size, \
                f"the world size {self._world_size} should equals to the product of DP degree {self._dp_degree}" \
                f"and TP degree {self._tp_degree}"

        self._tp_rank_list = None
        self._dp_rank_list = None

        for i in range(self._dp_degree):
            i_tp_list = [self._rank_list[i * self._tp_degree + j] for j in range(self._tp_degree)]
            PYTORCHPGDICT_.get(i_tp_list, 'nccl')
            if self._rank in i_tp_list:
                self._tp_rank_list = i_tp_list

        for j in range(self._tp_degree):
            j_dp_list = [self._rank_list[i * self._tp_degree + j] for i in range(self._dp_degree)]
            PYTORCHPGDICT_.get(j_dp_list, 'nccl')
            if self._rank in j_dp_list:
                self._dp_rank_list = j_dp_list

        self._has_cpu_groups = False
        self.is_init = True

    def set_cpu_groups(self):
        if self.has_cpu_groups:
            return

        for i in range(self._dp_degree):
            i_tp_list = [self._rank_list[i * self._tp_degree + j] for j in range(self._tp_degree)]
            PYTORCHPGDICT_.get(i_tp_list, 'gloo')

        for j in range(self._tp_degree):
            j_dp_list = [self._rank_list[i * self._tp_degree + j] for i in range(self._dp_degree)]
            PYTORCHPGDICT_.get(j_dp_list, 'gloo')

        self._has_cpu_groups = True

    @property
    def has_cpu_groups(self):
        return self._has_cpu_groups

    def __repr__(self):
        if self.is_init:
            return "ProcessGroup:\n\tRank: {}, World size: {}, DP degree: {}, TP degree: {}\n\tRanks in group: {}".\
                format(self._rank, self._world_size, self._dp_degree, self._tp_degree, self._rank_list)
        else:
            return "ProcessGroup not initialized"

    def __eq__(self, obj: 'ProcessGroup') -> bool:
        if not isinstance(obj, ProcessGroup):
            return False
        if self._rank != obj._rank:
            return False
        if self._rank_list != obj._rank_list:
            return False
        if self._tp_rank_list != obj._tp_rank_list:
            return False
        if self._dp_rank_list != obj._dp_rank_list:
            return False
        if self._tp_degree != obj._tp_degree:
            return False
        if self._dp_degree != obj._dp_degree:
            return False
        return True

    def rank(self):
        return self._rank

    def ranks_in_group(self):
        return self._rank_list

    def world_size(self):
        return self._world_size

    def tp_rank_list(self):
        return self._tp_rank_list

    def dp_rank_list(self):
        return self._dp_rank_list

    def tp_local_rank(self):
        return self._rank % self._tp_degree

    def dp_local_rank(self):
        return self._rank // self._tp_degree

    def dp_world_size(self):
        return len(self._dp_rank_list)

    def tp_world_size(self):
        return len(self._tp_rank_list)

    def dp_process_group(self):
        # return self._dp_process_group
        return PYTORCHPGDICT_.get(self._dp_rank_list, 'nccl')

    def tp_process_group(self):
        # return self._tp_process_group
        return PYTORCHPGDICT_.get(self._tp_rank_list, 'nccl')

    def cpu_dp_process_group(self):
        assert self._has_cpu_groups
        return PYTORCHPGDICT_.get(self._dp_rank_list, 'gloo')

    def cpu_tp_process_group(self):
        assert self._has_cpu_groups
        return PYTORCHPGDICT_.get(self._tp_rank_list, 'gloo')

    def get_ranks_in_dp(self):
        return self._dp_rank_list

    def get_ranks_in_tp(self):
        return self._tp_rank_list
