#!/usr/bin/env python3
"""
Defines operations used to perform calculations on streaming arrays
"""
import dataclasses
import typing
import collections.abc as generic
import pathlib
import logging
import enum
import abc
import functools

import numpy
import xarray

import post_processing.schema.profile as base_profile
from post_processing.schema.base import BaseModel
from post_processing.configuration import settings
from post_processing.utilities.logging import get_logger

LOGGER: logging.Logger = get_logger(__file__)


class OnlineCalculationType(enum.StrEnum):
    """
    Definitions for what type of operations may be performed
    """
    MEAN = "mean"
    MEDIAN = "median"
    VARIANCE = "variance"
    MAX = "max"
    MIN = "min"
    SUM = "sum"
    STD = "std"


class OnlineCalculation(abc.ABC):
    """
    Performs online operations on arrays as they arrive
    """
    def __init__(self, *args, name: str = None, **kwargs):
        for argument_index, argument in enumerate(args):
            LOGGER.debug(
                f"Received the value '{argument}' (type={type(argument)}) at variable position '{argument_index}'. "
                f"It is not used by the Base class of '{self.__class__.__qualname__}'"
            )

        for keyword, argument in kwargs.items():
            LOGGER.debug(
                f"Received the value '{argument}' (type={type(argument)}) with the key '{keyword}'. "
                f"It is not used by the Base class of '{self.__class__.__qualname__}'"
            )

        self.name: str | None = name
        self.dimensions: tuple[str, ...] | None = None
        self.count: numpy.typing.NDArray[numpy.integer] | None = None
        self.current_data: numpy.typing.NDArray[numpy.floating] | None = None
        self.encoding: dict = {}
        self.attributes: dict = {}

    def _register_first_array(self, variable: xarray.DataArray):
        """
        Set correct values that will set expectations on following inputs

        :param variable: The variable to base the settings on
        """
        self.current_data: numpy.typing.NDArray = variable.data.copy()
        self.count: numpy.typing.NDArray = numpy.zeros(shape=variable.shape, dtype=variable.dtype)
        value_mask: numpy.typing.NDArray[numpy.bool_] = ~numpy.isnan(self.current_data)

        if value_mask.any():
            self.count[value_mask] += 1

        self.encoding.update(variable.encoding)
        self.attributes.update(variable.attrs)

        self.attributes['cell_methods'] = f"{variable.dims[0]}: {self.operation_type()}"
        self.dimensions = variable.dims

        if not self.name:
            self.name = variable.name

    def _update_count(self, new_values: numpy.typing.NDArray):
        """
        Increases the counts of all non-nan new values by 1
        """
        self.count[~numpy.isnan(new_values)] += 1

    def __del__(self):
        del self.current_data

    def _array_is_valid(self, candidate: xarray.DataArray) -> bool:
        if self.current_data is None:
            return True
        if candidate.shape != self.current_data.shape:
            raise ValueError(
                f"'{candidate.name}' cannot be used to update the value of '{self}' - "
                f"its shape doesn't match ({candidate.shape} != {self.current_data.shape})"
            )
        return True

    @classmethod
    def get_operation(cls, operation_type: OnlineCalculationType) -> typing.Type["OnlineCalculation"]:
        """
        Get the correct type of operation based on the desired type

        :param operation_type: The type of operation to perform
        :returns: The operation that will calculate the desired data
        """
        available_operations: generic.Mapping[OnlineCalculationType, typing.Type[OnlineCalculation]] = get_stream_calculators()

        if operation_type in available_operations:
            return available_operations[operation_type]

        raise KeyError(f"There is not an available operation that performs a '{operation_type}' calculation")


    @classmethod
    @abc.abstractmethod
    def operation_type(cls) -> OnlineCalculationType:
        """
        What type of operation will be performed
        """
        ...

    @abc.abstractmethod
    def update(self, variable: xarray.DataArray):
        """
        Update the calculation with the next set of values

        :param variable: The next set of values to add to the calculation
        """
        ...

    def gather(self) -> xarray.DataArray:
        """
        Create an xarray array from the calculated data
        """
        completed_logic: xarray.DataArray = xarray.DataArray(
            name=self.name,
            data=self.value,
            dims=self.dimensions,
            attrs=self.attributes.copy(),
        )
        completed_logic.encoding.update(self.encoding)
        return completed_logic

    @property
    def value(self) -> numpy.typing.NDArray:
        """
        Perform the final calculation and generate the completed data

        :return: The completed data
        """
        return self.current_data.copy()


class OnlineSum(OnlineCalculation):
    """
    Sum all inbound arrays
    """
    @classmethod
    def operation_type(cls) -> OnlineCalculationType:
        return OnlineCalculationType.SUM

    def update(self, variable: xarray.DataArray):
        if self.current_data is None:
            self._register_first_array(variable=variable)
        else:
            variable_is_valid: bool = self._array_is_valid(candidate=variable)
            if not variable_is_valid:
                raise ValueError(
                    f"The variable '{variable.name}' cannot be used to update the value of '{self}'"
                )
            new_data: numpy.typing.NDArray = variable.data.copy()
            both_nan: numpy.typing.NDArray = numpy.isnan(self.current_data) & numpy.isnan(new_data)
            self.current_data = numpy.nansum([self.current_data, new_data], axis=0)

            # nansum will insert a 0 where both are nan - we want to maintain nan, so we use the mask to revert to
            # the correct values
            self.current_data[both_nan] = numpy.nan

            self._update_count(new_values=new_data)

        LOGGER.debug(f"Updated the {self.operation_type()} calculation for the {self.name} variable")


class OnlineMax(OnlineCalculation):
    """
    Calculate the max on the stream of arrays
    """
    @classmethod
    def operation_type(cls) -> OnlineCalculationType:
        return OnlineCalculationType.MAX

    def update(self, variable: xarray.DataArray):
        if self.current_data is None:
            self._register_first_array(variable=variable)
        else:
            variable_is_valid: bool = self._array_is_valid(candidate=variable)
            if not variable_is_valid:
                raise ValueError(
                    f"The variable '{variable.name}' cannot be used to update the value of '{self}'"
                )
            new_data: numpy.typing.NDArray = variable.data.copy()
            self.current_data = numpy.fmax(self.current_data, new_data)
            self._update_count(new_values=new_data)
        LOGGER.debug(f"Updated the {self.operation_type()} calculation for the {self.name} variable")


class OnlineMin(OnlineCalculation):
    """
    Calculate the min on the stream of arrays
    """
    @classmethod
    def operation_type(cls) -> OnlineCalculationType:
        return OnlineCalculationType.MIN

    def update(self, variable: xarray.DataArray):
        if self.current_data is None:
            self._register_first_array(variable=variable)
        else:
            variable_is_valid: bool = self._array_is_valid(candidate=variable)
            if not variable_is_valid:
                raise ValueError(
                    f"The variable '{variable.name}' cannot be used to update the value of '{self}'"
                )
            new_values: numpy.typing.NDArray = variable.data.copy()
            self.current_data = numpy.fmin(self.current_data, new_values)
            self._update_count(new_values=new_values)
        LOGGER.debug(f"Updated the {self.operation_type()} calculation for the {self.name} variable")


class OnlineMean(OnlineSum):
    """
    Calculate the mean on the stream of arrays
    """
    @classmethod
    def operation_type(cls) -> OnlineCalculationType:
        return OnlineCalculationType.MEAN

    @property
    def value(self) -> numpy.typing.NDArray:
        value_mask: numpy.typing.NDArray[numpy.bool_] = self.count > 0
        output_array: numpy.typing.NDArray = numpy.full_like(
            self.current_data,
            numpy.nan,
            dtype=self.current_data.dtype
        )
        numpy.divide(
            self.current_data,
            self.count,
            out=output_array,
            where=value_mask,
        )
        return output_array


class OnlineVariance(OnlineCalculation):
    """
    Calculate the variance of a stream of arrays
    """
    def __init__(self, *args, for_sample: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.for_sample: bool = for_sample
        self.mean: numpy.typing.NDArray | None = None
        self.squared_difference_from_mean: numpy.typing.NDArray | None = None

    @classmethod
    def operation_type(cls) -> OnlineCalculationType:
        return OnlineCalculationType.VARIANCE

    def _register_first_array(self, variable: xarray.DataArray):
        super()._register_first_array(variable=variable)
        self.mean = numpy.zeros(shape=variable.shape, dtype=variable.dtype)
        self.squared_difference_from_mean = numpy.zeros(shape=variable.shape, dtype=variable.dtype)

    def update(self, variable: xarray.DataArray):
        if self.current_data is None:
            self._register_first_array(variable=variable)

        new_data: numpy.typing.NDArray = variable.data.copy()

        new_value_mask: numpy.typing.NDArray[numpy.bool_] = ~numpy.isnan(new_data)
        uninitiated_cells: numpy.typing.NDArray[numpy.bool_] = (
            self.count <= 0 | numpy.isnan(self.mean) | numpy.isnan(self.squared_difference_from_mean)
        )

        novel_value_mask: numpy.typing.NDArray[numpy.bool_] = new_value_mask & uninitiated_cells

        if novel_value_mask.any():
            self.mean[novel_value_mask] = new_data[novel_value_mask]
            self.squared_difference_from_mean[novel_value_mask] = 0.0
            self.count[novel_value_mask] = 1.0

        updatable_value_mask: numpy.typing.NDArray[numpy.bool_] = new_value_mask & (~uninitiated_cells)

        if not updatable_value_mask.any():
            return

        self.count[updatable_value_mask] += 1
        difference: numpy.typing.NDArray = new_data[updatable_value_mask] - self.mean[updatable_value_mask]
        self.mean[updatable_value_mask] += difference / self.count[updatable_value_mask]
        adjusted_difference: numpy.typing.NDArray = new_data[updatable_value_mask] - self.mean[updatable_value_mask]
        self.squared_difference_from_mean[updatable_value_mask] += difference * adjusted_difference
        LOGGER.debug(f"Updated the {self.operation_type()} calculation for the {self.name} variable")

    @property
    def value(self) -> numpy.typing.NDArray:
        if self.for_sample:
            where_clause: numpy.typing.NDArray[numpy.bool_] = self.count > 1
        else:
            where_clause: numpy.typing.NDArray[numpy.bool_] = self.count > 0

        output_array: numpy.typing.NDArray = numpy.full_like(
            self.squared_difference_from_mean,
            numpy.nan,
            dtype=self.squared_difference_from_mean.dtype
        )
        numpy.divide(
            self.squared_difference_from_mean,
            self.count,
            out=output_array,
            where=where_clause
        )
        return output_array


class OnlineStandardDeviation(OnlineVariance):
    """
    Calculates the standard deviation of a stream of arrays
    """
    @classmethod
    def operation_type(cls) -> OnlineCalculationType:
        return OnlineCalculationType.STD

    @property
    def value(self) -> numpy.typing.NDArray:
        variance: numpy.typing.NDArray = super().value
        std: numpy.typing.NDArray = numpy.sqrt(variance)
        return std

@dataclasses.dataclass(unsafe_hash=True)
class StreamCalculation:
    """
    A description for a calculation to perform on a particular variable to create a new variable
    """
    input_variable: str
    calculation_type: OnlineCalculationType
    output_variable: typing.Optional[str] = dataclasses.field(default=None)

    def __post_init__(self):
        if not self.output_variable:
            self.output_variable = self.input_variable

@dataclasses.dataclass
class StreamOperation(base_profile.PathToPathOperation, base_profile.FileOutputMixin):
    @classmethod
    def operation(cls) -> base_profile.OperationType:
        return base_profile.OperationType.STREAM

    def _validate(self):
        if not self.streams:
            raise ValueError(f"Invalid Stream Operation encountered - no stream calculations were detected")

        replacements: dict[int, StreamCalculation] = {}
        for calculation_index, calculation in enumerate(self.streams):
            if isinstance(calculation, generic.Mapping):
                replacements[calculation_index] = StreamCalculation(**calculation)

        for calculation_index, calculation in replacements.items():
            self.streams[calculation_index] = calculation

    # TODO: Break up __call__ to make code reuse easier
    def __call__(
        self,
        profile: base_profile.Profile,
        process_identifier: str,
        work_directory: pathlib.Path,
        data: generic.Sequence[pathlib.Path],
        previous_operations: list[base_profile.ProfileOperation],
        metadata: dict[str, typing.Any]
    ) -> generic.Sequence[pathlib.Path]:
        from post_processing.utilities import netcdf
        available_calculation_types: dict[OnlineCalculationType, typing.Type[OnlineCalculation]] = get_stream_calculators()

        calculations: dict[str, list[OnlineCalculation]] = {}

        for stream in self.streams:
            calculations.setdefault(stream.input_variable, []).append(
                available_calculation_types[stream.calculation_type](name=stream.output_variable)
            )

        attributes: dict[str, typing.Any] | None = {}
        encoding: dict[str, typing.Any] | None = {}

        output_path: pathlib.Path = self.get_output_path(
            work_directory=work_directory,
            input_path=data[0],
            **metadata
        )

        # TODO: Thread
        for path in data:
            with netcdf.load(path) as input_data:
                if attributes is None:
                    attributes = input_data.attrs.copy()
                if encoding is None:
                    encoding = input_data.encoding.copy()

                for variable_name, calculators in calculations.items():
                    if variable_name not in input_data:
                        raise KeyError(f"Variable '{variable_name}' not found in '{path}'. Data cannot be streamed")

                    variable: xarray.DataArray = input_data[variable_name]
                    for calculator in calculators:
                        calculator.update(variable=variable)

                if path == data[-1]:
                    for calculator_group in calculations.values():
                        for calculator in calculator_group:
                            input_data[calculator.name] = calculator.gather()
                            input_data[calculator.name].encoding.update(calculator.encoding)

                    output_path: pathlib.Path = self.get_output_path(
                        work_directory=work_directory,
                        input_path=path,
                        **metadata
                    )

                    LOGGER.debug(f"Updated values from {path}")

                    netcdf.write(
                        dataset=input_data,
                        target=output_path,
                    )
        return [output_path]


    def __hash__(self):
        try:
            parent_hash: int = super().__hash__()
        except:
            parent_hash = 0

        return hash((
            parent_hash,
            *map(hash, self.streams)
        ))


    streams: list[StreamCalculation] = dataclasses.field()


def get_stream_calculators(
    root: typing.Type[OnlineCalculation] = OnlineCalculation
) -> dict[OnlineCalculationType, typing.Type[OnlineCalculation]]:
    """
    Get all the concrete operation types

    :param root: The base object whose concrete subclasses to look for
    :returns: All non-abstract implementations of the root OnlineCalculation
    """
    # DO NOT DELETE! This will bring in all operation types that need to be considered
    import post_processing.operations

    subclasses: dict[typing.Optional[OnlineCalculationType], typing.Type[OnlineCalculation]] = {
        subclass.operation_type(): subclass
        for subclass in root.__subclasses__()
    }

    immediate_subclasses: generic.Sequence[typing.Type[OnlineCalculation]] = list(subclasses.values())

    for subclass in immediate_subclasses:
        sub_subclasses: dict[OnlineCalculationType, typing.Type[OnlineCalculation]] = get_stream_calculators(
            root=subclass
        )
        preexisting_operations: list[tuple[OnlineCalculationType, typing.Type[OnlineCalculation], typing.Type[OnlineCalculation]]] = []

        for operation_type, operation_class in sub_subclasses.items():
            conflicting_operation: typing.Type[OnlineCalculation] = subclasses.get(operation_type)
            if conflicting_operation is not None:
                preexisting_operations.append((operation_type, operation_class, conflicting_operation))

        if preexisting_operations:
            conflicting_type_messages: list[str] = [
                f"{operation_type}: {conflicting_type.__qualname__} vs {preexisting_type.__qualname__}"
                for operation_type, conflicting_type, preexisting_type in preexisting_operations
            ]
            message = (
                f"Cannot load in Profile Operation Types - there are conflicts on the following types and there "
                f"can only be one OnlineCalculation class per operation type: {', '.join(conflicting_type_messages)}"
            )
            raise KeyError(message)

        subclasses.update(sub_subclasses)

    subclasses = {
        operation_type: subclass
        for operation_type, subclass in subclasses.items()
        if operation_type is not None
           and subclass is not None
    }
    return subclasses
