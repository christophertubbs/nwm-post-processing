#!/usr/bin/env python3
import os
import unittest
import typing
import logging
import pathlib
import re

from datetime import datetime

from post_processing.configuration import settings
from post_processing.schema import profile
from post_processing.schema.manifest import InputManifest

from test.data_test import DataTest

from test.helpers import get_logger

LOGGER = get_logger(pathlib.Path(__file__).stem)

class ProfileTest(DataTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile_directory = settings.application_path / "test" / "specifications" / "profiles"

    def test_get_function_by_name(self):
        logger: logging.Logger = logging.getLogger("test_get_function_by_name")
        square_root = profile.get_function_by_name("math.sqrt")
        self.assertIsNotNone(square_root)
        self.assertTrue(callable(square_root))
        self.assertEqual(square_root(9), 3)

        import math
        import operator

        scope = {
            **globals(),
            "dummy_func": lambda x: x,
            "non_callable": 123
        }

        self.assertEqual(profile.get_function_by_name("dummy_func", scope), scope['dummy_func'])

        with self.assertRaises(ValueError):
            profile.get_function_by_name("")

        with self.assertRaises(KeyError):
            profile.get_function_by_name("unknown_function")

        with self.assertRaises(ValueError):
            profile.get_function_by_name("non_callable", scope)

        self.assertEqual(profile.get_function_by_name("math.sqrt", scope), math.sqrt)
        self.assertEqual(profile.get_function_by_name("operator.add", scope), operator.add)

        with self.assertRaises(ImportError):
            profile.get_function_by_name("nonexistent.module.function")

        with self.assertRaises(AttributeError):
            profile.get_function_by_name("math.nonexistent_function")

        with self.assertRaises(ValueError):
            profile.get_function_by_name("math.pi")  # pi is not callable

        del scope["dummy_func"]
        del scope["non_callable"]

        first_from_root = profile.get_function_by_name("post_processing.utilities.common.first", scope)

        self.assertEqual(first_from_root([1, 2, 3]), 1)

        from post_processing import utilities

        first_from_utilities = profile.get_function_by_name("utilities.common.first", {**scope, **locals()})

        self.assertEqual(utilities.common.first, first_from_utilities)

        self.assertEqual(first_from_utilities([1, 2, 3]), 1)

        from post_processing.utilities import common

        first_from_common = profile.get_function_by_name("common.first", {**scope, **locals()})

        self.assertEqual(common.first, first_from_common)

        self.assertEqual(first_from_common([1, 2, 3]), 1)

        from post_processing.utilities.common import first

        first_from_here = profile.get_function_by_name("first", {**scope, **locals()})

        self.assertEqual(first, first_from_here)

        self.assertEqual(first_from_here([1, 2, 3]), 1)
        logger.info("Test Complete")

    def test_get_profile_operation_types(self):
        logger: logging.Logger = logging.getLogger("test_get_profile_operation_types")
        logger.info("Running test")
        profile_operation_types: typing.Dict[profile.OperationType, typing.Type[profile.ProfileOperation]] = {}
        profile_operation_types.update(profile.get_profile_operation_types())
        self.assertTrue(len(profile_operation_types) > 0)
        logger.info("Test Complete")

    def test_merge_files(self):
        """
        Test to ensure the `merge` operation correctly merges multiple files and maintains data and metadata integrity
        """
        logger: logging.Logger = logging.getLogger("test_merge_files")
        logger.info("Running test")
        merge_profile_path: pathlib.Path = self.profile_directory / "merge_operation.json"
        merge_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=merge_profile_path)

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        def update_save_directory(profile_operation: profile.ProfileOperation) -> None:
            if isinstance(profile_operation, profile.SaveOperation):
                profile_operation.directory = output_directory

        merge_profile.visit(update_save_directory)

        self.assertEqual(len(merge_profile.operations), 2)
        self.assertTrue(isinstance(merge_profile.operations[0], profile.MergeOperation))
        self.assertTrue(isinstance(merge_profile.operations[1], profile.SaveOperation))

        logger.info("Loading the input manifest")
        manifest: InputManifest = InputManifest.from_files(self.get_input_files())
        merged_paths: typing.Sequence[pathlib.Path] = merge_profile(
            date=datetime.now().astimezone(),
            cycle=manifest.cycle,
            files=manifest.files
        )
        self.assertGreater(len(merged_paths), 0)
        
        for merged_path in merged_paths:
            self.assertTrue(merged_path.exists())

        logger.info("Test Complete")
            
    def test_extract_operation(self):
        logger: logging.Logger = logging.getLogger("test_extract_operation")
        logger.info("Running test")
        extract_profile_path: pathlib.Path = self.profile_directory / "extract_operation.json"
        extract_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=extract_profile_path)

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        def update_save_directory(profile_operation: profile.ProfileOperation) -> None:
            if isinstance(profile_operation, profile.SaveOperation):
                profile_operation.directory = output_directory

        extract_profile.visit(update_save_directory)

        logger.info("Extracting data from input...")
        results: typing.Sequence[pathlib.Path] = extract_profile.run(
            date=self.get_date(),
            cycle=self.get_test_forecast_cycle(),
            files=self.get_input_files()
        )
        self.assertTrue(isinstance(results, typing.Sequence))
        self.assertTrue(
            all(isinstance(path, pathlib.Path) for path in results),
            f"All members of the result set should have been paths but instead received: "
            f"{set([type(result) for result in results])}"
        )
        logger.info("Data has been extracted into separate files")

        extraction_step: profile.ExtractOperation = extract_profile.operations[0]

        filename_pattern: str = extraction_step.output_pattern
        mask_identifier_pattern: re.Pattern = re.compile(extraction_step.identifier_pattern)

        frame_pattern: re.Pattern = re.compile(r"(?<=\.)(tm|f)\d+(?=\.)")

        def get_frame_identifier(filename: str) -> str:
            match: typing.Optional[re.Match] = frame_pattern.search(filename)
            if match:
                return match.group(0)
            return ""

        from post_processing.utilities.common import first
        from post_processing.nwm_file import NWMFile
        import xarray
        import numpy

        mask_coordinates: typing.Dict[pathlib.Path, numpy.ndarray] = {}

        for input_file in self.get_input_files():
            nwm_file: NWMFile = NWMFile.parse(path=input_file)
            base_name_metadata: typing.Dict[str, typing.Any] = {
                "Configuration": nwm_file.configuration,
                "ModelOutputType": nwm_file.model_output_type,
                "cycle": str(nwm_file.cycle).zfill(2),
                "frame": get_frame_identifier(input_file.stem),
                "region": nwm_file.region,
                "input_name": input_file.stem,
            }

            for mask_path in map(pathlib.Path, extraction_step.masks):
                naming_metadata: typing.Dict[str, typing.Any] = {
                    "mask": mask_path.stem,
                    **base_name_metadata,
                }
                mask_identifier_match: typing.Optional[re.Match] = mask_identifier_pattern.search(mask_path.name)

                if mask_identifier_match:
                    naming_metadata.update(mask_identifier_match.groupdict())

                output_name_for_mask: str = filename_pattern.format(**naming_metadata)
                output_path: typing.Optional[pathlib.Path] = first(
                    results,
                    condition=lambda path: path.name == output_name_for_mask
                )
                self.assertIsNotNone(
                    output_path,
                    f"Could not find a match for the '{output_name_for_mask}' file associated with the mask at '{mask_path.name}'"
                )
                self.assertTrue(output_path.is_file())

                if mask_path not in mask_coordinates:
                    with xarray.open_dataset(filename_or_obj=mask_path, chunks={}, engine="h5netcdf") as mask:
                        coordinates: numpy.ndarray = mask[extraction_step.dimension].values
                        mask_coordinates[mask_path] = coordinates

                with xarray.open_dataset(filename_or_obj=output_path, chunks={}, engine="h5netcdf") as output:
                    coordinates_for_mask: numpy.ndarray = mask_coordinates[mask_path]
                    dimension_variable: xarray.DataArray = output[extraction_step.dimension]
                    coordinate_in_mask: numpy.ndarray = numpy.isin(dimension_variable.values, coordinates_for_mask)
                    extra_output_coordinates: xarray.DataArray = dimension_variable.sel(**{
                        extraction_step.dimension: ~coordinate_in_mask
                    })
                    self.assertEqual(
                        extra_output_coordinates.size,
                        0,
                        f"The masked data at '{output_path}' has {extra_output_coordinates.size} "
                        f"more coordinates than the mask at '{mask_path}' - "
                        f"{dimension_variable.size} vs {coordinates_for_mask.size}"
                    )

                    missing_output_coordinates: numpy.ndarray = coordinates_for_mask[
                        ~numpy.isin(coordinates_for_mask, dimension_variable.values)
                    ]

                    self.assertEqual(
                        len(missing_output_coordinates),
                        0,
                        f"There are {len(missing_output_coordinates)} coordinates from the mask ('{mask_path}') "
                        f"that aren't in the output ('{output_path}')"
                    )
        logger.info("Test Complete")
        
    def test_drop_operation(self):
        logger: logging.Logger = logging.getLogger("test_merge_files")
        logger.info("Running test")
        drop_profile_path: pathlib.Path = self.profile_directory / "drop_operation.json"
        drop_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=drop_profile_path)

        # Ensure output is saved to the correct testing location
        def is_save(profile_operation: profile.ProfileOperation) -> bool:
            return profile_operation.operation() == profile.OperationType.SAVE

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        for operation in filter(is_save, drop_profile.operations):  # type: profile.SaveOperation
            output_directory.mkdir(parents=True, exist_ok=True)
            operation.directory = output_directory

        # TODO: Make sure that the data to be dropped is present
        logger.info("Dropping data from input...")
        output_files: typing.Sequence[pathlib.Path] = drop_profile.run(
            date=self.get_date(),
            cycle=self.get_test_forecast_cycle(),
            files=self.get_input_files()
        )
        
    def test_rename_operation(self):
        logger: logging.Logger = logging.getLogger("test_rename_operation")
        logger.info("Running test")
        rename_profile_path: pathlib.Path = self.profile_directory / "rename_operation.json"
        rename_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=rename_profile_path)

        # Ensure output is saved to the correct testing location
        def is_save(profile_operation: profile.ProfileOperation) -> bool:
            return profile_operation.operation() == profile.OperationType.SAVE

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        for operation in filter(is_save, rename_profile.operations):  # type: profile.SaveOperation
            output_directory.mkdir(parents=True, exist_ok=True)
            operation.directory = output_directory

        # TODO: Ensure that the variables to be renamed are there but the new names aren't
        logger.info("Renaming variables")
        output_files: typing.Sequence[pathlib.Path] = rename_profile.run(
            date=self.get_date(),
            cycle=self.get_test_forecast_cycle(),
            files=self.get_input_files()
        )
        logger.info("Variables renamed")
        # TODO: Ensure that the variables to be renamed are not there but the new names are
        self.assertFalse(True, "Implement the test for profile.RenameOperation")
        logger.info("Test Complete")
        
    def test_attribute_operation(self):
        logger: logging.Logger = logging.getLogger("test_attribute_operation")
        logger.info("Running test")
        attribute_profile_path: pathlib.Path = self.profile_directory / "attribute_operation.json"
        attribute_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=attribute_profile_path)

        # Ensure output is saved to the correct testing location
        def is_save(profile_operation: profile.ProfileOperation) -> bool:
            return profile_operation.operation() == profile.OperationType.SAVE

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        for operation in filter(is_save, attribute_profile.operations):  # type: profile.SaveOperation
            output_directory.mkdir(parents=True, exist_ok=True)
            operation.directory = output_directory

        # TODO: Establish the baseline of what attributes are set
        logger.info("Updating attributes")
        output_files: typing.Sequence[pathlib.Path] = attribute_profile.run(
            date=self.get_date(),
            cycle=self.get_test_forecast_cycle(),
            files=self.get_input_files()
        )
        logger.info("Attributes updated")
        # TODO: Check for expected differences
        self.assertFalse(True, "Implement the test for profile.AttributeOperation")
        logger.info("Test Complete")
        
    def test_save_operation(self):
        logger: logging.Logger = logging.getLogger("test_save_operation")
        logger.info("Running test")
        # This may not need to be done - it is already tested in some of the above steps
        save_profile_path: pathlib.Path = self.profile_directory / "save_operation.json"
        save_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=save_profile_path)

        # Ensure output is saved to the correct testing location
        def is_save(profile_operation: profile.ProfileOperation) -> bool:
            return profile_operation.operation() == profile.OperationType.SAVE

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        for operation in filter(is_save, save_profile.operations):  # type: profile.SaveOperation
            output_directory.mkdir(parents=True, exist_ok=True)
            operation.directory = output_directory

        # TODO: Ensure that the files are not present at the target location
        logger.info("Saving data")
        output_files: typing.Sequence[pathlib.Path] = save_profile.run(
            date=self.get_date(),
            cycle=self.get_test_forecast_cycle(),
            files=self.get_input_files()
        )
        logger.info("Data saved.")
        
    def test_branch_operation(self):
        logger: logging.Logger = logging.getLogger("test_branch_operation")
        logger.info("Running test")
        branch_profile_path: pathlib.Path = self.profile_directory / "branch_operation.json"
        branch_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=branch_profile_path)

        # Ensure output is saved to the correct testing location
        def is_save(profile_operation: profile.ProfileOperation) -> bool:
            return profile_operation.operation() == profile.OperationType.SAVE

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        for operation in filter(is_save, branch_profile.operations):  # type: profile.SaveOperation
            output_directory.mkdir(parents=True, exist_ok=True)
            operation.directory = output_directory

        # TODO: Ensure that the precondition for each branch is true
        logger.info("Performing branch operations")
        output_files: typing.Sequence[pathlib.Path] = branch_profile.run(
            date=self.get_date(),
            cycle=self.get_test_forecast_cycle(),
            files=self.get_input_files()
        )
        logger.info("Branches evaluated")

    def test_anomaly_operation(self):
        logger: logging.Logger = logging.getLogger("test_anomaly_operation")
        profile_path: pathlib.Path = self.profile_directory / "anomaly_operation.json"
        anomaly_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=profile_path)

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        try:
            results: typing.Sequence[pathlib.Path] = anomaly_profile.run(
                date=self.get_date(),
                cycle=self.get_test_forecast_cycle(),
                files=self.get_input_files(),
                output_path=output_directory
            )
        except:
            logger.error(f"Failed to run the profile defined at '{profile_path}'")
            raise

        # TODO: Ensure that all products were generated in the expected fashion

        logger.info("Test Complete")
        
    def test_load_operation(self):
        logger: logging.Logger = logging.getLogger("test_load_operation")
        logger.info("Running test")
        # This may need to be tested indirectly
        load_profile_path: pathlib.Path = self.profile_directory / "load_operation.json"
        load_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=load_profile_path)

        # Ensure output is saved to the correct testing location
        def is_save(profile_operation: profile.ProfileOperation) -> bool:
            return profile_operation.operation() == profile.OperationType.SAVE

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        for operation in filter(is_save, load_profile.operations):  # type: profile.SaveOperation
            output_directory.mkdir(parents=True, exist_ok=True)
            operation.directory = output_directory

        # TODO: manually load the files to be loaded
        # TODO: Find the load operation, call it, assign the loaded data
        # TODO: Ensure that the manually loaded data matches the data loaded via the operation

    def test_on_each_operation(self):
        """
        This tests a fully defined, semi-real world example of how to process an entire profile
        """
        logger: logging.Logger = logging.getLogger("on_each")
        logger.info("Running test")
        profile_path: pathlib.Path = self.profile_directory / "on_each.json"
        on_each_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=profile_path)

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        try:
            results: typing.Sequence[pathlib.Path] = on_each_profile.run(
                date=self.get_date(),
                cycle=self.get_test_forecast_cycle(),
                files=self.get_input_files(),
                output_path=output_directory
            )
        except:
            logger.error(f"Failed to run the profile defined at '{profile_path}'")
            raise

        # TODO: Ensure that all products were generated in the expected fashion

        logger.info("Test Complete")

    def test_profile(self):
        """
        This tests a fully defined, semi-real world example of how to process an entire profile
        """
        logger: logging.Logger = logging.getLogger("test_profile")
        logger.info("Running test")
        full_profile_path: pathlib.Path = self.profile_directory / "test_profile.json"
        full_profile: profile.Profile = profile.Profile.from_json(path_or_buffer=full_profile_path)

        output_directory: pathlib.Path = self.get_output_directory() / logger.name

        try:
            results: typing.Sequence[pathlib.Path] = full_profile.run(
                date=self.get_date(),
                cycle=self.get_test_forecast_cycle(),
                files=self.get_input_files(),
                output_path=output_directory
            )
        except:
            logger.error(f"Failed to run the profile defined at '{full_profile_path}'")
            raise

        # TODO: Ensure that all products were generated in the expected fashion

        logger.info("Test Complete")


if __name__ == '__main__':
    unittest.main()
