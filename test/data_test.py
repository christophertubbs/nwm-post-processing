#!/usr/bin/env python3
"""
Defines a base class that has the ability to create important sample data
"""
import shutil
import typing
import unittest
import abc
import pathlib
import os

from datetime import datetime
from threading import RLock

from post_processing.configuration import settings
from post_processing.enums import Region, Configuration, ModelOutputType

from . import helpers
from .output_specification import Dataset
from .output_specification import deserialize


FORECAST_CYCLE: int = 0
FORECAST_LENGTH: int = 18
FORECAST_INTERVAL: int = 1

REQUIRED_MATCHING_DIGITS: int = 6

NPP_TEST_DATA_DIRECTORY: pathlib.Path = pathlib.Path(
    os.environ.get(
        'NPP_TEST_DATA_DIRECTORY',
        settings.application_path / "test" / "data" / "input"
    )
)
"""Where to look for static test data"""
NPP_MASK_DATA_DIRECTORY: pathlib.Path = pathlib.Path(
    os.environ.get(
        "NPP_TEST_MASK_DIRECTORY",
        settings.application_path / "test" / "data" / "masks"
    )
)
NPP_ROUTELINK_DIRECTORY: pathlib.Path = pathlib.Path(
    os.environ.get(
        "NPP_TEST_ROUTELINK_DIRECTORY",
        settings.application_path / "test" / "data" / "routelink"
    )
)
NPP_THRESHOLD_DIRECTORY: pathlib.Path = pathlib.Path(
    os.environ.get(
        "NPP_TEST_THRESHOLD_DIRECTORY",
        settings.application_path / "test" / "data" / "thresholds"
    )
)
OVERWRITE_PREEXISTING_TEST_DATA: bool = False
"""Whether to write new test data if preexisting test data was found"""


class DataTest(unittest.TestCase, abc.ABC):
    """
    Base class that may be used to programmatically point tests to required resources
    """
    _resource_lock: RLock = RLock()
    """A lock to ensure that different tests using the same resources don't overstep one another"""
    _temporary_directories: typing.List[pathlib.Path] = None
    """A list of directories created by this test that should be removed upon completion"""
    _data_directory: typing.Optional[pathlib.Path] = None
    """Where raw input data should be stored"""
    _mask_directory: typing.Optional[pathlib.Path] = None
    """Where masks for input data should be stored"""
    _routelink_directory: typing.Optional[pathlib.Path] = None
    """The directory that the routelink will be saved in"""
    #_threshold_directory: typing.Optional[pathlib.Path] = None
    """The directory that the thresholds will be saved in"""
    _created_data_directory: bool = False
    """Whether this test created a temporary data directory"""
    _output_directory: typing.Optional[pathlib.Path] = None
    """Where this test's output should be stored"""
    _input_dataset: typing.Optional[Dataset] = None
    """The configuration for how input data should be generated"""
    _input_files: typing.Optional[typing.Sequence[pathlib.Path]] = None
    """The files to use as input for tests"""
    _masks: typing.Optional[typing.Sequence[pathlib.Path]] = None
    """The masks to use for subsetting in tests"""
    _routelink_path: typing.Optional[pathlib.Path] = None
    """The path to the routelink for this dataset"""
    #_thresholds: typing.Optional[typing.Sequence[pathlib.Path]] = None
    """The paths to all the thresholds available for use"""
    _date: typing.Optional[datetime] = None
    """The date to use on tests"""
    _region: typing.Optional[Region] = None
    """The region to generate data for"""
    _configuration: typing.Optional[Configuration] = None
    """The model configuration to generate data for"""
    _model_output_type: typing.Optional[ModelOutputType] = None
    """The output type to generate data for"""

    @classmethod
    def get_configuration(cls) -> Configuration:
        """
        The model configuration of data to generate
        """
        return Configuration.ShortRange

    @classmethod
    def get_model_output_type(cls) -> ModelOutputType:
        """
        The type of model output to use for testing
        """
        return ModelOutputType.ChannelRouting

    @classmethod
    def get_region(cls) -> Region:
        """
        The region to use for testing
        """
        return Region.CONUS

    @classmethod
    def should_overwrite_input(cls) -> bool:
        """
        Whether generated input should be overwridden
        """
        return False

    @classmethod
    def make_temporary_directory(cls) -> pathlib.Path:
        """
        Create and issue a temporary directory that will get cleaned up when testing is complete
        """
        temporary_directory: pathlib.Path = helpers.get_temporary_directory()
        with cls._resource_lock:
            if cls._temporary_directories is None:
                cls._temporary_directories = []
            cls._temporary_directories.append(temporary_directory)
        return temporary_directory

    @classmethod
    def clean_up_temporary_directories(cls):
        """
        Remove all generated temporary directories
        """
        with cls._resource_lock:
            for temporary_directory in cls._temporary_directories:
                shutil.rmtree(temporary_directory)

    @classmethod
    def get_output_directory(cls) -> pathlib.Path:
        """
        Get the directory where generated products should be stored
        """
        with cls._resource_lock:
            if cls._output_directory is None:
                cls._output_directory = cls.make_temporary_directory()
            return cls._output_directory

    @classmethod
    def get_input_dataset(cls) -> Dataset:
        """
        The output specification to use to generate data
        """
        with cls._resource_lock:
            if cls._input_dataset is None:
                cls._input_dataset = deserialize(Dataset, cls.get_specification_paths()[0])
            return cls._input_dataset

    @classmethod
    def dataset_identifier(cls) -> str:
        """
        The identifier for the type of data being produced and manipulated

        For example: short_range.channel_rt.conus
        """
        return f"{cls.get_configuration()}.{cls.get_model_output_type()}.{cls.get_region()}"

    @classmethod
    def get_default_root_specification_path(cls) -> pathlib.Path:
        """
        Where to find general data specifications
        """
        return settings.application_path / "test" / "specifications"

    @classmethod
    def get_specification_paths(cls) -> typing.Sequence[pathlib.Path]:
        """
        The paths to each source of input data for the tests
        """
        default_paths: typing.Sequence[pathlib.Path] = [
            cls.get_default_root_specification_path() / f"{cls.dataset_identifier()}.json"
        ]
        if not any(path.is_file() for path in default_paths):
            raise FileNotFoundError(f"There are no test dataset specifications available within {cls.get_default_root_specification_path()}")
        return default_paths

    @classmethod
    def get_test_forecast_cycle(cls) -> int:
        """
        The cycle t00z number of the forecast within the day to impersonate
        """
        return FORECAST_CYCLE

    @classmethod
    def get_model_forecast_length(cls) -> int:
        """
        The number of values in a forecast
        """
        return FORECAST_LENGTH

    @classmethod
    def get_model_forecast_interval(cls) -> int:
        """
        The number of general time units between values in a forecast
        """
        return FORECAST_INTERVAL

    @classmethod
    def get_date(cls) -> datetime:
        """
        The date to use as an input parameter for the datasets.
        The date portion of 'nwm.t<date>00z.short_range.channel_rt.conus.nc'
        """
        with cls._resource_lock:
            if cls._date is None:
                cls._date = datetime.now().astimezone()
            return cls._date

    @classmethod
    def get_data_directory(cls) -> pathlib.Path:
        """
        Where raw test input data will be generated and saved
        """
        with cls._resource_lock:
            if cls._data_directory is None:
                if isinstance(NPP_TEST_DATA_DIRECTORY, pathlib.Path):
                    if NPP_TEST_DATA_DIRECTORY.is_file():
                        raise FileExistsError(
                            f"Cannot use {NPP_TEST_DATA_DIRECTORY} as a data location directory - it is a file, not a directory"
                        )
                    NPP_TEST_DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
                    cls._data_directory = NPP_TEST_DATA_DIRECTORY
                else:
                    cls._data_directory = cls.make_temporary_directory()
            return cls._data_directory


    @classmethod
    def get_mask_directory(cls) -> pathlib.Path:
        """
        Get where mask data should be saved
        """
        with cls._resource_lock:
            if cls._mask_directory is None:
                if isinstance(NPP_MASK_DATA_DIRECTORY, pathlib.Path):
                    if NPP_MASK_DATA_DIRECTORY.is_file():
                        raise FileExistsError(
                            f"Cannot use '{NPP_MASK_DATA_DIRECTORY}' as a mask location directory - it is a file, not a directory"
                        )
                    mask_directory: pathlib.Path = NPP_MASK_DATA_DIRECTORY / f"{cls.dataset_identifier()}"
                    mask_directory.mkdir(parents=True, exist_ok=True)
                    cls._mask_directory = mask_directory
                else:
                    cls._mask_directory = cls.make_temporary_directory()
            return cls._mask_directory

    @classmethod
    def get_routelink_directory(cls) -> pathlib.Path:
        """
        Get the directory where the routelink should be saved
        """
        with cls._resource_lock:
            if cls._routelink_directory is None:
                if isinstance(NPP_ROUTELINK_DIRECTORY, pathlib.Path):
                    routelink_directory = NPP_ROUTELINK_DIRECTORY
                    routelink_directory.mkdir(parents=True, exist_ok=True)
                    cls._routelink_directory = routelink_directory
                else:
                    cls._routelink_directory = cls.make_temporary_directory()
            return cls._routelink_directory

    """
    @classmethod
    def get_threshold_directory(cls) -> pathlib.Path:
        \"""
        Get the directory where thresholds will be saved
        \"""
        with cls._resource_lock:
            if cls._threshold_directory is None:
                if isinstance(NPP_THRESHOLD_DIRECTORY, pathlib.Path):
                    threshold_directory = NPP_THRESHOLD_DIRECTORY
                    threshold_directory.mkdir(parents=True, exist_ok=True)
                    cls._threshold_directory = threshold_directory
                else:
                    cls._threshold_directory = cls.make_temporary_directory()
            return cls._threshold_directory
    """
    @classmethod
    def setUpClass(cls):
        """Build up everything that this testing class needs"""
        cls.get_input_files()
        cls.get_masks()
        cls.get_output_directory()
        cls.get_routelink_path()
        #cls.get_thresholds()

    @classmethod
    def get_input_files(cls) -> typing.Sequence[pathlib.Path]:
        """
        Where to get the raw input files for tests
        """
        with cls._resource_lock:
            if cls._input_files is None or len(cls._input_files) == 0:
                cls._input_files = cls.get_input_dataset().generate_netcdf(
                    output_directory=cls.get_data_directory(),
                    cycle=cls.get_test_forecast_cycle(),
                    length=cls.get_model_forecast_length(),
                    step=cls.get_model_forecast_interval(),
                    overwrite=cls.should_overwrite_input()
                )
            return cls._input_files


    @classmethod
    def get_masks(cls) -> typing.Sequence[pathlib.Path]:
        """
        Get the path to makes for this test dataset
        """
        with cls._resource_lock:
            if cls._masks is None or len(cls._masks) == 0:
                cls._masks = cls.get_input_dataset().generate_masks(
                    data_path=cls.get_data_directory(),
                    mask_directory=cls.get_mask_directory(),
                    cycle=cls.get_test_forecast_cycle(),
                    length=cls.get_model_forecast_length(),
                    step=cls.get_model_forecast_interval(),
                )
            return cls._masks
    """
    @classmethod
    def get_thresholds(cls) -> typing.Sequence[pathlib.Path]:
        \"""
        Get the thresholds that are available for use
        \"""
        with cls._resource_lock:
            if cls._thresholds is None or len(cls._thresholds) == 0:
                cls._thresholds = cls.get_input_dataset().generate_thresholds(
                    data_path=cls.get_data_directory(),
                    threshold_directory=cls.get_threshold_directory() / f"{cls.dataset_identifier()}",
                    variable_to_threshold=cls.get_input_dataset().thresholds.variable_to_threshold,
                    percentiles=cls.get_input_dataset().thresholds.percentiles,
                    feature_dimension=cls.get_input_dataset().thresholds.feature,
                    time_dimension=cls.get_input_dataset().thresholds.time,
                    cycle=cls.get_test_forecast_cycle(),
                    step=cls.get_model_forecast_interval(),
                    length=cls.get_model_forecast_length(),
                    seed=cls.get_input_dataset().thresholds.seed,
                )
            return cls._thresholds
            """

    @classmethod
    def get_routelink_path(cls) -> pathlib.Path:
        """
        Get the path to the routelink
        """
        with cls._resource_lock:
            if cls._routelink_path is None or not cls._routelink_path.exists():
                cls._routelink_path = cls.get_input_dataset().generate_routelink(
                    sample_path=cls.get_input_files()[0],
                    output_path=cls.get_routelink_directory() / f"{cls.dataset_identifier()}.nc",
                    none_ratio=cls.get_input_dataset().routelink.none_ratio,
                    none_value=cls.get_input_dataset().routelink.none_value,
                    from_column_name=cls.get_input_dataset().routelink.from_column_name,
                    to_column_name=cls.get_input_dataset().routelink.to_column_name,
                    dimension=cls.get_input_dataset().routelink.dimension,
                    seed=cls.get_input_dataset().routelink.seed,
                )
            return cls._routelink_path



    @classmethod
    def tearDownClass(cls):
        """
        Remove test data from disk
        """
        cls.clean_up_temporary_directories()
