import logging
from inspect import Parameter, currentframe
from itertools import repeat
from typing import Callable, Iterable, Optional, Tuple, Union

import attrs

from parafun.backend.mixins import BackendEngine
from parafun.entry_point import get_parallel_backend, set_parallel_backend_context
from parafun.functions import parallel_timed_map
from parafun.kernel.function_signature import FunctionSignature, NamedArguments
from parafun.object import FunctionInputType, FunctionOutputType, PartitionType
from parafun.partition.api import multiple_arguments
from parafun.partition.object import PartitionFunction
from parafun.partition_size_estimator.linear_regression_estimator import LinearRegessionEstimator
from parafun.partition_size_estimator.mixins import PartitionSizeEstimator
from parafun.profiler.functions import export_task_trace, print_profile_trace, timed_combine_with, timed_partition
from parafun.profiler.object import PartitionedTaskTrace


@attrs.define
class ParallelFunction:
    """Wraps a function so that it executes in parallel using a map-reduce/scatter-gather approach.

    See the `@parafun()` decorator for a more user-friendly interface.
    """

    function: Callable[[FunctionInputType], FunctionOutputType] = attrs.field()

    combine_with: Callable[[Iterable[FunctionOutputType]], FunctionOutputType] = attrs.field()

    split: PartitionFunction[NamedArguments] = attrs.field()

    function_name: Optional[str] = attrs.field(default=None)

    initial_partition_size: Optional[Callable[[FunctionInputType], int]] = attrs.field(default=None)
    fixed_partition_size: Optional[Callable[[FunctionInputType], int]] = attrs.field(default=None)

    profile: bool = attrs.field(default=False)
    trace_export: Optional[str] = attrs.field(default=None)

    partition_size_estimator_factory: Callable[[], PartitionSizeEstimator] = attrs.field(
        default=LinearRegessionEstimator
    )

    _partition_size_estimator: Optional[PartitionSizeEstimator] = attrs.field(init=False, default=None)

    _function_signature: FunctionSignature = attrs.field(init=False)
    _partition_arg_names: Tuple[str] = attrs.field(init=False)

    def __init__(
        self,
        *args,
        split: Optional[PartitionFunction[NamedArguments]] = None,
        partition_on: Optional[Union[str, Tuple[str, ...]]] = None,
        partition_with: Optional[PartitionFunction[PartitionType]] = None,
        initial_partition_size: Optional[Callable[[FunctionInputType], int]] = None,
        fixed_partition_size: Optional[Callable[[FunctionInputType], int]] = None,
        **kwargs,
    ):
        if (partition_on is None) != (partition_with is None):
            raise ValueError("`partition_on` and `partition_with` should be both simultaneously set or None.")

        if partition_on is not None:
            assert partition_with is not None

            if split is not None:
                raise ValueError("`split` cannot be set with `partition_on` or `partition_with`.")

            if isinstance(partition_on, str):
                partition_on = (partition_on,)

            # Implements the legacy `partition_on` and `partition_with` API using the newer `split` API.

            split = ParallelFunction._legacy_partition_with(partition_on, partition_with)
            initial_partition_size = ParallelFunction._legacy_partition_size(partition_on, initial_partition_size)
            fixed_partition_size = ParallelFunction._legacy_partition_size(partition_on, fixed_partition_size)

        self.__attrs_init__(
            *args,
            split=split,
            initial_partition_size=initial_partition_size,
            fixed_partition_size=fixed_partition_size,
            **kwargs,
        )

        self._function_signature = FunctionSignature.from_function(self.function)

        if self.initial_partition_size is not None and self.fixed_partition_size is not None:
            raise ValueError("`initial_partition_size` and `fixed_partition_size` cannot be set simultaneously.")

        if self.fixed_partition_size is None:
            self._partition_size_estimator = self.partition_size_estimator_factory()

        self._validate_function_signature()

    @initial_partition_size.validator
    @fixed_partition_size.validator
    def _partition_size_validator(self, attribute, value):
        if value is not None and not isinstance(value, int) and not callable(value):
            raise ValueError(f"`{attribute.name}` should be either an integer, a callable or `None`.")

    def _validate_function_signature(self):
        if self._function_signature.has_var_arg or self._function_signature.has_var_kwarg:
            return

        if any(arg.kind == Parameter.POSITIONAL_ONLY for arg in self._function_signature.args.values()):
            raise ValueError("parafun toolkit does not support positional only parameters yet.")

    def __call__(self, *args, **kwargs) -> FunctionOutputType:
        current_backend = get_parallel_backend()
        allows_nested_tasks = current_backend is not None and current_backend.allows_nested_tasks()

        # Note: is_nested_parallelism check should appears before any backend check, as unsupported nested function
        # calls will have an empty backend setup.
        if is_nested_parallelism() and not allows_nested_tasks:
            logging.debug(
                f"backend does not support nested parallelism. Running {self.function.__name__} sequentially."
            )
            return self.function(*args, **kwargs)

        if current_backend is None:
            logging.warning(f"no parallel backend engine set, run `{self.function_name}(...)` sequentially.")
            return self.function(*args, **kwargs)

        # 1. Assigns a name to each argument based on the decorated function's signature.

        named_args = self._function_signature.assign(args, kwargs)

        # 2. Builds the partitions

        non_partitioned_args, partition_generator = self.split(named_args)

        with current_backend.session() as backend_session:
            # 3. Preloads the non-partitioned arguments for each partition.
            preloaded_non_partitioned_args = backend_session.preload_value(non_partitioned_args)

            # 4. Generates the partition

            initial_partition_size, fixed_partition_size = self._get_user_partition_sizes(args, kwargs)

            partitions = timed_partition(
                partition_generator, self._partition_size_estimator, initial_partition_size, fixed_partition_size
            )

            # 5. Submits the function to the parallel backend.

            if allows_nested_tasks:
                nested_backend = current_backend
            else:
                nested_backend = None

            results = parallel_timed_map(
                apply_function,
                repeat(self.function),
                repeat(preloaded_non_partitioned_args),
                partitions,
                repeat(nested_backend),
                backend_session=backend_session,
            )

            # 6. Combines results

            combined_result, task_trace = timed_combine_with(self.combine_with, self._partition_size_estimator, results)

        if self.profile:
            print_profile_trace(self.function, self.function_name, self._partition_size_estimator, task_trace)

        if self.trace_export:
            export_task_trace(self.trace_export, task_trace)

        logging.info(
            f"Run `{self.function_name}(...)` with {task_trace.partition_count} of "
            f"sub-tasks using backend {current_backend.__class__} successfully"
        )

        return combined_result

    def _get_user_partition_sizes(self, args, kwargs) -> Tuple[Optional[int], Optional[int]]:
        """Returns the initial partition size and fixed partition size for the calling function arguments."""

        if callable(self.initial_partition_size):
            initial_partition_size = self.initial_partition_size(*args, **kwargs)
        else:
            initial_partition_size = self.initial_partition_size

        if callable(self.fixed_partition_size):
            fixed_partition_size = self.fixed_partition_size(*args, **kwargs)
        else:
            fixed_partition_size = self.fixed_partition_size

        return initial_partition_size, fixed_partition_size

    @staticmethod
    def _legacy_partition_with(
        partition_on: Union[str, Tuple[str, ...]], partition_with: PartitionFunction[PartitionType]
    ) -> PartitionFunction[NamedArguments]:
        """Implements the legacy `partition_on` and `partition_with` API using the newer `split` interface."""

        return multiple_arguments(partition_on, partition_with)

    @staticmethod
    def _legacy_partition_size(
        partition_on: Tuple[str, ...], partition_size: Optional[Callable[[FunctionInputType], int]]
    ) -> Optional[Callable[[FunctionInputType], int]]:
        """
        Implements the legacy behaviour of `initial_partition_size` and `fixed_partition_size` when used with
        `partition_on` and `partition_with` API.
        """

        if not callable(partition_size):
            return partition_size

        # When the partition size argument is a callable, the old API only passes the `partition_on` values as
        # positional arguments.

        def legacy_partition_size(**kwargs):
            partition_args = [kwargs[arg_name] for arg_name in partition_on]
            return partition_size(*partition_args)

        return legacy_partition_size


def is_nested_parallelism():
    """Returns True if there is any call to `_apply_function()` in the current call stack."""

    frame = currentframe()
    while frame is not None:
        if frame.f_code.co_name == apply_function.__name__ and frame.f_code.co_filename == __file__:
            return True
        frame = frame.f_back
    return False


def apply_function(
    function: Callable[[PartitionType], FunctionOutputType],
    non_partitioned_args: NamedArguments,
    partition: Tuple[NamedArguments, PartitionedTaskTrace],
    backend: Optional[BackendEngine] = None,
) -> Tuple[FunctionOutputType, PartitionedTaskTrace]:
    """
    Runs the function with the partitioned object and its profiling trace.

    :param non_partitioned_args: the function arguments that are identical for every function call.
    :param partition: the partitioned arguments and the associated partition task trace.
    :param backend: if not None, setup this backend before executing the function.

    :returns the function's output and the original partition task trace.
    """

    partitioned_args, trace = partition

    merged_args = non_partitioned_args.merge(partitioned_args)
    assert len(non_partitioned_args.var_args) == 0

    args, kwargs = merged_args.as_args_kwargs()

    if backend is not None:
        with set_parallel_backend_context(backend):
            result = function(*args, **kwargs)
    else:
        result = function(*args, **kwargs)

    return result, trace