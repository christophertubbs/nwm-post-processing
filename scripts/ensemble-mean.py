#!/usr/bin/env python3
"""
Create an ensemble mean product of selected post-processed output
"""
import os
import typing
import collections.abc as generic
import argparse
import dataclasses
import sys
import pathlib
import re

import xarray
import numpy

from post_processing.utilities import logging

LOGGER: logging.Logger = logging.get_logger(pathlib.Path(__file__))

OUTPUT_NAME_PATTERN: typing.Final[re.Pattern[str]] = re.compile(
    r'nwm\.'
    r't(?P<cycle>[0-2]\d)z\.'
    r'(?P<configuration>[^.]+)\.'
    r'(?P<output_type>channel_rt|land|forcing|reservoir(\.full)?)_(?P<member>\d)\.'
    r'((f(?P<frame>\d+)|tm(?P<tminus>\d+))\.)?'
    r'(?P<region>[a-z]+(\.\w\wrfc)?)\.'
    r'(nc(df)?|hdf)$'
)
"""
The pattern for what a post-processing output's name should look like. 
This is not reliable as profiles may dictate their own naming schemes.

This needs to be refactored to handle the edge cases
"""


@dataclasses.dataclass
class VariableInformation:
    """
    General information about a variable in a netcdf file
    """
    name: str
    dimensions: tuple[int, ...]
    attributes: dict[str, typing.Any]
    encoding: dict[str, typing.Any]
    is_combinable: bool
    """Whether the data in this variable is valid for combining. Integers, strings, dates, etc are not valid - only floats are"""

    def __hash__(self) -> int:
        items: tuple[generic.Hashable, ...] = tuple([
            self.name,
            *self.dimensions,
            *self.attributes.keys()
        ])

        return hash(items)

    @classmethod
    def from_data_array(cls, variable: xarray.DataArray) -> "VariableInformation":
        return cls(
            name=str(variable.name),
            dimensions=tuple([int(size) for size in variable.sizes.values()]),
            attributes=variable.attrs.copy(),
            encoding=variable.encoding.copy(),
            is_combinable=numpy.issubdtype(variable.dtype, numpy.floating)
        )

    @classmethod
    def from_dataset(cls, dataset: xarray.Dataset) -> list["VariableInformation"]:
        return [
            cls.from_data_array(variable)
            for name, variable in dataset.data_vars.items()
        ]

@dataclasses.dataclass
class CoordinateInformation:
    """
    General information about a coordinate in a netcdf file
    """
    name: str
    dimensions: tuple[int, ...]
    attributes: dict[str, typing.Any]
    encoding: dict[str, typing.Any]
    datatype: type

    def __hash__(self) -> int:
        items: tuple[generic.Hashable, ...] = tuple([
            self.name,
            *self.dimensions,
            *self.attributes.keys()
        ])

        return hash(items)

    @classmethod
    def from_data_array(cls, variable: xarray.Dataset) -> "CoordinateInformation":
        return cls(
            name=str(variable.name),
            dimensions=tuple([int(size) for size in variable.sizes.values()]),
            attributes=variable.attrs.copy(),
            encoding=variable.encoding.copy(),
            datatype=variable.dtype
        )

    @classmethod
    def from_dataset(cls, dataset: xarray.Dataset) -> list["CoordinateInformation"]:
        return [
            cls.from_data_array(array)
            for array in dataset.coords.values()
        ]



@dataclasses.dataclass
class DatasetInformation:
    """
    General metadata about a netcdf file
    """
    coordinates: list[CoordinateInformation]
    variables: list[VariableInformation]
    attributes: dict[str, typing.Any]
    dimensions: dict[str, int]

    @classmethod
    def from_path(cls, path: pathlib.Path) -> "DatasetInformation":
        LOGGER.debug(f"Loading '{path}'")
        with xarray.open_dataset(path) as dataset:
            coordinates: list[CoordinateInformation] = CoordinateInformation.from_dataset(
                dataset=dataset
            )
            variables: list[VariableInformation] = VariableInformation.from_dataset(
                dataset=dataset
            )
            dimensions: dict[str, int] = {str(name): size for name, size in dataset.sizes.items()}
            attributes: dict[str, typing.Any] = dataset.attrs.copy()
            return cls(coordinates=coordinates, variables=variables, attributes=attributes, dimensions=dimensions)

    def __hash__(self):
        items: tuple[generic.Hashable, ...] = tuple([
            *self.coordinates,
            *self.variables,
            *self.attributes.keys(),
            *self.dimensions.items()
        ])

        return hash(items)


@dataclasses.dataclass
class Arguments:
    """
    Input parameters for the application
    """
    input_path: pathlib.Path
    output_path: pathlib.Path
    expected: int
    overwrite: bool = dataclasses.field(default=False)
    allow_overcount: bool = dataclasses.field(default=False)

    @classmethod
    def create_parser(cls) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = argparse.ArgumentParser(
            description=__doc__
        )
        parser.add_argument(
            "input_path",
            type=pathlib.Path,
            help="The path to one of the input files to be aggregated"
        )
        parser.add_argument(
            "output_path",
            type=pathlib.Path,
            help="Where to put the generated file. Must be the path to a file"
        )
        parser.add_argument(
            "expected",
            type=int,
            help="The number of ensemble members that needs to be available"
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Whether to overwrite preexisting values"
        )
        parser.add_argument(
            "--allow-overcount",
            action="store_true",
            dest="allow_overcount",
            help="Allow operations for when too many files were found"
        )
        return parser

    def __post_init__(self):
        if self.output_path.exists() and not self.overwrite:
            raise FileExistsError(
                f"Cannot write to '{self.output_path}' - it already exists and this application has not been told to "
                f"overwrite previously existing data"
            )

    @classmethod
    def parse(cls, args: generic.Sequence[str]) -> "Arguments":
        parser: argparse.ArgumentParser = cls.create_parser()

        input_values: argparse.Namespace = parser.parse_args(args or None)

        parsed_arguments: cls = cls(**vars(input_values))

        return parsed_arguments


def get_ensemble_members(first_member: pathlib.Path) -> tuple[generic.Sequence[pathlib.Path], DatasetInformation]:
    """
    Find all files adjacent to the given input that will be included in the combined data

    :param first_member: The path to a single netcdf file to consider - this doesn't need to be the first member, just any single member file
    :return: The series of paths to consider and a basic overview of what the input data will look like
    """
    LOGGER.debug(f"Finding adjacent ensemble members of '{first_member}'")
    if first_member.is_dir():
        raise IsADirectoryError(
            f"Cannot search for ensemble members - the first value was a path to a directory, not a member"
        )

    if not first_member.is_file():
        raise FileNotFoundError(
            f"Cannot search for ensemble members - the first value did not lead to a file"
        )

    first_member_name: str = first_member.name

    name_part_match: typing.Optional[re.Match[str]] = OUTPUT_NAME_PATTERN.match(first_member_name)

    if not name_part_match:
        raise ValueError(
            f"Cannot search for ensemble output members - '{first_member}' does not appear to be a ensemble post-processing output"
        )

    name_parts: dict[str, typing.Optional[str]] = name_part_match.groupdict()

    if not name_parts.get('member'):
        raise ValueError(
            f"Cannot search for ensemble output members for '{first_member}' - it does not appear to be ensemble output"
        )

    # All the files should have different member values, so exclude this in order to make hash comparisons correct
    name_parts.pop("member")

    # This id will be used as a basic way to see if parsed netcdf metadata is compatible
    primary_file_id: int = hash(tuple(name_parts.items()))

    primary_dataset_information: DatasetInformation = DatasetInformation.from_path(path=first_member)

    pp_output_directory: pathlib.Path = first_member.parent

    matching_files: set[pathlib.Path] = {first_member}

    for path in pp_output_directory.iterdir():
        if path == first_member:
            continue

        if not path.is_file():
            LOGGER.debug(f"Skipping '{path}' - it does not appear to be a file")
            continue

        path_match: typing.Optional[re.Match[str]] = OUTPUT_NAME_PATTERN.match(path.name)

        if not path_match:
            LOGGER.debug(f"Skipping '{path}' - it does not appear to be post processing ensemble output")
            continue

        path_match_parts: dict[str, typing.Optional[typing.Any]] = path_match.groupdict()

        # The value isn't going to register as an ensemble member if the member isn't reflected in the file name
        if not path_match_parts.get("member"):
            LOGGER.debug(f"Skipping '{path}' - it does not appear to be ensemble output")

        # Remove the member value as it will naturally differ from the rest
        path_match_parts.pop("member")

        path_id: int = hash(tuple(path_match_parts.items()))

        if path_id == primary_file_id:
            matching_files.add(path)
        else:
            LOGGER.debug(
                f"Skipping '{path}' - it does not appear to match the primary identifiers - '{name_parts}' vs '{path_match_parts}'"
            )

    if len(matching_files) < 2:
        raise ValueError(
            f"Only {len(matching_files)} matching file was found - this does not appear to have ensemble data"
        )

    LOGGER.debug(f"Found the matching ensemble members: {matching_files}")

    return sorted(matching_files), primary_dataset_information


def combine_files(
    paths: generic.Sequence[pathlib.Path],
    dataset_information: DatasetInformation
) -> xarray.Dataset:
    """
    Create a new xarray Dataset by combining the contained values

    :param paths: The paths of the netcdf files to combine
    :param dataset_information: Information about what to expect input data to look like
    :return: A new xarray dataset combining the resulting combined data
    """
    LOGGER.info(
        f"Combining the following paths via Mean{os.linesep}"
        f"    - {(os.linesep + '    - ').join(str(path.resolve()) for path in paths)}"
    )
    output_parameters: dict[str, typing.Any] = {
        "data_vars": {},
        "coords": {},
        "attrs": dataset_information.attributes.copy()
    }

    calculations: dict[str, StreamingMean] = {}

    for path_index, path in enumerate(paths):
        # Open and don't start by unpacking - it adds unnecessary overhead
        with xarray.open_dataset(path, mask_and_scale=False) as dataset:
            # Set initial expectations on the first go since they haven't been set yet
            if path_index == 0:
                output_parameters['coords'] = {
                    coordinate_name: coord.copy(deep=True)
                    for coordinate_name, coord in dataset.coords.items()
                }

                for details in dataset_information.variables:
                    # Prepare aggregation for this variable since the data is combinable
                    if details.is_combinable:
                        calculations[details.name] = StreamingMean(template=dataset.data_vars[details.name])
                    else:
                        # Go ahead and store the value here - it should be consistent across the board
                        output_parameters["data_vars"][details.name] = dataset.data_vars[details.name].copy(deep=True)

            # Update each calculation - this allows values to be calculated without loading all necessary data at the same time
            for variable_name, calculation in calculations.items():
                LOGGER.debug(f"Updating {variable_name} with data from {path}")
                calculation.update(dataset.data_vars[variable_name])

    # Convert the calculations into actual data to insert into the output
    for variable_name, calculation in calculations.items():
        calculated_data: xarray.DataArray = calculation.gather()
        output_parameters["data_vars"][variable_name] = calculated_data

    generated_data: xarray.Dataset = xarray.Dataset(**output_parameters)

    LOGGER.debug(f"Data from {paths} files have been combined")
    return generated_data


def clean_encoding(encoding: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """
    Filter out unneeded encoding values for an xarray DataArray

    Some encoding values, like a 'source' are added when loaded. This removes that junk

    :param encoding: The dictionary of encoding instructions to filter
    :return: A dictionary containing only valid variable encodings
    """
    valid_encoding_keys: set[str] = {
        "dtype",
        "_FillValue",
        "missing_value",
        "scale_factor",
        "add_offset",
        "zlib",
        "shuffle",
        "complevel",
        "fletcher32",
        "contiguous",
        "chunksizes",
    }

    cleaned_encoding: dict[str, typing.Any] = {
        key: value
        for key, value in encoding.items()
        if key in valid_encoding_keys
    }

    cleaned_encoding.setdefault("zlib", True)
    cleaned_encoding.setdefault("shuffle", True)
    cleaned_encoding.setdefault("complevel", 1)

    return cleaned_encoding


class StreamingMean:
    """
    Helper class to iterative calculate the mean over several arrays of files
    """
    def __init__(self, template: xarray.DataArray):
        self.name = str(template.name)
        self.dims = template.dims
        self.attrs = template.attrs.copy()

        if "long_name" in self.attrs:
            self.attrs['long_name'] = f"Ensemble Mean for {self.attrs['long_name']}"

        self.encoding = clean_encoding(template.encoding)

        self.fill_value = template.encoding.get("_FillValue")
        self.missing_value = template.encoding.get("missing_value", self.fill_value)

        if self.fill_value is None and "_FillValue" in self.attrs:
            self.fill_value = self.attrs.pop("_FillValue")
            self.encoding['_FillValue'] = self.fill_value

        if self.missing_value is None and "missing_value" in self.attrs:
            self.missing_value = self.attrs.pop("missing_value")
            self.encoding['missing_value'] = self.missing_value

        self.scale_factor = numpy.float32(template.encoding.get("scale_factor", 1.0))
        self.add_offset = numpy.float32(template.encoding.get("add_offset", 0.0))

        self.total = numpy.zeros(template.shape, dtype=numpy.float32)
        self.count = numpy.zeros(template.shape, dtype=numpy.uint16)

    def update(self, variable: xarray.DataArray):
        """
        Add a new array of data to the current store so that values may slowly and correctly accumulate

        :param variable: The netcdf variable to add to the calculation
        """
        self.normalize_size(new_shape=variable.shape)

        packed_slices: tuple[slice, ...] = tuple(slice(0, size) for size in variable.shape)
        packed: numpy.typing.NDArray[numpy.number] = numpy.full(
            self.total.shape,
            self.fill_value or self.missing_value or 0 if numpy.issubdtype(variable.data.dtype, numpy.integer) else 0.0,
            dtype=variable.data.dtype
        )
        packed[packed_slices] = variable.data

        # Consider all values when updating, since we can't tell what's a placeholder and what's not
        if self.fill_value is None and self.missing_value is None:
            valid = numpy.ones(packed.shape, dtype=bool)
        # We determined the missing_value has been set by the above conditional, so only update values in the arrays that aren't marked as the missing value
        elif self.fill_value is None:
            valid = packed != self.missing_value
        # We determined that full_value has been set by the above condition, so only update values in the arrays that aren't marked as being a filler value
        elif self.missing_value is None:
            valid = packed != self.fill_value
        # Only consider values are aren't a flag for a blank value
        else:
            valid = (packed != self.fill_value) & (packed != self.missing_value)

        # Make the read values an array of floats so that they may be adjusted and added
        decoded: numpy.typing.NDArray[numpy.float32] = packed.astype(numpy.float32, copy=False)

        # Adjust the values by the template's scale factor and add offset to ensure that the values are correctly unpacked
        decoded = decoded * self.scale_factor + self.add_offset

        # Add the decoded values to the total. This is performed this way to avoid unneeded operations under the hood
        numpy.add(self.total, decoded, out=self.total, where=valid)

        # Increment the count for all updated values
        self.count[valid] += 1

    def normalize_size(self, new_shape: tuple[int, ...]):
        """
        Resize the current total and count to match a new shape

        This is a no-op if the current size is larger than the new size

        :param new_shape: The new shape to conform to
        """

        if new_shape <= self.total.shape:
            return

        LOGGER.info(f"The array for {self.name} has to be reshaped from {self.total.shape} to {new_shape}")
        new_total: numpy.typing.NDArray = numpy.zeros(shape=new_shape, dtype=self.total.dtype)
        new_count: numpy.typing.NDArray = numpy.zeros(shape=new_shape, dtype=self.count.dtype)

        slices: tuple[slice, ...] = tuple(slice(0, size) for size in self.total.shape)

        new_total[slices] = self.total
        new_count[slices] = self.count

        self.total = new_total
        self.count = new_count

    def gather(self) -> xarray.DataArray:
        """
        Convert the calculated data into a valid data array to go into the output

        :return: An xarray dataarray ready to be inserted into a new dataset
        """
        # Allocate a new array filled with nan to insert values into
        mean: numpy.typing.NDArray[numpy.float32] = numpy.full(self.total.shape, numpy.nan, dtype=numpy.float32)

        # Divide the totals by the counts to get the standard mean where there have been values added and insert them
        # into our allocated array
        numpy.divide(
            self.total,
            self.count,
            out=mean,
            where=self.count > 0
        )

        # Create the DataArray to return
        output: xarray.DataArray = xarray.DataArray(
            name=self.name,
            data=mean,
            dims=self.dims,
            attrs=self.attrs,
        )

        # Ensure that the encoding is correct so that the variable is written to disk correctly
        output.encoding.update(self.encoding)
        return output



def main(args: generic.Sequence[str]) -> int:
    """
    The entry point of the application
    """
    arguments: Arguments = Arguments.parse(args)
    exit_code: int = 0

    try:
        member_paths, dataset_information = get_ensemble_members(arguments.input_path)  # type: generic.Sequence[pathlib.Path], DatasetInformation

        if len(member_paths) < arguments.expected:
            LOGGER.info(
                f"Only the following members were found:{os.linesep}"
                f"    - {(os.linesep + '    - ').join([str(index + 1) + ': ' + str(path) for index, path in enumerate(member_paths)])}"
                f"And this operation has been told to expect {arguments.expected} members. Rerun this script when all are available."
            )
            return 0
        if len(member_paths) > arguments.expected and not arguments.allow_overcount:
            raise ValueError(
                f"This application was expecting {arguments.expected} files, but the following were found:{os.linesep}"
                f"    - {(os.linesep + '    - ').join([str(index + 1) + ': ' + str(path) for index, path in enumerate(member_paths)])}"
                f"Some assumption was wrong and input values need to be double checked"
            )
        with combine_files(paths=member_paths, dataset_information=dataset_information) as combined_files:
            LOGGER.debug(f"Writing combined data to {arguments.output_path}")
            combined_files.to_netcdf(arguments.output_path)
            LOGGER.info(f"Data written to {arguments.output_path}{os.linesep}")
    except KeyboardInterrupt:
        pass
    except BaseException as error:
        LOGGER.error(error, exc_info=True)
        raise

    return exit_code


if __name__ == "__main__":
    logging.setup_logging()

    _exit_code: int = 0
    try:
        _exit_code = main(sys.argv[1:])
    except KeyboardInterrupt:
        LOGGER.info(f"{__file__} interrupted by keyboard - now exiting")
    except BaseException as e:
        _exit_code = 1
        LOGGER.critical(
            f"Could not produce the ensemble mean: {e}"
        )

    raise SystemExit(_exit_code)
