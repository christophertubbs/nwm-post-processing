#!/usr/bin/env python3
"""
Defines a ProfileOperation that is used to groups inputs by lead for smaller operations
"""
import os
import typing
import collections.abc as generic
import pathlib
import logging
import dataclasses

from datetime import timedelta

from post_processing.nwm_file import NWMFile
from post_processing.utilities.logging import get_logger
from post_processing.schema.base import postprocessing_model
from post_processing.schema import profile as base_profile
from post_processing.enums import TimeUnit
from post_processing.schema.base import member
from post_processing.work import starmap
from post_processing.work import starmap_executor
from post_processing.utilities.common import timed_function
from post_processing.configuration import settings

if typing.TYPE_CHECKING:
    import xarray
    import numpy

LOGGER: logging.Logger = get_logger(__file__)

@dataclasses.dataclass
class FloorAndMaximum:
    """
    A coupling of the time from which data starts being covered to the time it is last valid. Consider the
    floor and maximum time as (floor time, maximum time] that spans a subset of a time oriented simulation
    """
    floor: "numpy.datetime64"
    """The time at which all data occurs after"""
    maximum: "numpy.datetime64"
    """The lastest time that data within the span may reach"""

    def to_numpy(self) -> "numpy.typing.NDArray[numpy.datetime64]":
        """
        Convert the floor and maximum into a numpy array
        """
        return numpy.array([self.floor, self.maximum], dtype=numpy.datetime64)

    def __str__(self):
        return f"({self.floor}, {self.maximum}]"

    def __len__(self):
        return 2

    def __getitem__(self, item: int) -> "numpy.datetime64":
        if item == 0 or item == -2:
            return self.floor
        elif item == 1 or item == -1:
            return self.maximum
        raise KeyError(f"Index {item} is not a valid index - minimum is 0, maximum is 1")

    def __iter__(self):
        yield self.floor
        yield self.maximum


def get_group_name(
    group_duration: timedelta,
    last_lead: timedelta
) -> str:
    """
    Generate a name for a group based on its forecast hours

    :param group_duration: duration of the simulation that each group spans
    :param last_lead: The furthest out within the simulation that this group individually reaches
    :return: A name for the group based upon the group duration and the group that the data pertains to
    """
    total_hours: int = int(last_lead.total_seconds() / 3600)
    if group_duration == timedelta(hours=24) and total_hours >= 24 and total_hours % 24 == 0:
        return f"day{total_hours // 24}"
    return f"{total_hours}hour"


@timed_function()
def generate_groups_by_lead(
    paths: generic.Iterable[pathlib.Path],
    duration: timedelta,
) -> generic.Mapping[str, generic.Sequence[pathlib.Path]]:
    """
    Split NWM file paths into groups based off of a duration of acceptable leads

    :param paths: All paths that will be split into groups
    :param duration: The amount of time to group data by
    :return: A mapping between group names and the input data that belongs to it
    """
    groups: dict[str, generic.Sequence[pathlib.Path]] = {}
    files: list[NWMFile] = sorted(map(NWMFile.parse, paths))

    current_group: list[NWMFile] = []
    current_upper_limit: timedelta = duration

    while files:
        current_file: NWMFile = files.pop(0)
        if current_file.lead > current_upper_limit:
            group_name: str = get_group_name(
                group_duration=duration,
                last_lead=current_group[-1].lead
            )
            groups[group_name] = [file.path for file in current_group]
            current_group = []
            current_upper_limit += duration
        current_group.append(current_file)

    if current_group:
        group_name: str = get_group_name(
            group_duration=duration,
            last_lead=current_group[-1].lead
        )
        groups[group_name] = [file.path for file in current_group]

    return groups


def get_floor_and_maximum_time(
    variable: "xarray.DataArray",
    period: typing.Union[str, timedelta, "numpy.datetime64"] = "PT1H"
) -> FloorAndMaximum:
    """
    Get the time at which all data within a variable occurs and the latest possible time that data may occur

    :param variable: The netcdf variable containing time oriented data
    :param period: The period over which data has been generated. If the data represents a mean over an hour, the period would be an hour
    :returns: A structure containing the floor time and maximum time
    """
    import numpy

    # Don't check for ints because knowing the start time can be a mess
    if not numpy.issubdtype(variable.dtype, numpy.datetime64):
        raise TypeError(
            f"The input array needed to determine the time floor ({variable.name}) must be a datetime64 but was instead "
            f"'{variable.dtype}' (type={type(variable.dtype)})"
        )

    if len(variable.shape) != 1:
        raise ValueError(
            f"Cannot determine the minimum floor time from "
            f"'{variable.name}({', '.join(map(lambda pair: str(pair[0]) + '=' + str(pair[1]), variable.sizes))})' - "
            f"it is not one dimensional"
        )

    if isinstance(period, str):
        from post_processing.utilities.common import parse_timedelta
        period: timedelta = parse_timedelta(period=period)

    if isinstance(period, timedelta):
        period: numpy.timedelta64 = numpy.timedelta64(period)

    if not isinstance(period, numpy.timedelta64):
        raise TypeError(
            f"Cannot determine the floor of a time variable - The defined period is not a timedelta64 dtype"
        )

    minimum: numpy.datetime64 = numpy.min(variable.data)

    # The floor will be the time of earliest value minus the simulated duration
    floor: numpy.datetime64 = minimum - period

    maximum: numpy.datetime64 = numpy.max(variable.data)

    floor_and_maximum: FloorAndMaximum = FloorAndMaximum(
        floor=floor,
        maximum=maximum,
    )

    return floor_and_maximum

@timed_function()
def create_time_bounds(
    first_file: pathlib.Path,
    last_file: pathlib.Path,
    time_variable_name: str = "time",
    period: typing.Union[str, timedelta, "numpy.timedelta64"] = "PT1H",
    attributes: typing.Dict[str, typing.Any] | None = None,
    encoding: typing.Dict[str, typing.Any] | None = None,
    name: str = "time_bounds",
    dimensions: generic.Sequence[str] | None = None,
) -> "xarray.DataArray":
    """
    Create a netcdf variable representing the bounds for what values a set of data represents

    :param first_file: The path to the earliest data in grouped data
    :param last_file: The path to the latest data in grouped data
    :param time_variable_name: The name of the time variable within the datasets
    :param period: The period of time over which data is generated. It would be an hour if the read data is the
    mean over an hour, for example
    :param attributes: Optional dictionary of attributes to include in the netcdf variable
    :param encoding: Optional instructions on how to encode the values on disk
    :param name: The name of the time bound variable
    :param dimensions: The ordered dimension names that should be within the bounds. Must have a length of 2 if given,
    first being the time variable, the second marking the beginning and end for each entry of the time variable.
    :returns: A netcdf variable represented the lowest bound exclusive and the upper bound inclusive of the data included within a group
    """
    if isinstance(dimensions, generic.Sequence) and len(dimensions) != 2:
        raise ValueError(
            f"A definition of the dimensions of the time bound were given, but they must be of length 2, not: "
            f"{', '.join(map(str, dimensions))}"
        )

    import numpy
    import xarray

    from post_processing.work import cycle_future_list
    from post_processing.utilities.netcdf import submit_variable_transformation
    from post_processing.interfaces.work import PendingTaskResult

    # Schedule tasks that will determine the earliest and lastest times within the given files
    future_time_bounds: list[PendingTaskResult[FloorAndMaximum]] = [
        submit_variable_transformation(
            target=first_file,
            variable_name=time_variable_name,
            function=get_floor_and_maximum_time,
            kwargs={
                "period": period
            }
        ),
        submit_variable_transformation(
            target=last_file,
            variable_name=time_variable_name,
            function=get_floor_and_maximum_time,
            kwargs={
                "period": period
            }
        )
    ]

    # Wait for the tasks to complete
    minimum_and_maximum_times, errors = cycle_future_list(future_time_bounds)

    if errors:
        if len(errors) == 1:
            raise errors[0]
        from post_processing.utilities.common import condense_exceptions
        raise condense_exceptions(
            message=f"Could not create the time bounds between '{first_file}' and '{last_file}'",
            exceptions=errors
        )

    earliest: numpy.datetime64 = numpy.min(minimum_and_maximum_times)
    latest: numpy.datetime64 = numpy.max(minimum_and_maximum_times)

    if not dimensions:
        dimensions = (time_variable_name, "nv")

    array: xarray.DataArray = xarray.DataArray(
        name=name,
        dims=dimensions,
        data=numpy.array([[earliest, latest]]),
        attrs=attributes.copy() if isinstance(attributes, generic.Mapping) else {}
    )

    array.attrs["earliest"] = str(earliest)
    array.attrs["latest"] = str(latest)

    if isinstance(encoding, generic.Mapping):
        array.encoding.update(encoding)

    return array

@timed_function()
def apply_time_bounds(
    dataset: "xarray.Dataset",
    first_file: pathlib.Path,
    last_file: pathlib.Path,
    time_variable_name: str = "time",
    period: typing.Union[str, timedelta, "numpy.timedelta64"] = "PT1H",
    attributes: typing.Dict[str, typing.Any] | None = None,
    encoding: typing.Dict[str, typing.Any] | None = None,
    name: str = "time_bounds",
    dimensions: generic.Sequence[str] | None = None,
) -> "xarray.Dataset":
    """
    Attach time bounds to a dataset

    :param dataset: The dataset to apply time bounds to
    :param first_file: The path to the earliest data in grouped data
    :param last_file: The path to the latest data in grouped data
    :param time_variable_name: The name of the time variable within the dataset
    :param period: The amount of time that each time entry spans within the dataset
    :param attributes: Attributes to attach to the time bound
    :param encoding: Optional instructions on how to encode the values on disk
    :param name: The name of the time bound variable
    :param dimensions: The ordered dimension names that should be within the bounds. Must have a length of 2 if given
    :returns: The given dataset with the appropriate time range
    """
    time_bounds: xarray.DataArray = create_time_bounds(
        first_file=first_file,
        last_file=last_file,
        time_variable_name=time_variable_name,
        period=period,
        attributes=attributes,
        encoding=encoding,
        name=name,
        dimensions=dimensions,
    )

    encoding: generic.Mapping[str, typing.Any] = time_bounds.encoding.copy()
    attributes: generic.Mapping[str, typing.Any] = time_bounds.attrs.copy()

    dataset[time_bounds.name] = time_bounds
    dataset[time_bounds.name].attrs.update(attributes)
    dataset[time_bounds.name].encoding.update(encoding)

    return dataset


@postprocessing_model
class GroupByLeadOperation(base_profile.PathToPathOperation, base_profile.FileOutputMixin):
    """
    Operation to group output by time relative to the reference and perform operations on each
    """
    @classmethod
    def operation(cls) -> base_profile.OperationType:
        return base_profile.OperationType.GROUP_BY

    def _validate(self):
        if isinstance(self.time_unit, str):
            self.time_unit = TimeUnit(self.time_unit)

        self._duration = self.time_unit * self.amount_of_time
        errors: list[Exception] = []
        if len(self.on_each) == 0:
            errors.append(
                ValueError(
                    f"Encountered an invalid {self.__class__.__qualname__} operation - there is no configured logic"
                )
            )
        for operation_index, operation in enumerate(self.on_each):
            try:
                if isinstance(operation, generic.Mapping):
                    operation = base_profile.load_operation(specification=operation)
                    self.on_each[operation_index] = operation
                elif not isinstance(operation, base_profile.ProfileOperation):
                    raise ValueError(
                        f"Encountered an invalid sub-operation for a {self.__class__.__qualname__} - item "
                        f"{operation_index}  holds a '{type(operation)}', which cannot "
                        f"be converted into a {base_profile.ProfileOperation.__qualname__}"
                    )
            except Exception as exception:
                errors.append(exception)

        if len(errors) == 1:
            raise errors[0]
        elif errors:
            from post_processing.utilities.common import condense_exceptions
            raise condense_exceptions(
                message=f"Encountered an invalid {self.__class__.__qualname__} operation",
                exceptions=errors
            )

    @timed_function(name="GroupByLeadOperation")
    def __call__(
        self,
        profile: base_profile.Profile,
        process_identifier: str,
        work_directory: pathlib.Path,
        data: generic.Sequence[pathlib.Path],
        previous_operations: list[base_profile.ProfileOperation],
        metadata: dict[str, typing.Any]
    ) -> generic.Sequence[pathlib.Path]:
        """
        Group given files by lead and apply operations on their grouped data

        NOTE: This may encounter errors if file names don't match the WCOSS model name standard to some degree.
        See the NWM_FILENAME_PATTERN for how names should be formed.

        :param profile: The profile that prescribed this operation
        :param process_identifier: The identifier for this process
        :param work_directory: The directory that intermediate work may be saved in
        :param data: The data to group
        :param previous_operations: The previously applied operations
        :param metadata: The metadata that may be used to form names
        :return: Paths to the output files
        """
        if not isinstance(data, generic.Sequence):
            raise TypeError(
                f"Cannot group data - a series of paths must be given, but instead received a '{type(data)}'"
            )

        file_groups: generic.Mapping[str, generic.Sequence[pathlib.Path] | pathlib.Path] = generate_groups_by_lead(
            paths=data,
            duration=self._duration,
        )

        keyword_arguments: dict[str, dict] = {}

        for group_name, file_group in file_groups.items():
            group_metadata: dict[str, typing.Any] = {
                "group": group_name,
                **metadata.copy(),
            }
            keyword_arguments[group_name] = {
                "operations": self.on_each,
                "profile": profile,
                "process_identifier": process_identifier,
                "work_directory": work_directory,
                "data": file_group,
                "previous_operations": previous_operations.copy(),
                "metadata": group_metadata
            }

        results: generic.Mapping[str, generic.Sequence[pathlib.Path]] = starmap_executor(
            function=base_profile.call_generic_operations,
            args=keyword_arguments,
            executor=profile.executor,
            fallback_to_threads=True
        )

        if self.include_time_bounds:
            final_results: generic.Sequence[pathlib.Path] = self._add_time_bounds_to_results(
                results=results,
                file_groups=file_groups,
                keyword_arguments=keyword_arguments,
                work_directory=work_directory
            )
        else:
            final_results: list[pathlib.Path] = []
            for key, files in results.items():
                if isinstance(files, pathlib.Path):
                    final_results.append(files)
                else:
                    final_results.extend(files)


        return final_results

    def _add_time_bounds_to_results(
        self,
        results: generic.Mapping[str, typing.Sequence[pathlib.Path] | pathlib.Path],
        file_groups: generic.Mapping[str, generic.Sequence[pathlib.Path] | pathlib.Path],
        keyword_arguments: generic.Mapping[str, generic.Mapping[str, typing.Any]],
        work_directory: pathlib.Path
    ) -> generic.Sequence[pathlib.Path]:
        from post_processing.utilities.netcdf import submit_dataset_operation
        from post_processing.interfaces.work import PendingTaskResult

        time_bound_applications: list[PendingTaskResult[pathlib.Path]] = []
        for group_name in list(results.keys()):
            generated_paths: generic.Sequence[pathlib.Path] | pathlib.Path = results[group_name]
            group_metadata: generic.Mapping[str, typing.Any] = keyword_arguments[group_name]["metadata"]

            if isinstance(generated_paths, pathlib.Path):
                generated_paths = [generated_paths]

            for path in generated_paths:
                updated_path: pathlib.Path = self.get_output_path(
                    work_directory=work_directory,
                    input_path=path,
                    **group_metadata
                )
                first_file: pathlib.Path = file_groups[group_name][0]
                last_file: pathlib.Path = file_groups[group_name][-1]

                time_bound_applications.append(
                    submit_dataset_operation(
                        target=path,
                        output_path=updated_path,
                        function=apply_time_bounds,
                        kwargs={
                            "first_file": first_file,
                            "last_file": last_file,
                            "time_variable_name": self.time_variable_name,
                            "period": self.period,
                            "attributes": self.time_bound_attributes.copy(),
                            "encoding": self.time_bound_encoding.copy(),
                            "name": self.time_bound_name,
                            "dimensions": self.time_bound_dimensions
                        }
                    )
                )

        from post_processing.work import cycle_futures
        group_results, errors = cycle_futures(time_bound_applications)

        if errors:
            if len(errors) == 1:
                raise errors[0]
            from post_processing.utilities.common import condense_exceptions
            raise condense_exceptions(
                message=f"Could not apply time bounds to results of grouped operations",
                exceptions=errors
            )

        return group_results

    def __hash__(self):
        try:
            parent_hash: int = super().__hash__()
        except:
            parent_hash: int = 0

        child_hashes: tuple[int, ...] = tuple(map(hash, self.on_each))
        return hash((
            parent_hash,
            self.time_unit,
            self.amount_of_time,
            *child_hashes,
        ))

    def __str__(self):
        description: str = f"{self.operation_id}: " if self.operation_id else ""
        description += (
            f"Group files into data {self.amount_of_time} {self.time_unit} at a time and perform the following "
            f"operations on each:{os.linesep}"
        )
        each_description: list[str] = list(map(lambda operation: f"    - {operation}", self.on_each))
        description += os.linesep.join(each_description)
        return description

    on_each: list[base_profile.ProfileOperation]
    """The operations to perform on each group"""
    time_unit: TimeUnit
    """The unit of time that the individual inputs were formulated/aggregated over"""
    amount_of_time: typing.Union[int, float] = dataclasses.field(default=1.0, kw_only=True)
    """The amount of time units that individual inputs were formulated/aggregated over"""
    period: typing.Union[str, timedelta, "numpy.timedelta64"] = dataclasses.field(default="PT1H", kw_only=True)
    """The amount of time to group individual inputs over"""
    include_time_bounds: bool = dataclasses.field(default=False, kw_only=True)
    """Whether to include a time bound for each group"""
    time_bound_name: str = dataclasses.field(default="time_bounds", kw_only=True)
    """The name of the variable that will describe the bookends for each group"""
    time_bound_dimensions: typing.Optional[generic.Sequence[str]] = dataclasses.field(default=None, kw_only=True)
    """The names of the dimensions for the eventual time bound variable"""
    time_variable_name: str = dataclasses.field(default="time", kw_only=True)
    """The name of the eventual time bound variable"""
    time_bound_encoding: dict[str, typing.Any] = dataclasses.field(default_factory=dict, kw_only=True)
    """Custom encoding attributes that may be used to describe how the time bounds should be saved to disk"""
    time_bound_attributes: dict[str, typing.Any] = dataclasses.field(default_factory=dict, kw_only=True)
    """Custom attributes that should be attached to the time bound variable"""
    _duration: timedelta = member(default=None)
    """The internal timedelta object that formally describes the period and not just the specification for the period"""

