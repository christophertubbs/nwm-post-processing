#!/usr/bin/env python3
"""
Classes used to define what output netcdf files should look like
"""
import os
import typing
import dataclasses
import logging
import pathlib
import json
import multiprocessing
import multiprocessing.pool

from datetime import datetime

import numpy
import xarray
from pandas.core.dtypes.common import is_integer_dtype

import post_processing.enums
from post_processing import nco
from post_processing.nwm_file import NWMFile
from post_processing.configuration import settings

T = typing.TypeVar("T")
"""A generic type"""


MAXIMUM_CPUS: int = os.cpu_count() // 2

LOGGER: logging.Logger = logging.getLogger(pathlib.Path(__file__).stem)


def xarray_dtype_from_str(dtype_str: str) -> numpy.dtype:
    """
    Map a string like 'int', 'float', 'double', 'char' to a NumPy dtype for xarray.

    Args:
        dtype_str (str): String type name.

    Returns:
        numpy.dtype: Corresponding NumPy dtype.

    Raises:
        ValueError: If inumpyut is not recognized.
    """
    mapping = {
        "byte": numpy.int8,
        "ubyte": numpy.uint8,
        "short": numpy.int16,
        "ushort": numpy.uint16,
        "int": numpy.int32,
        "uint": numpy.uint32,
        "int64": numpy.int64,
        "uint64": numpy.uint64,
        "float": numpy.float32,
        "double": numpy.float64,
        "char": numpy.dtype("S"),
        "string": numpy.dtype("S"),  # variable-length strings in NetCDF3
        "str": numpy.dtype("S"),  # variable-length strings in NetCDF3
        "bool": numpy.bool_,
        "datetime64": numpy.datetime64,
        "datetime64[ns]": numpy.datetime64,
        "datetime64[ms]": numpy.datetime64
    }

    key = dtype_str.lower().strip()
    if key in mapping:
        return numpy.dtype(mapping[key])
    raise ValueError(f"Unsupported data type string: '{dtype_str}'")


def deserialize(cls: typing.Type[T], data: typing.Union[str, pathlib.Path, typing.Dict]) -> T:
    """
    Convert a path or dictionary to a dataclass

    :param cls: The type of class to deserialize into
    :param data: The data or path to deserialize
    :return: The deserialized class
    """
    if isinstance(data, str):
        data = pathlib.Path(data)

    if isinstance(data, pathlib.Path):
        data = json.loads(data.read_text())

    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")
    
    initialization_kwargs: typing.Dict = {}

    for field in dataclasses.fields(cls):
        if field.name not in data:
            continue

        value = data[field.name]

        dataclass_type_args: typing.List[typing.Type[T]] = [
            inner_type
            for inner_type in typing.get_args(field.type)
            if dataclasses.is_dataclass(inner_type)
        ]
        type_origin: typing.Optional[typing.Type] = typing.get_origin(field.type)

        if dataclasses.is_dataclass(field.type) and isinstance(value, dict):
            initialization_kwargs[field.name] = deserialize(field.type, value)
        elif isinstance(value, typing.Iterable) and not isinstance(value, str) and issubclass(type_origin, typing.Sequence) and dataclass_type_args:
            initialization_kwargs[field.name] = []

            for entry_index, entry in enumerate(value):
                acceptable_value = entry
                if isinstance(entry, dict):
                    for possible_type in dataclass_type_args:
                        try:
                            acceptable_value = deserialize(possible_type, entry)
                            break
                        except:
                            LOGGER.debug(f'"{entry}" could not be deserialized as a {possible_type}')
                initialization_kwargs[field.name].append(acceptable_value)

        else:
            initialization_kwargs[field.name] = value

    return cls(**initialization_kwargs)


@dataclasses.dataclass
class Dimension:
    """
    Represents a Netcdf Dimension
    """
    name: str
    """The name of the dimension"""
    size: int
    """The length of the dimension"""
    unlimited: bool = dataclasses.field(default=False)
    """Whether this is an unlimited/record dimension"""


@dataclasses.dataclass
class Variable:
    """
    Represents a Netcdf Variable
    """
    name: str
    """The name of the variable"""
    datatype: str
    """The type of data stored ('char', 'int', float', etc)"""
    dimensions: typing.List[Dimension] = dataclasses.field(default_factory=list)
    """The names of dimensions and their expected lengths in indexing order"""
    attributes: typing.Dict[str, typing.Union[str, int, typing.List[typing.Union[str, int]]]] = dataclasses.field(default_factory=dict)
    """Key value pairs describing the data within the variable"""
    encoding: typing.Dict[str, typing.Union[str, int, typing.List[typing.Union[str, int]]]] = dataclasses.field(default_factory=dict)
    """Key value pairs describing how the data should be stored"""
    coordinates: typing.List[str] = dataclasses.field(default_factory=list)
    """The names of coordinate variables that this relies upon"""

    @classmethod
    def from_data_array(cls, array: xarray.DataArray) -> "Variable":
        """
        Create a variable specification from a data array

        :param array: The data array to mimic in specification form
        :returns: A new variable specification from the given data array
        """
        name: str = str(array.name) if array.name is not None else 'array'
        attributes: typing.Dict[str, typing.Any] = {
            key: value.tolist() if isinstance(value, numpy.ndarray) else value
            for key, value in array.attrs.items()
            if key != 'source'
        }
        datatype: str = array.dtype.str
        dimensions: typing.List[Dimension] = [
            Dimension(name=str(key), size=value)
            for key, value in array.sizes.items()
        ]
        coordinates: typing.List[str] = list(map(str, array.sizes.keys()))
        encoding: typing.Dict[str, typing.Any] = {
            key: value.tolist() if isinstance(value, numpy.ndarray) else value
            for key, value in array.encoding.items()
            if key != 'source'
        }
        return cls(
            name=name,
            attributes=attributes,
            datatype=datatype,
            dimensions=dimensions,
            coordinates=coordinates,
            encoding=encoding,
        )

    def generate_raw_data(self, step: int, max_steps: int) -> numpy.ndarray:
        """
        Generate arbitrary data
        """
        jitter_fraction: float = 0.05
        data_type: numpy.dtype = xarray_dtype_from_str(self.datatype)

        shape: typing.Tuple[int, ...] = tuple(dimension.size for dimension in self.dimensions)

        if shape == tuple():
            return data_type.type()

        is_dimension: bool = len(shape) == 1 and self.dimensions[0].name == self.name

        if 'valid_range' in self.attributes:
            absolute_low_bound = int(self.attributes['valid_range'][0])
            absolute_high_bound = int(self.attributes['valid_range'][1])
        elif 'valid_min' in self.attributes and 'valid_max' in self.attributes:
            absolute_low_bound = int(self.attributes['valid_min'])
            absolute_high_bound = int(self.attributes['valid_max'])
        else:
            absolute_low_bound = 0 if is_dimension else 200
            absolute_high_bound = shape[0] if is_dimension else 8000

        if len(shape) == 1 and self.dimensions[0].name == self.name:
            # This needs to be sequential values as this the set of values for a dimension
            count: int = shape[0]

            # If there is only a single value, there's a good chance this is a record variable. Just choose the one that is in the correct step in the range
            if count == 1:
                absolute_range: numpy.ndarray = numpy.linspace(absolute_low_bound, absolute_high_bound, num=max_steps, dtype=data_type)
                return absolute_range[step:step + 1]
            else:
                values: numpy.ndarray = numpy.linspace(absolute_low_bound, absolute_high_bound, num=shape[0], dtype=data_type)
                values = values.astype(data_type)
                return values

        random_number_generator: numpy.random.Generator = numpy.random.default_rng(abs(hash((self.name, self.datatype))))

        initial_range: int = absolute_high_bound - absolute_low_bound
        fourth = initial_range / 4.0
        fourth_range = fourth * 0.8
        low_midpoint = absolute_low_bound + fourth
        high_midpoint = absolute_high_bound - fourth

        if numpy.issubdtype(data_type, numpy.number):
            if numpy.issubdtype(data_type, numpy.integer):
                generator_function = random_number_generator.integers
            elif numpy.issubdtype(data_type, numpy.floating):
                generator_function = random_number_generator.uniform
            else:
                raise NotImplementedError(f"The generation of '{data_type}' numbers is not implemented.")

            low_range: numpy.ndarray = generator_function(
                low=low_midpoint - fourth_range,
                high=low_midpoint + fourth_range,
                size=shape
            )
            high_range: numpy.ndarray = generator_function(
                low=high_midpoint - fourth_range,
                high=high_midpoint + fourth_range,
                size=shape
            )

            raw_data: numpy.ndarray = generator_function(low_range, high_range)
            span: numpy.ndarray = high_range - low_range

            for _ in range(step):
                jitter: numpy.ndarray = random_number_generator.uniform(-jitter_fraction, jitter_fraction, size=shape) * span
                jitter = jitter.astype(dtype=data_type)
                raw_data += jitter
                raw_data = numpy.clip(raw_data, low_range, high_range)

            raw_data = raw_data.astype(dtype=data_type)
            return raw_data

        if numpy.issubdtype(data_type, numpy.bool_):
            return random_number_generator.choice([numpy.True_, numpy.False_], size=shape)

        if numpy.issubdtype(data_type, numpy.bytes_):
            raise NotImplementedError(f"Random byte generation has not been implemented yet")

        if numpy.issubdtype(data_type, numpy.datetime64):
            raise NotImplementedError(f"Random Datetime generation has not been implemented yet")

        return numpy.empty(shape, dtype=data_type)


    def generate_array(
        self,
        coordinates: typing.Mapping[str, typing.Sequence],
        step: int,
        max_steps: int
    ) -> xarray.DataArray:
        """
        Generate a data array representing this variable
        """
        LOGGER.info(f"Generating the data for {self.name} ({self.datatype})")

        raw_data = self.generate_raw_data(step=step, max_steps=max_steps)
        array: xarray.DataArray = xarray.DataArray(
            data=raw_data,
            name=self.name,
            attrs={
                key: shrink_value(value=value)
                for key, value in self.attributes.items()
            },
            coords={
                coordinate_name: coordinate
                for coordinate_name, coordinate in coordinates.items()
                if coordinate_name in self.coordinates
            } or None,
            dims=list(map(lambda dim: dim.name, self.dimensions)) or tuple(),
        )

        array.encoding.update(self.encoding)

        return array

@dataclasses.dataclass
class RouteLink:
    """

    """
    dimension: str = dataclasses.field(default="feature_id")
    from_column_name: str = dataclasses.field(default="from")
    to_column_name: str = dataclasses.field(default="to")
    seed: int = dataclasses.field(default=12345)
    none_ratio: float = dataclasses.field(default=0.25)
    none_value: int = dataclasses.field(default=-1)

@dataclasses.dataclass
class Threshold:
    """

    """
    variable_to_threshold: str = dataclasses.field(default="streamflow")
    percentiles: typing.List[float] = dataclasses.field(default_factory=lambda: [0.1, 0.25, 0.5, 0.75, 0.9, 0.95])
    feature: str = dataclasses.field(default="feature_id")
    time: str = dataclasses.field(default="time")
    seed: int = dataclasses.field(default=12345)

@dataclasses.dataclass
class Dataset:
    """
    Represents a Post Processing Output Netcdf
    """
    dimensions: typing.List[Dimension]
    """A list of all dimensions within this dataset"""
    coordinates: typing.List[Variable]
    """The variables marked as coordinates within this dataset"""
    variables: typing.List[Variable]
    """The variables stored within this dataset"""
    attributes: typing.Dict[str, typing.Union[int, str]]
    """Global Attributes for this dataset"""
    configuration: post_processing.enums.Configuration
    """What configuration this output came from"""
    model_output_type: post_processing.enums.ModelOutputType
    """The type of model output that this data represents"""
    region: typing.Union[post_processing.enums.Region, post_processing.enums.RFC]
    """The region over which this data is valid"""
    mask_names: typing.List[str]
    """Names for mask files to use for testing"""
    mask_coordinate: str
    """The name of the coordinate to mask off of"""
    thresholds: Threshold
    """Information on how to use thresholds to determine anomaly levels"""
    member: typing.Optional[int] = dataclasses.field(default=None)
    """The ensemble member that this dataset reflects"""
    mask_seed: int = dataclasses.field(default=123456)
    """A seed number to use for generating consistent partitions for masks"""
    routelink: RouteLink = dataclasses.field(default_factory=RouteLink)
    """Specifications for how to generate a routelink"""


    def __str__(self):
        return (
            f"Dataset: "
            f"{self.configuration}."
            f"{self.model_output_type}{'_' + str(self.member) if self.member is not None else ''}."
            f"{self.region}"
        )

    @classmethod
    def from_netcdf(cls, path: pathlib.Path) -> "Dataset":
        """
        Read a netcdf file and convert it into a Dataset specification

        :param path: Path to the netcdf file
        :returns: Dataset specification
        """
        if not path.is_file():
            raise FileNotFoundError(f"File {path} not found - cannot create dataset specification")

        data: xarray.Dataset = xarray.open_dataset(path)
        dimensions: typing.List[Dimension] = list(Dimension(name=str(key), size=value) for key, value in data.sizes.items())
        attributes: typing.Dict[str, typing.Any] = {
            key: value.tolist() if isinstance(value, numpy.ndarray) else value
            for key, value in data.encoding.items()
            if key != 'source'
        }
        unlimited_dimensions: typing.Set[str] = data.encoding.get("unlimited_dims", set())

        for unlimited_dimension in unlimited_dimensions:
            matching_dimension: typing.Optional[Dimension] = next(
                (dimension for dimension in dimensions if dimension.name == unlimited_dimension),
                None
            )
            if matching_dimension is not None:
                matching_dimension.unlimited = True

        coordinates: typing.List[Variable] = [
            Variable.from_data_array(array=array)
            for array in data.coords.values()
        ]

        variables: typing.List[Variable] = [
            Variable.from_data_array(array=array)
            for array in data.data_vars.values()
        ]

        file_data: NWMFile = NWMFile.parse(path=path)

        return cls(
            dimensions=dimensions,
            coordinates=coordinates,
            variables=variables,
            attributes=attributes,
            configuration=file_data.configuration,
            model_output_type=file_data.model_output_type,
            region=file_data.region,
            member=file_data.member,
            mask_names=[
                "abrfc.nc",
                "aprfc.nc",
                "cbrfc.nc",
                "cnrfc.nc",
                "lmrfc.nc",
                "marfc.nc",
                "mbrfc.nc",
                "ncrfc.nc",
                "nerfc.nc",
                "nwrfc.nc",
                "ohrfc.nc",
                "serfc.nc",
                "wgrfc.nc"
            ],
            mask_coordinate=next(
                (dimension.name for dimension in dimensions if dimension.unlimited),
                coordinates[0].name
            ),
            thresholds=Threshold()
        )

    def get_filenames(
        self,
        data_path: pathlib.Path,
        cycle: int = 0,
        length: int = 18,
        step: int = 1
    ) -> typing.List[pathlib.Path]:
        filenames: typing.List[str] = [
            (
                f"nwm."
                f"t{str(cycle).zfill(2)}z."
                f"{self.configuration}."
                f"{self.model_output_type}{'_' + str(self.member) if self.member else ''}."
                f"f{str(frame_number).zfill(6 if self.region == post_processing.enums.Region.Hawaii else 3)}."
                f"{self.region}."
                f"nc"
            )
            for frame_number in range(step, step * length + 1, step)
        ]

        output_paths: typing.List[pathlib.Path] = [
            data_path / filename
            for filename in filenames
        ]
        return output_paths

    def generate_routelink(
        self,
        sample_path: pathlib.Path,
        output_path: pathlib.Path,
        none_ratio: float = 0.75,
        none_value: int = -1,
        from_column_name: str = "from",
        to_column_name: str = "to",
        dimension: str = "feature_id",
        seed: int = 12345,
    ) -> pathlib.Path:
        """
        Generate a routelink file for the generated netcdf data

        :param sample_path: The path to a sample of the generated data
        :param output_path: Where to put the routelink
        :param none_ratio: What percentage of features in the routelink should point to nothing
        :param none_value: The value to use to indicate that the feature doesn't lead to anything
        :param from_column_name: The column name to use the indicates the primary id that might point to another 'from_column_name' value
        :param to_column_name: The name of the column that shows what id that the 'from_column_name' value points to
        :param dimension: The name of the dimension used as the index for these values. This should match up with the primary dimension of the sample
        :param seed: A seed for the random number generator
        :returns: The path to the generated routelink file
        """
        if output_path.is_file():
            LOGGER.info(f"There appears to already be a routelink at {output_path} - nothing needs to be generated")
            return output_path

        if not sample_path.is_file():
            raise FileNotFoundError(f"No sample data could be found at {sample_path}")

        if none_ratio <= 0 or none_ratio >= 1:
            raise ValueError(f"The `none_ratio` must be within the bounds of (0, 1). Received: {none_ratio}")

        generator: numpy.random.Generator = numpy.random.default_rng(seed=seed)
        sample_data: xarray.Dataset = xarray.open_dataset(sample_path, chunks={}, engine="h5netcdf")
        features: numpy.array = sample_data[dimension].values
        number_of_nones: int = int(len(features) * none_ratio)
        number_of_values: int = len(features) - number_of_nones

        chosen_values: numpy.typing.NDArray[int] = generator.choice(features, size=number_of_values, replace=True)

        to_array: numpy.ndarray = numpy.empty(len(features), dtype=features.dtype)
        to_array[:number_of_nones] = none_value
        to_array[number_of_nones:] = chosen_values
        generator.shuffle(to_array)

        routelink: xarray.Dataset = xarray.Dataset(
            {
                from_column_name: (dimension, features),
                to_column_name: (dimension, to_array),
            },
            attrs={
                "generated_for": f"RouteLink for {self}",
                "generated_on": datetime.now().astimezone().strftime(settings.date_format)
            }
        )
        routelink.to_netcdf(output_path)
        return output_path

    """
    def generate_thresholds(
        self,
        data_path: pathlib.Path,
        threshold_directory: pathlib.Path,
        variable_to_threshold: str,
        percentiles: typing.List[float] = None,
        time_dimension: str = 'time',
        feature_dimension: str = 'feature_id',
        cycle: int = 0,
        length: int = 18,
        step: int = 1,
        seed: int = 12345
    ) -> typing.Sequence[pathlib.Path]:
        \"""
        Generate a threshold file for each passed in percentile

        :param data_path: The path to where all generated data lies
        :param threshold_directory: The directory to write thresholds to
        :param variable_to_threshold: The variable to base thresholds off of
        :param percentiles: The list of percentiles to use
        :param time_dimension: The time dimension to use
        :param feature_dimension: The feature dimension to use
        :param cycle: The cycle of simulated data being mocked
        :param length: The number of files in the cycle
        :param step: The amount of hours between each data file
        :param seed: The seed for the random number generator
        :returns: The paths to all generated threshold files
        \"""
        paths_to_data: typing.Sequence[pathlib.Path] = self.get_filenames(
            data_path=data_path,
            cycle=cycle,
            length=length,
            step=step,
        )

        percentiles = {
            percentile if percentile < 1 else percentile / 100.0
            for percentile in percentiles
        }

        import xarray
        import numpy
        from post_processing.utilities import netcdf

        if any(percentile > 1 or percentile < 0 for percentile in percentiles):
            raise ValueError(f"percentiles must be between 0 and 1, received: {percentiles}")

        percentile_paths: typing.Dict[float, pathlib.Path] = {
            percentile: threshold_directory / f"p{int(percentile * 100)}.nc"
            for percentile in percentiles
        }

        overall_data: xarray.Dataset = netcdf.load(target=paths_to_data)
        variable: xarray.DataArray = overall_data[variable_to_threshold]
        minimum: numpy.float64 = variable.min().values.min()
        maximum: numpy.float64 = variable.max().values.max()
        range_of_values: numpy.float64 = maximum - minimum
        noise_scale: numpy.float64 = range_of_values * 0.005
        random_number_generator: numpy.random.Generator = numpy.random.default_rng(seed)
        days_in_year: int = 366

        output_paths: typing.List[pathlib.Path] = []
        for percentile in percentiles:
            path = percentile_paths[percentile]
            if path.is_file():
                LOGGER.info(f"The threshold file for the {int(percentile * 100)}th percentile already exists at {path}")
                output_paths.append(path)
                continue
            LOGGER.info(f"Generating thresholds for the {int(percentile * 100)}th percentile for {self}")
            threshold_name: str = f"p{str(int(percentile * 100)).zfill(2)}"
            threshold_variable_name: str = f"{variable_to_threshold}_{threshold_name}"
            quantile: numpy.ndarray = variable.quantile(percentile, dim=time_dimension).values
            length: int = variable[feature_dimension].size
            full_year: numpy.ndarray = random_number_generator.normal(
                loc=quantile,
                scale=noise_scale,
                size=(days_in_year, length)
            )

            # Transpose so that it's locations first, time last
            full_year = full_year.transpose()

            percentile_dataset: xarray.Dataset = xarray.Dataset(
                data_vars={
                    threshold_variable_name: xarray.DataArray(
                        data=full_year,
                        dims=[feature_dimension, time_dimension],
                        name=threshold_variable_name
                    ),
                },
                coords={
                    feature_dimension: overall_data[feature_dimension].compute().copy(),
                    time_dimension: numpy.arange(1, days_in_year + 1)
                },
                attrs={
                    "TITLE": f"Test thresholds for {threshold_name}",
                    "seed": seed,
                    "cycle": cycle,
                    "length": length,
                    "step": step,
                    "script": str(pathlib.Path(__file__).name),
                    "created_by": os.environ.get("USER", "Unknown"),
                    "created_on": datetime.now().astimezone().strftime(settings.date_format),
                }
            )
            output_path: pathlib.Path = threshold_directory / f"{threshold_name}.nc"

            threshold_directory.mkdir(parents=True, exist_ok=True)

            netcdf.write(target=output_path, dataset=percentile_dataset)
            LOGGER.info(f"Saved the threshold dataset for the {int(percentile * 100)}th percentile to {output_path}")
            output_paths.append(output_path)

        missing_locations = list(filter(lambda pth: not pth.exists(), output_paths))
        if missing_locations:
            raise FileNotFoundError(
                f"For some reason, the following files are no longer available: {missing_locations}"
            )
        if len(output_paths) != len(percentiles):
            raise RuntimeError(
                f"Files for percentiles are missing. {len(output_paths)} files generated vs "
                f"{len(percentiles)} files requested."
            )

        return output_paths
"""


    def generate_masks(
        self,
        data_path: pathlib.Path,
        mask_directory: pathlib.Path,
        cycle: int = 0,
        length: int = 18,
        step: int = 1
    ) -> typing.Sequence[pathlib.Path]:
        """
        Generate a mask for each configured mask name to use for data subsetting tests

        :param data_path: The directory where pregenerated test data is stored
        :param mask_directory: Where to store the masks
        :param cycle: Which cycle to use
        :param length: The length of the 'forecast' that this mask is for
        :param step: The step between each file in the 'forecast' that this mask is for
        :returns: The path to each mask file
        """
        generated_data_paths: typing.Sequence[pathlib.Path] = self.get_filenames(
            data_path=data_path,
            cycle=cycle,
            length=length,
            step=step
        )

        if not generated_data_paths or not generated_data_paths[0].is_file():
            raise Exception(
                f"Data has not been generated for {generated_data_paths[0]} - "
                f"data must be generated before generating masks"
            )

        partition_count: int = len(self.mask_names)
        mask_paths: typing.List[pathlib.Path] = [
            mask_directory / mask_name
            for mask_name in self.mask_names
        ]

        if all(mask_path.exists() for mask_path in mask_paths):
            LOGGER.info(f"Masks for {self} already exists in {mask_directory}")
            return mask_paths

        import xarray
        import numpy

        random_number_generator: numpy.random.Generator = numpy.random.default_rng(self.mask_seed)
        first_dataset: xarray.Dataset = xarray.open_dataset(generated_data_paths[0], chunks={})
        coordinate_values: numpy.ndarray = first_dataset[self.mask_coordinate].values.flatten()
        indices: numpy.ndarray = numpy.arange(len(coordinate_values))
        random_number_generator.shuffle(indices)
        partitions: typing.List[numpy.ndarray] = numpy.array_split(indices, partition_count)

        for partition_path, partition in zip(mask_paths, partitions):
            partition_coordinates: numpy.ndarray = coordinate_values[partition]
            mask_dataset: xarray.Dataset = xarray.Dataset(
                {
                    self.mask_coordinate: (self.mask_coordinate, partition_coordinates)
                },
                attrs={
                    "generated_on": datetime.now().astimezone().strftime(settings.date_format),
                    "mask_name": partition_path.stem,
                    "created_from": generated_data_paths[0].name,
                }
            )
            mask_dataset.to_netcdf(path=partition_path)

        return mask_paths

    def generate_netcdf(
        self,
        output_directory: pathlib.Path,
        cycle: int = 0,
        length: int = 18,
        step: int = 1,
        overwrite: bool = False
    ) -> typing.Sequence[pathlib.Path]:
        """
        Generate a series of netcdf files based off of this configuration

        This won't accurately model Hawaii

        :param output_directory: Where to save the files
        :param cycle: Which cycle the dataset will belong to
        :param length: How many frames should be in the dataset
        :param step: The number of hours between each frame
        :param overwrite: Whether to overwrite preexisting files
        :returns: The paths to the newly created files
        """

        output_paths: typing.List[pathlib.Path] = self.get_filenames(
            data_path=output_directory,
            cycle=cycle,
            length=length,
            step=step
        )

        if all(map(lambda path: path.exists(), output_paths)) and not overwrite:
            LOGGER.info(f"Data for {self} already exists in {output_directory}")
            return output_paths

        LOGGER.info(f"Generating data for {len(output_paths)} files")
        coordinate_values: typing.Dict[str, typing.Union[typing.Sequence[xarray.DataArray], xarray.DataArray]] = {}

        for coordinate in self.coordinates:
            LOGGER.info(f"Generating coordinate information for {coordinate.name}")
            data_type = xarray_dtype_from_str(coordinate.datatype)
            if 'time' in coordinate.name:
                import pandas
                if coordinate.name == 'time':
                    values: typing.Sequence[typing.Sequence[int]] = [
                        [int(date.timestamp()) // 60]
                        for date in pandas.date_range(pandas.Timestamp.today().date(), periods=length, freq=f"{step}h")
                    ]
                else:
                    values: typing.Sequence[int] = [
                        int(date.timestamp()) // 60
                        for date in pandas.date_range(pandas.Timestamp.today().date(), periods=coordinate.dimensions[0].size, freq="h")
                    ]
            else:
                if not is_integer_dtype(data_type):
                    raise ValueError(
                        f"The data type for the '{coordinate.name}' coordinate must be an integer, but received '{data_type}'."
                    )
                values: typing.Sequence[int] = numpy.linspace(
                    1,
                    coordinate.dimensions[0].size,
                    coordinate.dimensions[0].size,
                    dtype=data_type
                )

            if isinstance(values[-1], typing.Sequence):
                coordinate_values[coordinate.name] = [
                    xarray.DataArray(
                        data=inner_values,
                        name=coordinate.name,
                        attrs=coordinate.attributes,
                        dims=[dimension.name for dimension in coordinate.dimensions],
                    )
                    for inner_values in values
                ]
            else:
                coordinate_values[coordinate.name] = xarray.DataArray(
                    data=values,
                    name=coordinate.name,
                    attrs=coordinate.attributes,
                    dims=[dimension.name for dimension in coordinate.dimensions],
                )

        dataset_files: typing.List[pathlib.Path] = []
        output_directory.mkdir(parents=True, exist_ok=True)

        keyword_arguments: typing.Sequence[typing.Dict[str, typing.Any]] = [
            {
                "filename": output_path,
                "coordinates": {
                    coordinate_name: coordinates if isinstance(coordinates, xarray.DataArray) else coordinates[file_index]
                    for coordinate_name, coordinates in coordinate_values.items()
                },
                "variable_definitions": self.variables,
                "global_attributes": self.attributes,
                "unlimited_dimensions": list(dimension.name for dimension in self.dimensions if dimension.unlimited),
                "step": file_index,
                "max_steps": len(output_paths),
                "overwrite": overwrite
            }
            for file_index, output_path in enumerate(output_paths)
        ]

        with multiprocessing.Pool(processes=MAXIMUM_CPUS) as pool:
            future_paths: typing.List[multiprocessing.pool.AsyncResult[pathlib.Path]] = [
                pool.apply_async(
                    func=write_file,
                    kwds=kwargs
                )
                for kwargs in keyword_arguments
            ]

            while future_paths:
                future: multiprocessing.pool.AsyncResult[pathlib.Path] = future_paths.pop(0)
                try:
                    result: pathlib.Path = future.get(timeout=1)
                    dataset_files.append(result)
                except multiprocessing.TimeoutError:
                    future_paths.append(future)


        dataset_files = sorted(dataset_files, key=lambda path: path.stem)
        return dataset_files


def shrink_value(value: typing.Any) -> typing.Any:
    """
    Reduce the size of an input value if necessary

    :param value: The value whose type to shrink
    :returns: The shrunken value, properly typed if necessary
    """
    if isinstance(value, int):
        if -2_147_483_648 <= value <= 2_147_483_647:
            return numpy.int32(value)
        return numpy.int64(value)
    if isinstance(value, float):
        return numpy.float64(value)
    return value

def write_file(
    filename: pathlib.Path,
    coordinates: typing.Dict[str, xarray.DataArray],
    variable_definitions: typing.Sequence[Variable],
    global_attributes: typing.Dict[str, typing.Any],
    unlimited_dimensions: typing.Union[str, typing.Sequence[str]],
    step: int,
    max_steps: int,
    overwrite: bool = False
) -> pathlib.Path:
    """
    Generate fake data based on components from an output specification

    :param filename: Where to put the generated data
    :param coordinates: Common coordinate data
    :param variable_definitions: The definitions for what variables should be included
    :param global_attributes: Attributes that should be present on the global scope
    :param unlimited_dimensions: One or more dimensions that should be considered as being unlimited, i.e. the record dimension
    :param step: The step that this file represents in the order of generation
    :param max_steps: The maximum amount of steps being created. This will show {step} out of {max_steps}
    :param overwrite: Whether to overwrite the data if it already exists
    :returns: The path to the generated data
    """
    if filename.exists() and not overwrite:
        LOGGER.debug(f"Data already exists for {filename} - reusing that")
        return filename

    LOGGER.debug(f"Generating data for {filename}")

    global_attributes = {
        attribute_name: shrink_value(attribute_value)
        for attribute_name, attribute_value in global_attributes.items()
    }

    variables: typing.Dict[str, xarray.DataArray] = {}

    for variable in variable_definitions:
        variables[variable.name] = variable.generate_array(coordinates=coordinates, step=step, max_steps=max_steps)

    dataset: xarray.Dataset = xarray.Dataset(
        data_vars=variables,
        coords=coordinates,
        attrs=global_attributes,
    )

    if isinstance(unlimited_dimensions, str):
        unlimited_dimensions = [unlimited_dimensions]

    try:
        dataset.to_netcdf(path=filename, unlimited_dims=unlimited_dimensions)
    except:
        LOGGER.error(f"Failed to write data to {filename}. Data:{os.linesep}{dataset}")
        raise
    header: str = nco.get_header(filename)
    LOGGER.debug(f"Saved dataset to {filename}:{os.linesep}{header}")
    return filename
