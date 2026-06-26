#!/usr/bin/env python3
import unittest
import typing
import pathlib
import os
import shutil

import numpy
import xarray

from post_processing.configuration import settings
from post_processing.transform.subset import mask_array

from ..helpers import setup_logging
from ..helpers import get_logger
from ..helpers import TestLogger
from ..helpers import get_temporary_directory
from ..output_specification import Dataset
from ..output_specification import deserialize

setup_logging()
LOGGER: TestLogger = get_logger(__file__)

FORECAST_CYCLE: int = 0
FORECAST_LENGTH: int = 18
FORECAST_INTERVAL: int = 1

REQUIRED_MATCHING_DIGITS: int = 6

NPP_TEST_DATA_DIRECTORY: pathlib.Path = pathlib.Path(os.environ['NPP_TEST_DATA_DIRECTORY']) if 'NPP_TEST_DATA_DIRECTORY' in os.environ else None
"""Where to look for static test data"""
OVERWRITE_PREEXISTING_TEST_DATA: bool = False
"""Whether to write new test data if preexisting test data was found"""


class MaskArrayTest(unittest.TestCase):
    def test_false_mask_cells_are_empty(self):
        input_data = xarray.DataArray(
            data=numpy.array([[1.0, 2.0], [3.0, 4.0]], dtype=numpy.float32),
            dims=("y", "x"),
            name="streamflow",
        )
        input_data.attrs["units"] = "m3 s-1"
        input_data.encoding["_FillValue"] = numpy.float32(-9999.0)

        mask = numpy.array([[True, False], [False, True]])

        result = mask_array(input_data=input_data, mask=mask)

        self.assertEqual(result.attrs, input_data.attrs)
        self.assertEqual(result.encoding, input_data.encoding)
        numpy.testing.assert_array_equal(
            numpy.isnan(result.values),
            numpy.array([[False, True], [True, False]])
        )
        numpy.testing.assert_array_equal(
            result.values[~numpy.isnan(result.values)],
            numpy.array([1.0, 4.0], dtype=numpy.float32)
        )

if __name__ == '__main__':
    unittest.main()
