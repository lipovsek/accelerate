# Copyright 2021 The HuggingFace Team. All rights reserved.
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

import importlib
import math
from contextlib import suppress
from typing import Callable, Optional, Union

import torch
from packaging import version
from torch.utils.data import BatchSampler, DataLoader, IterableDataset, RandomSampler

from .logging import get_logger
from .state import DistributedType, GradientState, PartialState, is_torch_xla_available
from .utils import (
    RNGType,
    broadcast,
    broadcast_object_list,
    compare_versions,
    concatenate,
    find_batch_size,
    get_data_structure,
    initialize_tensors,
    is_datasets_available,
    is_torch_version,
    is_torchdata_stateful_dataloader_available,
    send_to_device,
    slice_tensors,
    synchronize_rng_states,
)


logger = get_logger(__name__)

# kwargs of the DataLoader in min version 2.0
_PYTORCH_DATALOADER_KWARGS = {
    "batch_size": 1,
    "shuffle": False,
    "sampler": None,
    "batch_sampler": None,
    "num_workers": 0,
    "collate_fn": None,
    "pin_memory": False,
    "drop_last": False,
    "timeout": 0,
    "worker_init_fn": None,
    "multiprocessing_context": None,
    "generator": None,
    "prefetch_factor": 2,
    "persistent_workers": False,
    "pin_memory_device": "",
}

# kwargs added after by version
_PYTORCH_DATALOADER_ADDITIONAL_KWARGS = {"2.6.0": {"in_order": True}}

for v, additional_kwargs in _PYTORCH_DATALOADER_ADDITIONAL_KWARGS.items():
    if is_torch_version(">=", v):
        _PYTORCH_DATALOADER_KWARGS.update(additional_kwargs)


class SeedableRandomSampler(RandomSampler):
    """
    Same as a random sampler, except that in `__iter__` a seed can be used.

    Needed specifically in distributed cases, when the random generator for each GPU needs to start from the same seed
    and be fully reproducable on multiple iterations.

    If a custom `generator` is passed, it will rely on its initial seed as well as the current iteration it is on
    (stored in `self.epoch`).
    """

    def __init__(self, *args, **kwargs):
        data_seed = kwargs.pop("data_seed", None)
        super().__init__(*args, **kwargs)

        self.initial_seed = data_seed if data_seed is not None else torch.random.initial_seed()
        self.epoch = 0

    def __iter__(self):
        if self.generator is None:
            self.generator = torch.Generator(
                device=torch.get_default_device() if hasattr(torch, "get_default_device") else "cpu"
            )
            self.generator.manual_seed(self.initial_seed)

        # Allow `self.epoch` to modify the seed of the generator
        seed = self.epoch + self.initial_seed
        # print("Setting seed at epoch", self.epoch, seed)
        self.generator.manual_seed(seed)
        yield from super().__iter__()
        self.set_epoch(self.epoch + 1)

    def set_epoch(self, epoch: int):
        "Sets the current iteration of the sampler."
        self.epoch = epoch


class BatchSamplerShard(BatchSampler):
    """
    Wraps a PyTorch `BatchSampler` to generate batches for one of the processes only. Instances of this class will
    always yield a number of batches that is a round multiple of `num_processes` and that all have the same size.
    Depending on the value of the `drop_last` attribute of the batch sampler passed, it will either stop the iteration
    at the first batch that would be too small / not present on all processes or loop with indices from the beginning.

    Args:
        batch_sampler (`torch.utils.data.sampler.BatchSampler`):
            The batch sampler to split in several shards.
        num_processes (`int`, *optional*, defaults to 1):
            The number of processes running concurrently.
        process_index (`int`, *optional*, defaults to 0):
            The index of the current process.
        split_batches (`bool`, *optional*, defaults to `False`):
            Whether the shards should be created by splitting a batch to give a piece of it on each process, or by
            yielding different full batches on each process.

            On two processes with a sampler of `[[0, 1, 2, 3], [4, 5, 6, 7]]`, this will result in:

            - the sampler on process 0 to yield `[0, 1, 2, 3]` and the sampler on process 1 to yield `[4, 5, 6, 7]` if
              this argument is set to `False`.
            - the sampler on process 0 to yield `[0, 1]` then `[4, 5]` and the sampler on process 1 to yield `[2, 3]`
              then `[6, 7]` if this argument is set to `True`.
        even_batches (`bool`, *optional*, defaults to `True`):
            Whether or not to loop back at the beginning of the sampler when the number of samples is not a round
            multiple of (original batch size / number of processes).

    <Tip warning={true}>

    `BatchSampler`s with varying batch sizes are not enabled by default. To enable this behaviour, set `even_batches`
    equal to `False`

    </Tip>"""

    def __init__(
        self,
        batch_sampler: BatchSampler,
        num_processes: int = 1,
        process_index: int = 0,
        split_batches: bool = False,
        even_batches: bool = True,
    ):
        if split_batches and batch_sampler.batch_size % num_processes != 0:
            raise ValueError(
                f"To use `BatchSamplerShard` in `split_batches` mode, the batch size ({batch_sampler.batch_size}) "
                f"needs to be a round multiple of the number of processes ({num_processes})."
            )
        self.batch_sampler = batch_sampler
        self.num_processes = num_processes
        self.process_index = process_index
        self.split_batches = split_batches
        self.even_batches = even_batches
        self.batch_size = getattr(batch_sampler, "batch_size", None)
        self.drop_last = getattr(batch_sampler, "drop_last", False)
        if self.batch_size is None and self.even_batches:
            raise ValueError(
                "You need to use `even_batches=False` when the batch sampler has no batch size. If you "
                "are not calling this method directly, set `accelerator.even_batches=False` instead."
            )

    @property
    def total_length(self):
        return len(self.batch_sampler)

    def __len__(self):
        if self.split_batches:
            # Split batches does not change the length of the batch sampler
            return len(self.batch_sampler)
        if len(self.batch_sampler) % self.num_processes == 0:
            # If the length is a round multiple of the number of processes, it's easy.
            return len(self.batch_sampler) // self.num_processes
        length = len(self.batch_sampler) // self.num_processes
        if self.drop_last:
            # Same if we drop the remainder.
            return length
        elif self.even_batches:
            # When we even batches we always get +1
            return length + 1
        else:
            # Otherwise it depends on the process index.
            return length + 1 if self.process_index < len(self.batch_sampler) % self.num_processes else length

    def __iter__(self):
        return self._iter_with_split() if self.split_batches else self._iter_with_no_split()

    def _iter_with_split(self):
        initial_data = []
        batch_length = self.batch_sampler.batch_size // self.num_processes
        for idx, batch in enumerate(self.batch_sampler):
            if idx == 0:
                initial_data = batch
            if len(batch) == self.batch_size:
                # If the batch is full, we yield the part of it this process is responsible of.
                yield batch[batch_length * self.process_index : batch_length * (self.process_index + 1)]

        # If drop_last is True of the last batch was full, iteration is over, otherwise...
        if not self.drop_last and len(initial_data) > 0 and len(batch) < self.batch_size:
            if not self.even_batches:
                if len(batch) > batch_length * self.process_index:
                    yield batch[batch_length * self.process_index : batch_length * (self.process_index + 1)]
            else:
                # For degenerate cases where the dataset has less than num_process * batch_size samples
                while len(initial_data) < self.batch_size:
                    initial_data += initial_data
                batch = batch + initial_data
                yield batch[batch_length * self.process_index : batch_length * (self.process_index + 1)]

    def _iter_with_no_split(self):
        initial_data = []
        batch_to_yield = []
        for idx, batch in enumerate(self.batch_sampler):
            # We gather the initial indices in case we need to circle back at the end.
            if not self.drop_last and idx < self.num_processes:
                initial_data += batch
            # We identify the batch to yield but wait until we ar sure every process gets a full batch before actually
            # yielding it.
            if idx % self.num_processes == self.process_index:
                batch_to_yield = batch
            if idx % self.num_processes == self.num_processes - 1 and (
                self.batch_size is None or len(batch) == self.batch_size
            ):
                yield batch_to_yield
                batch_to_yield = []

        # If drop_last is True, iteration is over, otherwise...
        if not self.drop_last and len(initial_data) > 0:
            if not self.even_batches:
                if len(batch_to_yield) > 0:
                    yield batch_to_yield
            else:
                # ... we yield the complete batch we had saved before if it has the proper length
                if len(batch_to_yield) == self.batch_size:
                    yield batch_to_yield

                # For degenerate cases where the dataset has less than num_process * batch_size samples
                while len(initial_data) < self.num_processes * self.batch_size:
                    initial_data += initial_data

                # If the last batch seen was of the proper size, it has been yielded by its process so we move to the next
                if len(batch) == self.batch_size:
                    batch = []
                    idx += 1

                # Make sure we yield a multiple of self.num_processes batches
                cycle_index = 0
                while idx % self.num_processes != 0 or len(batch) > 0:
                    end_index = cycle_index + self.batch_size - len(batch)
                    batch += initial_data[cycle_index:end_index]
                    if idx % self.num_processes == self.process_index:
                        yield batch
                    cycle_index = end_index
                    batch = []
                    idx += 1


class IterableDatasetShard(IterableDataset):
    """
    Wraps a PyTorch `IterableDataset` to generate samples for one of the processes only. Instances of this class will
    always yield a number of samples that is a round multiple of the actual batch size (depending of the value of
    `split_batches`, this is either `batch_size` or `batch_size x num_processes`). Depending on the value of the
    `drop_last` attribute of the batch sampler passed, it will either stop the iteration at the first batch that would
    be too small or loop with indices from the beginning.

    Args:
        dataset (`torch.utils.data.dataset.IterableDataset`):
            The batch sampler to split in several shards.
        batch_size (`int`, *optional*, defaults to 1):
            The size of the batches per shard (if `split_batches=False`) or the size of the batches (if
            `split_batches=True`).
        drop_last (`bool`, *optional*, defaults to `False`):
            Whether or not to drop the last incomplete batch or complete the last batches by using the samples from the
            beginning.
        num_processes (`int`, *optional*, defaults to 1):
            The number of processes running concurrently.
        process_index (`int`, *optional*, defaults to 0):
            The index of the current process.
        split_batches (`bool`, *optional*, defaults to `False`):
            Whether the shards should be created by splitting a batch to give a piece of it on each process, or by
            yielding different full batches on each process.

            On two processes with an iterable dataset yielding of `[0, 1, 2, 3, 4, 5, 6, 7]`, this will result in:

            - the shard on process 0 to yield `[0, 1, 2, 3]` and the shard on process 1 to yield `[4, 5, 6, 7]` if this
              argument is set to `False`.
            - the shard on process 0 to yield `[0, 1, 4, 5]` and the sampler on process 1 to yield `[2, 3, 6, 7]` if
              this argument is set to `True`.
    """

    def __init__(
        self,
        dataset: IterableDataset,
        batch_size: int = 1,
        drop_last: bool = False,
        num_processes: int = 1,
        process_index: int = 0,
        split_batches: bool = False,
    ):
        if split_batches and batch_size > 1 and batch_size % num_processes != 0:
            raise ValueError(
                f"To use `IterableDatasetShard` in `split_batches` mode, the batch size ({batch_size}) "
                f"needs to be a round multiple of the number of processes ({num_processes})."
            )
        self.dataset = dataset
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.num_processes = num_processes
        self.process_index = process_index
        self.split_batches = split_batches

    def set_epoch(self, epoch):
        self.epoch = epoch
        if hasattr(self.dataset, "set_epoch"):
            self.dataset.set_epoch(epoch)

    def __len__(self):
        # We will just raise the downstream error if the underlying dataset is not sized
        if self.drop_last:
            return (len(self.dataset) // (self.batch_size * self.num_processes)) * self.batch_size
        else:
            return math.ceil(len(self.dataset) / (self.batch_size * self.num_processes)) * self.batch_size

    def __iter__(self):
        if (
            not hasattr(self.dataset, "set_epoch")
            and hasattr(self.dataset, "generator")
            and isinstance(self.dataset.generator, torch.Generator)
        ):
            self.dataset.generator.manual_seed(self.epoch)
        real_batch_size = self.batch_size if self.split_batches else (self.batch_size * self.num_processes)
        process_batch_size = (self.batch_size // self.num_processes) if self.split_batches else self.batch_size
        process_slice = range(self.process_index * process_batch_size, (self.process_index + 1) * process_batch_size)

        first_batch = None
        current_batch = []
        for element in self.dataset:
            current_batch.append(element)
            # Wait to have a full batch before yielding elements.
            if len(current_batch) == real_batch_size:
                for i in process_slice:
                    yield current_batch[i]
                if first_batch is None:
                    first_batch = current_batch.copy()
                current_batch = []

        # Finished if drop_last is True, otherwise complete the last batch with elements from the beginning.
        if not self.drop_last and len(current_batch) > 0:
            if first_batch is None:
                first_batch = current_batch.copy()
            while len(current_batch) < real_batch_size:
                current_batch += first_batch
            for i in process_slice:
                yield current_batch[i]


class DataLoaderStateMixin:
    """
    Mixin class that adds a state to a `DataLoader` to keep track of the status inside the dataloader such as at the
    end of the iteration, the number of items in the dataset in the last batch relative to the batch size, and other
    useful information that might be needed.

    **Available attributes:**

        - **end_of_dataloader** (`bool`) -- Whether at the last iteration or batch
        - **remainder** (`int`) -- The number of items that are remaining in the last batch, relative to the total
          batch size

    <Tip warning={true}>

        Inheriters of this class should ensure that the class creates a `GradientState()` instance, stored in
        `self.gradient_state`.

    </Tip>

    """

    def __init_subclass__(cls, **kwargs):
        cls.end_of_dataloader = False
        cls.remainder = -1

    def reset(self):
        self.end_of_dataloader = False
        self.remainder = -1

    def begin(self):
        "Prepares the gradient state for the current dataloader"
        self.reset()
        with suppress(Exception):
            if not self._drop_last:
                length = getattr(self.dataset, "total_dataset_length", len(self.dataset))
                self.remainder = length % self.total_batch_size
        self.gradient_state._add_dataloader(self)

    def end(self):
        "Cleans up the gradient state after exiting the dataloader"
        self.gradient_state._remove_dataloader(self)


class DataLoaderAdapter:
    """
    A class which wraps around a PyTorch `DataLoader` (or variants of it) to be used with the `Accelerator`. For
    compatability reasons, this class inherits from the class it wraps around, so it can be used as a drop-in.
    """

    def __init__(self, dataset, use_stateful_dataloader=False, batch_sampler=None, **kwargs):
        self.use_stateful_dataloader = use_stateful_dataloader
        if is_torchdata_stateful_dataloader_available():
            from torchdata.stateful_dataloader import StatefulDataLoader

        if use_stateful_dataloader and not is_torchdata_stateful_dataloader_available():
            raise ImportError(
                "StatefulDataLoader is not available. Please install torchdata version 0.8.0 or higher to use it."
            )
        if use_stateful_dataloader:
            torchdata_version = version.parse(importlib.metadata.version("torchdata"))
            if (
                "in_order" in kwargs
                and compare_versions(torchdata_version, "<", "0.11")
                and is_torch_version(">=", "2.6.0")
            ):
                kwargs.pop("in_order")
            self.base_dataloader = StatefulDataLoader(dataset, batch_sampler=batch_sampler, **kwargs)
        else:
            self.base_dataloader = DataLoader(dataset, batch_sampler=batch_sampler, **kwargs)

        if hasattr(self.base_dataloader, "state_dict"):
            self.dl_state_dict = self.base_dataloader.state_dict()

    def __getattr__(self, name):
        # Avoid infinite recursion if we try to access a nonexistent base_dataloader attribute.
        if name == "base_dataloader":
            raise AttributeError()
        # Delegate attribute access to the internal dataloader
        return getattr(self.base_dataloader, name)

    def state_dict(self):
        return self.dl_state_dict

    def load_state_dict(self, state_dict):
        self.base_dataloader.load_state_dict(state_dict)

    @property
    def __class__(self):
        """
        In order to maintain backwards compatability with other code, we need to ensure `isinstance(obj, DataLoader)`
        returs true. This is because some downstream code assumes that the `DataLoader` is the base class of the
        object.
        """
        return self.base_dataloader.__class__

    def __len__(self):
        return len(self.base_dataloader)

    def adjust_state_dict_for_prefetch(self):
        """
        Adjusts the state dict for prefetching. Natively, this will adjust all of the iters yielded keys in
        `self.dl_state_dict` by a factor of `num_processes - 1`, however if a custom correction is needed, this can be
        overridden.

        This should modify `self.dl_state_dict` directly
        """
        # The state dict will be off by a factor of `n-1` batch too many during DDP,
        # so we need to adjust it here
        if PartialState().distributed_type != DistributedType.NO:
            factor = PartialState().num_processes - 1
            if self.dl_state_dict["_sampler_iter_yielded"] > 0:
                self.dl_state_dict["_sampler_iter_yielded"] -= factor
            if self.dl_state_dict["_num_yielded"] > 0:
                self.dl_state_dict["_num_yielded"] -= factor
            if self.dl_state_dict["_index_sampler_state"] is not None:
                if (
                    "samples_yielded" in self.dl_state_dict["_index_sampler_state"]
                    and self.dl_state_dict["_index_sampler_state"]["samples_yielded"] > 0
                ):
                    self.dl_state_dict["_index_sampler_state"]["samples_yielded"] -= self.batch_size * factor

    def _update_state_dict(self):
        # The state_dict of the underlying base_dataloader may be ahead of what is currently being yielded.
        # E.g. the implementation of DataLoaderShard involves having an underlying iterator 1 element ahead of
        # what it wants to yield.
        #
        # _update_state_dict is called to snapshot the state_dict that would properly recover the DataLoaderAdapter.
        if hasattr(self.base_dataloader, "state_dict"):
            self.dl_state_dict = self.base_dataloader.state_dict()
            # Potentially modify the state_dict to adjust for prefetching
            self.adjust_state_dict_for_prefetch()
            # Then tag if we are at the end of the dataloader
            self.dl_state_dict["_iterator_finished"] = self.end_of_dataloader


class DataLoaderShard(DataLoaderAdapter, DataLoaderStateMixin):
    """
    Subclass of `DataLoaderAdapter` that will deal with device placement and current distributed setup.

    Args:
        dataset (`torch.utils.data.dataset.Dataset`):
            The dataset to use to build this dataloader.
        device (`torch.device`, *optional*):
            If passed, the device to put all batches on.
        rng_types (list of `str` or [`~utils.RNGType`]):
            The list of random number generators to synchronize at the beginning of each iteration. Should be one or
            several of:

            - `"torch"`: the base torch random number generator
            - `"cuda"`: the CUDA random number generator (GPU only)
            - `"xla"`: the XLA random number generator (TPU only)
            - `"generator"`: an optional `torch.Generator`
        synchronized_generator (`torch.Generator`, *optional*):
            A random number generator to keep synchronized across processes.
        skip_batches (`int`, *optional*, defaults to 0):
            The number of batches to skip at the beginning.
        use_stateful_dataloader (`bool`, *optional*, defaults to `False`):
            Whether to have this class adapt `StatefulDataLoader` from `torchdata` instead of the regular `DataLoader`.
        **kwargs (additional keyword arguments, *optional*):
            All other keyword arguments to pass to the regular `DataLoader` initialization.

    **Available attributes:**

        - **total_batch_size** (`int`) -- Total batch size of the dataloader across all processes.
            Equal to the original batch size when `split_batches=True`; otherwise the original batch size * the total
            number of processes

        - **total_dataset_length** (`int`) -- Total length of the inner dataset across all processes.
    """

    def __init__(
        self,
        dataset,
        device=None,
        rng_types=None,
        synchronized_generator=None,
        skip_batches=0,
        use_stateful_dataloader=False,
        _drop_last: bool = False,
        _non_blocking: bool = False,
        torch_device_mesh=None,
        **kwargs,
    ):
        super().__init__(dataset, use_stateful_dataloader=use_stateful_dataloader, **kwargs)
        self.device = device
        self.rng_types = rng_types
        self.synchronized_generator = synchronized_generator
        self.skip_batches = skip_batches
        self.gradient_state = GradientState()
        self._drop_last = _drop_last
        self._non_blocking = _non_blocking
        self.iteration = 0

    def __iter__(self):
        if self.rng_types is not None:
            synchronize_rng_states(self.rng_types, self.synchronized_generator)
        self.begin()

        self.set_epoch(self.iteration)
        dataloader_iter = self.base_dataloader.__iter__()
        # We iterate one batch ahead to check when we are at the end
        try:
            current_batch = next(dataloader_iter)
        except StopIteration:
            self.end()
            return

        batch_index = 0
        while True:
            try:
                # But we still move it to the device so it is done before `StopIteration` is reached
                if self.device is not None:
                    current_batch = send_to_device(current_batch, self.device, non_blocking=self._non_blocking)
                self._update_state_dict()
                next_batch = next(dataloader_iter)
                if batch_index >= self.skip_batches:
                    yield current_batch
                batch_index += 1
                current_batch = next_batch
            except StopIteration:
                self.end_of_dataloader = True
                self._update_state_dict()
                if batch_index >= self.skip_batches:
                    yield current_batch
                break

        self.iteration += 1
        self.end()

    def __reduce__(self):
        """
        Define the `__reduce__` method to ensure a `DataLoaderShard` can be pickled and unpickled. This needs to be
        explicitly defined since default pickling behavior is broken by `DataLoaderAdapter` messing with its
        `__class__` member.
        """
        args = super().__reduce__()
        return (DataLoaderShard, *args[1:])

    def set_epoch(self, epoch: int):
        # In case it is manually passed in, the user can set it to what they like
        if self.iteration != epoch:
            self.iteration = epoch
        if hasattr(self.batch_sampler, "set_epoch"):
            self.batch_sampler.set_epoch(epoch)
        if hasattr(self.batch_sampler, "sampler") and hasattr(self.batch_sampler.sampler, "set_epoch"):
            self.batch_sampler.sampler.set_epoch(epoch)
        if (
            hasattr(self.batch_sampler, "batch_sampler")
            and hasattr(self.batch_sampler.batch_sampler, "sampler")
            and hasattr(self.batch_sampler.batch_sampler.sampler, "set_epoch")
        ):
            self.batch_sampler.batch_sampler.sampler.set_epoch(epoch)
        # We support if a custom `Dataset` implementation has `set_epoch`
        # or in general HF datasets `Datasets`
        elif hasattr(self.dataset, "set_epoch"):
            self.dataset.set_epoch(epoch)

    @property
    def total_batch_size(self):
        batch_sampler = self.sampler if isinstance(self.sampler, BatchSampler) else self.batch_sampler
        return (
            batch_sampler.batch_size
            if getattr(batch_sampler, "split_batches", False)
            else (batch_sampler.batch_size * getattr(batch_sampler, "num_processes", 1))
        )

    @property
    def total_dataset_length(self):
        if hasattr(self.dataset, "total_length"):
            return self.dataset.total_length
        else:
            return len(self.dataset)

    def get_sampler(self):
        return get_sampler(self)

    def set_sampler(self, sampler):
        sampler_is_batch_sampler = isinstance(self.sampler, BatchSampler)
        if sampler_is_batch_sampler:
            self.sampler.sampler = sampler
        else:
            self.batch_sampler.sampler = sampler
            if hasattr(self.batch_sampler, "batch_sampler"):
                self.batch_sampler.batch_sampler.sampler = sampler


if is_torch_xla_available():
    import torch_xla.distributed.parallel_loader as xpl

    class MpDeviceLoaderWrapper(xpl.MpDeviceLoader):
        """
        Wrapper for the xpl.MpDeviceLoader class that knows the total batch size.

        XLA preloading threads will all call DataLoaderShard's __iter__(). Remove rng_types from DataLoaderShard to
        prevent it from using the XLA device in the preloading threads, and synchronize the RNG once from the main
        thread only.

        **Available attributes:**

        - **total_batch_size** (`int`) -- Total batch size of the dataloader across all processes.
            Equal to the original batch size when `split_batches=True`; otherwise the original batch size * the total
            number of processes

        - **total_dataset_length** (`int`) -- Total length of the inner dataset across all processes.
        """

        def __init__(self, dataloader: DataLoaderShard, device: torch.device):
            super().__init__(dataloader, device)
            self._rng_types = self._loader.rng_types
            self._loader.rng_types = None
            self.device = device

        def __iter__(self):
            if self._rng_types is not None:
                synchronize_rng_states(self._rng_types, self._loader.synchronized_generator)

            return super().__iter__()

        def set_epoch(self, epoch: int):
            if hasattr(self.dataloader, "set_epoch"):
                self.dataloader.set_epoch(epoch)

        @property
        def total_batch_size(self):
            return self._loader.total_batch_size

        @property
        def total_dataset_length(self):
            return self._loader.total_dataset_length

        @property
        def batch_sampler(self):
            return self._loader.batch_sampler

        @property
        def dataloader(self):
            return self._loader


class DataLoaderDispatcher(DataLoaderAdapter, DataLoaderStateMixin):
    """
    Subclass of `DataLoaderAdapter` that will iterate and preprocess on process 0 only, then dispatch on each process
    their part of the batch.

    Args:
        split_batches (`bool`, *optional*, defaults to `False`):
            Whether the resulting `DataLoader` should split the batches of the original data loader across devices or
            yield full batches (in which case it will yield batches starting at the `process_index`-th and advancing of
            `num_processes` batches at each iteration). Another way to see this is that the observed batch size will be
            the same as the initial `dataloader` if this option is set to `True`, the batch size of the initial
            `dataloader` multiplied by `num_processes` otherwise. Setting this option to `True` requires that the batch
            size of the `dataloader` is a round multiple of `batch_size`.
        skip_batches (`int`, *optional*, defaults to 0):
            The number of batches to skip at the beginning of an iteration.
        use_stateful_dataloader (`bool`, *optional*, defaults to `False`):
            Whether to have this class adapt `StatefulDataLoader` from `torchdata` instead of the regular `DataLoader`.

    **Available attributes:**

        - **total_batch_size** (`int`) -- Total batch size of the dataloader across all processes.
            Equal to the original batch size when `split_batches=True`; otherwise the original batch size * the total
            number of processes

        - **total_dataset_length** (`int`) -- Total length of the inner dataset across all processes.
    """

    def __init__(
        self,
        dataset,
        split_batches: bool = False,
        skip_batches=0,
        use_stateful_dataloader=False,
        _drop_last: bool = False,
        _non_blocking: bool = False,
        slice_fn=None,
        torch_device_mesh=None,
        **kwargs,
    ):
        shuffle = False
        from torch.utils.data.datapipes.iter.combinatorics import ShufflerIterDataPipe

        # We need to save the shuffling state of the DataPipe
        if isinstance(dataset, ShufflerIterDataPipe):
            shuffle = dataset._shuffle_enabled
        super().__init__(dataset, use_stateful_dataloader=use_stateful_dataloader, **kwargs)
        self.split_batches = split_batches
        if shuffle:
            torch.utils.data.graph_settings.apply_shuffle_settings(dataset, shuffle=shuffle)

        self.gradient_state = GradientState()
        self.state = PartialState()
        self._drop_last = _drop_last
        self._non_blocking = _non_blocking
        self.skip_batches = skip_batches
        self.torch_device_mesh = torch_device_mesh

        self.slice_fn = slice_tensors if slice_fn is None else slice_fn
        self.iteration = 0

        # if a device mesh is provided extract each dimension (dp, fsdp, tp)
        # device mesh may hold any number of dimensions, however,
        # below code is for targetted support for dp, fsdp and tp

        # device mesh will be used only if there is tp involved
        # or any multi-dimensional parallelism involving tp
        # (dp, tp) (fsdp, tp) (dp, fsdp, tp)
        # otherwise the default behavour not using device mesh should be sufficient
        # since multi dimensional parallelism devoid of tp would anyway need
        # different batches for each process irrespective of dp or fsdp
        self.submesh_tp = None
        self.submesh_dp = None
        self.submesh_fsdp = None
        if self.torch_device_mesh and "tp" in self.torch_device_mesh.mesh_dim_names:
            self.submesh_tp = self.torch_device_mesh["tp"]
            if "dp" in self.torch_device_mesh.mesh_dim_names:
                self.submesh_dp = self.torch_device_mesh["dp"]
            if "fsdp" in self.torch_device_mesh.mesh_dim_names:
                self.submesh_fsdp = self.torch_device_mesh["fsdp"]
        if self.submesh_tp and (self.submesh_dp or self.submesh_fsdp):
            raise ValueError("TP + (DP/FSDP) is not yet supported in dispatch mode")

    def _fetch_batches(self, iterator):
        batches, batch = None, None
        # On process 0, we gather the batch to dispatch.
        if self.state.process_index == 0:
            # Procedure to support TP only is simpler
            # since we want to dispatch the same batch of samples across all ranks
            # this removes complexity of handling multiple tp rank groups when TP + DP
            # combination is involved.

            try:
                # for TP case avoid using split_batches
                # since it would mean that the dataloader should be spilling out
                # duplicates of batches.
                if self.split_batches:
                    # One batch of the main iterator is dispatched and split.
                    if self.submesh_tp:
                        logger.warning(
                            "Use of split_batches for TP would need the dataloader to produce duplicate batches,"
                            "otherwise, use dispatch_batches=True instead."
                        )
                    self._update_state_dict()
                    batch = next(iterator)
                else:
                    # num_processes batches of the main iterator are concatenated then dispatched and split.
                    # We add the batches one by one so we have the remainder available when drop_last=False.
                    batches = []
                    if self.submesh_tp:
                        # when tp, extract single batch and then replicate
                        self._update_state_dict()
                        batch = next(iterator)
                        batches = [batch] * self.state.num_processes
                    else:
                        for _ in range(self.state.num_processes):
                            self._update_state_dict()
                            batches.append(next(iterator))
                    try:
                        batch = concatenate(batches, dim=0)
                    except RuntimeError as e:
                        raise RuntimeError(
                            "You can't use batches of different size with `dispatch_batches=True` or when using an `IterableDataset`."
                            "either pass `dispatch_batches=False` and have each process fetch its own batch "
                            " or pass `split_batches=True`. By doing so, the main process will fetch a full batch and "
                            "slice it into `num_processes` batches for each process."
                        ) from e
                # In both cases, we need to get the structure of the batch that we will broadcast on other
                # processes to initialize the tensors with the right shape.
                # data_structure, stop_iteration
                batch_info = [get_data_structure(batch), False]
            except StopIteration:
                batch_info = [None, True]
        else:
            batch_info = [None, self._stop_iteration]
        # This is inplace, so after this instruction, every process has the same `batch_info` as process 0.
        broadcast_object_list(batch_info)
        self._stop_iteration = batch_info[1]
        if self._stop_iteration:
            # If drop_last is False and split_batches is False, we may have a remainder to take care of.
            if not self.split_batches and not self._drop_last:
                if self.state.process_index == 0 and len(batches) > 0:
                    batch = concatenate(batches, dim=0)
                    batch_info = [get_data_structure(batch), False]
                else:
                    batch_info = [None, True]
                broadcast_object_list(batch_info)
        return batch, batch_info

    def __iter__(self):
        self.begin()
        self.set_epoch(self.iteration)
        main_iterator = None
        if is_torch_version(">=", "2.0.1"):
            # NOTE PyTorch DataLoader adds forward compatibilities for DataPipes, which broadcasts
            # shared seed to all dist processes. Thus, we need to create iterator for all dist processes.
            # But, we only iterate through the DataLoader on process 0.
            main_iterator = self.base_dataloader.__iter__()
        elif self.state.process_index == 0:
            main_iterator = self.base_dataloader.__iter__()
        stop_iteration = False
        self._stop_iteration = False
        first_batch = None
        next_batch, next_batch_info = self._fetch_batches(main_iterator)
        batch_index = 0
        while not stop_iteration:
            batch, batch_info = next_batch, next_batch_info

            if self.state.process_index != 0:
                # Initialize tensors on other processes than process 0.
                batch = initialize_tensors(batch_info[0])
            batch = send_to_device(batch, self.state.device, non_blocking=self._non_blocking)
            # Broadcast the batch before splitting it.
            batch = broadcast(batch, from_process=0)

            if not self._drop_last and first_batch is None:
                # We keep at least num processes elements of the first batch to be able to complete the last batch
                first_batch = self.slice_fn(
                    batch,
                    slice(0, self.state.num_processes),
                    process_index=self.state.process_index,
                    num_processes=self.state.num_processes,
                )

            if batch is None:
                raise ValueError(
                    f"Batch does not contain any data (`{batch}`). At the end of all iterable data available before expected stop iteration."
                )

            observed_batch_size = find_batch_size(batch)
            batch_size = observed_batch_size // self.state.num_processes

            stop_iteration = self._stop_iteration
            if not stop_iteration:
                # We may still be at the end of the dataloader without knowing it yet: if there is nothing left in
                # the dataloader since the number of batches is a round multiple of the number of processes.
                next_batch, next_batch_info = self._fetch_batches(main_iterator)
                # next_batch_info[0] is None when there are no more batches, otherwise we still need to process them.
                if self._stop_iteration and next_batch_info[0] is None:
                    stop_iteration = True

            if not self._drop_last and stop_iteration and observed_batch_size % self.state.num_processes != 0:
                # If the last batch is not complete, let's add the first batch to it.
                batch = concatenate([batch, first_batch], dim=0)
                # Batch size computation above is wrong, it's off by 1 so we fix it.
                batch_size += 1

            data_slice = slice(self.state.process_index * batch_size, (self.state.process_index + 1) * batch_size)
            batch = self.slice_fn(
                batch,
                data_slice,
                process_index=self.state.process_index,
                num_processes=self.state.num_processes,
            )

            if stop_iteration:
                self.end_of_dataloader = True
                self._update_state_dict()
                self.remainder = observed_batch_size
            if batch_index >= self.skip_batches:
                yield batch
            batch_index += 1
        self.iteration += 1
        self.end()

    def set_epoch(self, epoch: int):
        # In case it is manually passed in, the user can set it to what they like
        if self.iteration != epoch:
            self.iteration = epoch
        if hasattr(self.batch_sampler, "sampler") and hasattr(self.batch_sampler.sampler, "set_epoch"):
            self.batch_sampler.sampler.set_epoch(epoch)
        elif hasattr(self.dataset, "set_epoch"):
            self.dataset.set_epoch(epoch)

    def __len__(self):
        whole_length = len(self.base_dataloader)
        if self.split_batches:
            return whole_length
        elif self._drop_last:
            return whole_length // self.state.num_processes
        else:
            return math.ceil(whole_length / self.state.num_processes)

    def __reduce__(self):
        """
        Define the `__reduce__` method to ensure a `DataLoaderDispatcher` can be pickled and unpickled. This needs to
        be explicitly defined since default pickling behavior is broken by `DataLoaderAdapter` messing with its
        `__class__` member.
        """
        args = super().__reduce__()
        return (DataLoaderDispatcher, *args[1:])

    @property
    def total_batch_size(self):
        return (
            self.dataset.batch_size if self.split_batches else (self.dataset.batch_size * self.dataset.num_processes)
        )

    @property
    def total_dataset_length(self):
        return len(self.dataset)

    def get_sampler(self):
        return get_sampler(self)

    def set_sampler(self, sampler):
        sampler_is_batch_sampler = isinstance(self.sampler, BatchSampler)
        if sampler_is_batch_sampler:
            self.sampler.sampler = sampler
        else:
            self.batch_sampler.sampler = sampler
            if hasattr(self.batch_sampler, "batch_sampler"):
                self.batch_sampler.batch_sampler.sampler = sampler


def get_sampler(dataloader):
    """
    Get the sampler associated to the dataloader

    Args:
        dataloader (`torch.utils.data.dataloader.DataLoader`):
            The data loader to split across several devices.
    Returns:
        `torch.utils.data.Sampler`: The sampler associated to the dataloader
    """
    sampler_is_batch_sampler = isinstance(dataloader.sampler, BatchSampler)
    if sampler_is_batch_sampler:
        sampler = getattr(dataloader.sampler, "sampler", None)
    else:
        sampler = getattr(dataloader.batch_sampler, "sampler", None)
    return sampler


def prepare_data_loader(
    dataloader: DataLoader,
    device: Optional[torch.device] = None,
    num_processes: Optional[int] = None,
    process_index: Optional[int] = None,
    split_batches: bool = False,
    put_on_device: bool = False,
    rng_types: Optional[list[Union[str, RNGType]]] = None,
    dispatch_batches: Optional[bool] = None,
    even_batches: bool = True,
    slice_fn_for_dispatch: Optional[Callable] = None,
    use_seedable_sampler: bool = False,
    data_seed: Optional[int] = None,
    non_blocking: bool = False,
    use_stateful_dataloader: bool = False,
    torch_device_mesh=None,
) -> DataLoader:
    """
    Wraps a PyTorch `DataLoader` to generate batches for one of the processes only.

    Depending on the value of the `drop_last` attribute of the `dataloader` passed, it will either stop the iteration
    at the first batch that would be too small / not present on all processes or loop with indices from the beginning.

    Args:
        dataloader (`torch.utils.data.dataloader.DataLoader`):
            The data loader to split across several devices.
        device (`torch.device`):
            The target device for the returned `DataLoader`.
        num_processes (`int`, *optional*):
            The number of processes running concurrently. Will default to the value given by [`~state.PartialState`].
        process_index (`int`, *optional*):
            The index of the current process. Will default to the value given by [`~state.PartialState`].
        split_batches (`bool`, *optional*, defaults to `False`):
            Whether the resulting `DataLoader` should split the batches of the original data loader across devices or
            yield full batches (in which case it will yield batches starting at the `process_index`-th and advancing of
            `num_processes` batches at each iteration).

            Another way to see this is that the observed batch size will be the same as the initial `dataloader` if
            this option is set to `True`, the batch size of the initial `dataloader` multiplied by `num_processes`
            otherwise.

            Setting this option to `True` requires that the batch size of the `dataloader` is a round multiple of
            `batch_size`.
        put_on_device (`bool`, *optional*, defaults to `False`):
            Whether or not to put the batches on `device` (only works if the batches are nested list, tuples or
            dictionaries of tensors).
        rng_types (list of `str` or [`~utils.RNGType`]):
            The list of random number generators to synchronize at the beginning of each iteration. Should be one or
            several of:

            - `"torch"`: the base torch random number generator
            - `"cuda"`: the CUDA random number generator (GPU only)
            - `"xla"`: the XLA random number generator (TPU only)
            - `"generator"`: the `torch.Generator` of the sampler (or batch sampler if there is no sampler in your
              dataloader) or of the iterable dataset (if it exists) if the underlying dataset is of that type.

        dispatch_batches (`bool`, *optional*):
            If set to `True`, the dataloader prepared is only iterated through on the main process and then the batches
            are split and broadcast to each process. Will default to `True` when the underlying dataset is an
            `IterableDataset`, `False` otherwise.
        even_batches (`bool`, *optional*, defaults to `True`):
            If set to `True`, in cases where the total batch size across all processes does not exactly divide the
            dataset, samples at the start of the dataset will be duplicated so the batch can be divided equally among
            all workers.
        slice_fn_for_dispatch (`Callable`, *optional*`):
            If passed, this function will be used to slice tensors across `num_processes`. Will default to
            [`~utils.slice_tensors`]. This argument is used only when `dispatch_batches` is set to `True` and will be
            ignored otherwise.
        use_seedable_sampler (`bool`, *optional*, defaults to `False`):
            Whether to use the [`~data_loader.SeedableRandomSampler`] instead of a `RandomSampler` for better
            reproducability. Comes at a cost of potentially different performances due to different shuffling
            algorithms but ensures results will be the *exact* same. Should be paired with `set_seed()` at every
            `self.set_epoch`
        data_seed (`int`, *optional*, defaults to `None`):
            The seed to use for the underlying generator when using `use_seedable_sampler`. If `None`, the generator
            will use the current default seed from torch.
        non_blocking (`bool`, *optional*, defaults to `False`):
            If set to `True`, dataloader will utilize non-blocking host-to-device transfers. If the dataloader has
            `pin_memory` set to `True`, this will help to increase overlap between data transfer and computations.
        use_stateful_dataloader (`bool`, *optional*, defaults to `False`):
            "If set to true, the dataloader prepared by the Accelerator will be backed by "
            "[torchdata.StatefulDataLoader](https://github.com/pytorch/data/tree/main/torchdata/stateful_dataloader).
            This requires `torchdata` version 0.8.0 or higher that supports StatefulDataLoader to be installed."
        torch_device_mesh (`torch.distributed.DeviceMesh`, *optional*, defaults to `None`):
            PyTorch device mesh.


    Returns:
        `torch.utils.data.dataloader.DataLoader`: A new data loader that will yield the portion of the batches

    <Tip warning={true}>

    `BatchSampler`s with varying batch sizes are not enabled by default. To enable this behaviour, set `even_batches`
    equal to `False`

    </Tip>
    """
    if dispatch_batches is None:
        if not put_on_device:
            dispatch_batches = False
        else:
            dispatch_batches = isinstance(dataloader.dataset, IterableDataset)

    if dispatch_batches and not put_on_device:
        raise ValueError("Using `dispatch_batches=True` requires `put_on_device=True`.")
    # Grab defaults from PartialState
    state = PartialState()
    if num_processes is None:
        num_processes = state.num_processes

    if process_index is None:
        process_index = state.process_index

    if torch_device_mesh:
        if state.distributed_type == DistributedType.DEEPSPEED:
            # In DeepSpeed, the optimizer sharing level in DP is determined by the config file.
            # Only considers "dp" and "tp".
            # Given a device mesh (dp, tp) = (2, 3):
            # - From the data parallel perspective, ranks should be structured as: 0 0 0 1 1 1
            # - Processes with the same DP rank will receive the same batch.
            submesh_tp_size = 1
            if "tp" in torch_device_mesh.mesh_dim_names:
                submesh_tp_size = torch_device_mesh["tp"].size()
            process_index = process_index // submesh_tp_size
            num_processes = num_processes // submesh_tp_size
        else:
            # when device mesh is used, specifically with TP
            # then there is need to update process_index and num_processes
            # to bring in the effect of generating same batch across TP ranks
            # and different batch across FSDP and DP ranks.
            # Example:
            # if device mesh is (dp,fsdp,tp) = (2, 2, 3)
            # ranks would range from 0...11
            # from data angle ranks should look like 0 0 0 1 1 1 2 2 2 3 3 3
            # processes with same ranks/ids would receive the same batch
            # for CP the same as TP applies
            submesh_fsdp_size = 1
            submesh_dp_size = 1
            submesh_tp_size = 1
            submesh_cp_size = 1
            if "tp" in torch_device_mesh.mesh_dim_names:
                submesh_tp_size = torch_device_mesh["tp"].size()
            if "cp" in torch_device_mesh.mesh_dim_names:
                submesh_cp_size = torch_device_mesh["cp"].size()
            if "dp_replicate" in torch_device_mesh.mesh_dim_names:
                submesh_dp_size = torch_device_mesh["dp_replicate"].size()
            if "dp_shard" in torch_device_mesh.mesh_dim_names:
                submesh_fsdp_size = torch_device_mesh["dp_shard"].size()
            process_index = process_index // (submesh_tp_size * submesh_cp_size)
            num_processes = submesh_fsdp_size * submesh_dp_size

    # Sanity check
    if split_batches:
        if dataloader.batch_size is not None:
            batch_size_for_check = dataloader.batch_size
        else:
            # For custom batch_sampler
            if hasattr(dataloader.batch_sampler, "batch_size"):
                batch_size_for_check = dataloader.batch_sampler.batch_size
            else:
                raise ValueError(
                    "In order to use `split_batches==True` you must have a `batch_size` attribute either in the passed "
                    "`dataloader` or `dataloader.batch_sampler` objects, and it has to return a natural number. "
                    "Your `dataloader.batch_size` is None and `dataloader.batch_sampler` "
                    f"(`{type(dataloader.batch_sampler)}`) does not have the `batch_size` attribute set."
                )

        if batch_size_for_check > 1 and batch_size_for_check % num_processes != 0:
            raise ValueError(
                f"To use a `DataLoader` in `split_batches` mode, the batch size ({dataloader.batch_size}) "
                f"needs to be a round multiple of the number of processes ({num_processes})."
            )

    new_dataset = dataloader.dataset
    # Iterable dataset doesn't like batch_sampler, but data_loader creates a default one for it
    new_batch_sampler = dataloader.batch_sampler if not isinstance(new_dataset, IterableDataset) else None
    sampler_is_batch_sampler = isinstance(dataloader.sampler, BatchSampler)
    synchronized_generator = None

    sampler = get_sampler(dataloader)
    if isinstance(sampler, RandomSampler) and use_seedable_sampler:
        # When iterating through the dataloader during distributed processes
        # we want to ensure that on each process we are iterating through the same
        # samples in the same order if a seed is set. This requires a tweak
        # to the `torch.utils.data.RandomSampler` class (if used).
        sampler = SeedableRandomSampler(
            data_source=sampler.data_source,
            replacement=sampler.replacement,
            num_samples=sampler._num_samples,
            generator=getattr(
                sampler,
                "generator",
                torch.Generator(device=torch.get_default_device() if hasattr(torch, "get_default_device") else "cpu"),
            ),
            data_seed=data_seed,
        )

    if isinstance(dataloader.sampler, RandomSampler) and state.distributed_type == DistributedType.XLA:
        # isinstance(dataloader.sampler, RandomSampler) indicates the original dataloader has `shuffle` enabled.
        generator = torch.Generator(
            device=torch.get_default_device() if hasattr(torch, "get_default_device") else "cpu"
        )
        seed = int(torch.empty((), dtype=torch.int64).random_().item())
        generator.manual_seed(seed)
        dataloader.generator = generator
        dataloader.sampler.generator = generator
    # No change if no multiprocess
    if (num_processes != 1 or state.distributed_type == DistributedType.MEGATRON_LM) and not dispatch_batches:
        if is_datasets_available():
            from datasets import IterableDataset as DatasetsIterableDataset
        if (
            is_datasets_available()
            and isinstance(new_dataset, DatasetsIterableDataset)
            and not split_batches
            and new_dataset.n_shards > num_processes
        ):
            new_dataset = new_dataset.shard(num_shards=num_processes, index=process_index)
        elif isinstance(new_dataset, IterableDataset):
            if getattr(dataloader.dataset, "generator", None) is not None:
                synchronized_generator = dataloader.dataset.generator
            new_dataset = IterableDatasetShard(
                new_dataset,
                batch_size=dataloader.batch_size,
                drop_last=dataloader.drop_last,
                num_processes=num_processes,
                process_index=process_index,
                split_batches=split_batches,
            )
        else:
            if not use_seedable_sampler and hasattr(sampler, "generator"):
                if sampler.generator is None:
                    sampler.generator = torch.Generator(
                        device=torch.get_default_device() if hasattr(torch, "get_default_device") else "cpu"
                    )
                    seed = int(torch.empty((), dtype=torch.int64).random_().item())
                    sampler.generator.manual_seed(seed)
                synchronized_generator = sampler.generator
            batch_sampler = dataloader.sampler if sampler_is_batch_sampler else dataloader.batch_sampler
            new_batch_sampler = BatchSamplerShard(
                batch_sampler,
                num_processes=num_processes,
                process_index=process_index,
                split_batches=split_batches,
                even_batches=even_batches,
            )

    # We ignore all of those since they are all dealt with by our new_batch_sampler
    ignore_kwargs = [
        "batch_size",
        "shuffle",
        "sampler",
        "batch_sampler",
        "drop_last",
    ]

    if rng_types is not None and synchronized_generator is None and "generator" in rng_types:
        rng_types.remove("generator")

    kwargs = {
        k: getattr(dataloader, k, _PYTORCH_DATALOADER_KWARGS[k])
        for k in _PYTORCH_DATALOADER_KWARGS
        if k not in ignore_kwargs
    }

    # Need to provide batch_size as batch_sampler is None for Iterable dataset
    if new_batch_sampler is None:
        kwargs["drop_last"] = dataloader.drop_last
        kwargs["batch_size"] = (
            dataloader.batch_size // num_processes if split_batches and not dispatch_batches else dataloader.batch_size
        )
    if dispatch_batches:
        kwargs.pop("generator")
        dataloader = DataLoaderDispatcher(
            new_dataset,
            split_batches=split_batches,
            batch_sampler=new_batch_sampler,
            _drop_last=dataloader.drop_last,
            _non_blocking=non_blocking,
            slice_fn=slice_fn_for_dispatch,
            use_stateful_dataloader=use_stateful_dataloader,
            torch_device_mesh=torch_device_mesh,
            **kwargs,
        )
    elif sampler_is_batch_sampler:
        dataloader = DataLoaderShard(
            new_dataset,
            device=device if put_on_device and state.distributed_type != DistributedType.XLA else None,
            sampler=new_batch_sampler,
            batch_size=dataloader.batch_size,
            rng_types=rng_types,
            _drop_last=dataloader.drop_last,
            _non_blocking=non_blocking,
            synchronized_generator=synchronized_generator,
            use_stateful_dataloader=use_stateful_dataloader,
            **kwargs,
        )
    else:
        dataloader = DataLoaderShard(
            new_dataset,
            device=device if put_on_device and state.distributed_type != DistributedType.XLA else None,
            batch_sampler=new_batch_sampler,
            rng_types=rng_types,
            synchronized_generator=synchronized_generator,
            _drop_last=dataloader.drop_last,
            _non_blocking=non_blocking,
            use_stateful_dataloader=use_stateful_dataloader,
            **kwargs,
        )

    if isinstance(sampler, SeedableRandomSampler) and use_seedable_sampler:
        dataloader.set_sampler(sampler)
    if state.distributed_type == DistributedType.XLA:
        return MpDeviceLoaderWrapper(dataloader, device)
    return dataloader


class SkipBatchSampler(BatchSampler):
    """
    A `torch.utils.data.BatchSampler` that skips the first `n` batches of another `torch.utils.data.BatchSampler`.
    Should not be used if the original dataloader is a `StatefulDataLoader`.
    """

    def __init__(self, batch_sampler, skip_batches=0):
        self.batch_sampler = batch_sampler
        self.skip_batches = skip_batches

    def __iter__(self):
        for index, samples in enumerate(self.batch_sampler):
            if index >= self.skip_batches:
                yield samples

    @property
    def total_length(self):
        return len(self.batch_sampler)

    def __len__(self):
        return len(self.batch_sampler) - self.skip_batches


class SkipDataLoader(DataLoaderAdapter, DataLoaderStateMixin):
    """
    Subclass of a PyTorch `DataLoader` that will skip the first batches. Generally it's preferable to use
    `skip_first_batches`/`torchdata.StatefulDataLoader` instead of this class.

    Args:
        dataset (`torch.utils.data.dataset.Dataset`):
            The dataset to use to build this dataloader.
        skip_batches (`int`, *optional*, defaults to 0):
            The number of batches to skip at the beginning.
        kwargs:
            All other keyword arguments to pass to the regular `DataLoader` initialization.
    """

    def __init__(self, dataset, skip_batches=0, use_stateful_dataloader=False, **kwargs):
        super().__init__(dataset, use_stateful_dataloader=use_stateful_dataloader, **kwargs)
        self.skip_batches = skip_batches
        self.gradient_state = GradientState()

    def __iter__(self):
        self.begin()
        for index, batch in enumerate(self.base_dataloader.__iter__()):
            if index >= self.skip_batches:
                self._update_state_dict()
                yield batch
        self.end()

    def __len__(self):
        return len(self.base_dataloader) - self.skip_batches

    def __reduce__(self):
        """
        Define the `__reduce__` method to ensure a `SkipDataLoader` can be pickled and unpickled. This needs to be
        explicitly defined since default pickling behavior is broken by `DataLoaderAdapter` messing with its
        `__class__` member.
        """
        args = super().__reduce__()
        return (SkipDataLoader, *args[1:])


def skip_first_batches(dataloader, num_batches=0):
    """
    Creates a `torch.utils.data.DataLoader` that will efficiently skip the first `num_batches`. Should not be used if
    the original dataloader is a `StatefulDataLoader`.
    """
    state = PartialState()
    if state.distributed_type == DistributedType.XLA:
        device = dataloader.device
        dataloader = dataloader.dataloader

    dataset = dataloader.dataset
    sampler_is_batch_sampler = False
    if isinstance(dataset, IterableDataset):
        new_batch_sampler = None
    else:
        sampler_is_batch_sampler = isinstance(dataloader.sampler, BatchSampler)
        batch_sampler = dataloader.sampler if sampler_is_batch_sampler else dataloader.batch_sampler
        new_batch_sampler = SkipBatchSampler(batch_sampler, skip_batches=num_batches)

    # We ignore all of those since they are all dealt with by our new_batch_sampler
    ignore_kwargs = [
        "batch_size",
        "shuffle",
        "sampler",
        "batch_sampler",
        "drop_last",
    ]

    kwargs = {
        k: getattr(dataloader, k, _PYTORCH_DATALOADER_KWARGS[k])
        for k in _PYTORCH_DATALOADER_KWARGS
        if k not in ignore_kwargs
    }

    # Need to provide batch_size as batch_sampler is None for Iterable dataset
    if new_batch_sampler is None:
        kwargs["drop_last"] = dataloader.drop_last
        kwargs["batch_size"] = dataloader.batch_size

    if isinstance(dataloader, DataLoaderDispatcher):
        if new_batch_sampler is None:
            # Need to manually skip batches in the dataloader
            kwargs["skip_batches"] = num_batches
        dataloader = DataLoaderDispatcher(
            dataset,
            split_batches=dataloader.split_batches,
            batch_sampler=new_batch_sampler,
            _drop_last=dataloader._drop_last,
            **kwargs,
        )
    elif isinstance(dataloader, DataLoaderShard):
        if new_batch_sampler is None:
            # Need to manually skip batches in the dataloader
            kwargs["skip_batches"] = num_batches
        elif sampler_is_batch_sampler:
            kwargs["sampler"] = new_batch_sampler
            kwargs["batch_size"] = dataloader.batch_size
        else:
            kwargs["batch_sampler"] = new_batch_sampler
        dataloader = DataLoaderShard(
            dataset,
            device=dataloader.device,
            rng_types=dataloader.rng_types,
            synchronized_generator=dataloader.synchronized_generator,
            **kwargs,
        )
    else:
        if new_batch_sampler is None:
            # Need to manually skip batches in the dataloader
            dataloader = SkipDataLoader(dataset, skip_batches=num_batches, **kwargs)
        else:
            dataloader = DataLoader(dataset, batch_sampler=new_batch_sampler, **kwargs)

    if state.distributed_type == DistributedType.XLA:
        dataloader = MpDeviceLoaderWrapper(dataloader, device)

    return dataloader
