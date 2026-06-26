#!/usr/bin/env python3
"""
Recursively search a directory to find and profile output families
"""
import collections
import getpass
import typing
import collections.abc as generic
import pathlib
import logging
import re
import sqlite3
import dataclasses
import argparse
import sys
import os
import socket

from datetime import datetime

FILENAME_PATTERN: re.Pattern = re.compile(
    r"^nwm\."
    rf"t(?P<cycle>[0-2]\d)z\."
    rf"(?P<configuration>[^.]+)\."
    rf"(?P<output_type>channel_rt|land|forcing|reservoir(\.full)?)(_(?P<member>\d))?\."
    rf"((?P<slice_specification>(f|tm)\d+)\.)?"
    rf"(?P<domain>[a-z]+(\.(?P<rfc>\w\wrfc))?)\."
    r"nc$"
)
"""A pattern that will match expected post processing file names"""
LOGGER: logging.Logger = logging.getLogger(pathlib.Path(__file__).stem)
"""The dedicated logger for this script"""
SEARCH_GLOB: str = "nwm.*.nc"
"""The glob pattern to search for recursively"""
DB_TYPE_KEY: str = "db_type"
"""The key for what data type the field should be in the database"""
INSERT_CONVERSION_KEY: str = "insert_conversion"
"""The key for a function that will convert python data into a sql friendly format"""
SHOULD_INSERT_KEY: str = "should_insert"
"""The key for a metadata field that says whether or not the given field should be inserted into the database"""
FOREIGN_KEY_FIELD: str = "foreign_key"
"""The key for a foreign key designation for a field"""
FOREIGN_KEY_TABLE_FIELD: str = "table"
"""The key for what the foreign key fields uses to indicate what column to use"""
FOREIGN_KEY_COLUMN_FIELD: str = "column"
"""The key for what the foreign key fields uses to indicate what column to use"""
NAME: str = "post_processing_scan"
"""A general name for this script and what forms the default filename"""
IS_KEY_FIELD: str = "is_key_field"
"""The key for the metadata field that states whether or not this field is for a key"""
LOAD_VALUE_FUNCTION: str = "load_value_function"
"""The key for a function that will convert loaded data into the intended data type"""
DC = typing.TypeVar("DC")
"""A generic dataclass"""


@dataclasses.dataclass
class Arguments:
    """
    CLI arguments
    """
    search_path: typing.Optional[pathlib.Path] = dataclasses.field(default=pathlib.Path.cwd())
    output_path: typing.Optional[pathlib.Path] = dataclasses.field(default=pathlib.Path.cwd() / f"{datetime.now().astimezone().strftime('%Y%m%d_%H%M')}.{NAME}.db")

    @classmethod
    def create_parser(cls) -> argparse.ArgumentParser:
        """
        Create a command line parser
        """
        parser: argparse.ArgumentParser = argparse.ArgumentParser(
            description="Create a database of basic post processing output",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )

        field_mapping: dict[str, dataclasses.Field] = {
            field.name: field
            for field in dataclasses.fields(cls)
        }

        parser.add_argument(
            "-s",
            "--search",
            type=pathlib.Path,
            dest="search_path",
            default=field_mapping["search_path"].default,
            help="Where to start looking for files"
        )

        parser.add_argument(
            "-o",
            "--output",
            type=pathlib.Path,
            dest="output_path",
            default=field_mapping["output_path"].default,
            help="Where to put the output"
        )

        return parser

    @classmethod
    def parse(cls, args: typing.Union[generic.Sequence[str], None] = None) -> "Arguments":
        """
        Parse command line arguments into concrete typed results
        """
        parser: argparse.ArgumentParser = cls.create_parser()
        parsed_input: argparse.Namespace = parser.parse_args(args or None)

        return cls(**vars(parsed_input))


@dataclasses.dataclass
class Scan:
    """
    An indicator representing a single execution of this script
    """
    started: datetime = dataclasses.field(metadata={
        DB_TYPE_KEY: "TEXT NOT NULL",
        INSERT_CONVERSION_KEY: lambda value: value.isoformat() if isinstance(value, datetime) else value,
        LOAD_VALUE_FUNCTION: lambda value: datetime.fromisoformat(value)
    })
    """When the scan started"""
    search_path: pathlib.Path = dataclasses.field(metadata={
        DB_TYPE_KEY: "TEXT NOT NULL",
        INSERT_CONVERSION_KEY: lambda value: str(value),
        LOAD_VALUE_FUNCTION: lambda value: pathlib.Path(value)
    })
    """The root of where the scan is occurring"""
    completed: typing.Optional[datetime] = dataclasses.field(default=None, metadata={
        DB_TYPE_KEY: "TEXT",
        INSERT_CONVERSION_KEY: lambda value: value.isoformat() if value else None,
        LOAD_VALUE_FUNCTION: lambda value: datetime.fromisoformat(value)
    })
    """When the scan completed"""
    file_count: typing.Optional[int] = dataclasses.field(default=0, metadata={
        DB_TYPE_KEY: "INTEGER"
    })
    """The number of files found"""
    family_count: typing.Optional[int] = dataclasses.field(default=0, metadata={
        DB_TYPE_KEY: "INTEGER"
    })
    """The number of unique groups of data"""
    size: typing.Optional[int] = dataclasses.field(default=0, metadata={
        DB_TYPE_KEY: "INTEGER"
    })
    """The size of all data in megabytes"""
    scan_id: typing.Optional[int] = dataclasses.field(default=None, metadata={
        DB_TYPE_KEY: "INTEGER PRIMARY KEY",
        SHOULD_INSERT_KEY: False,
        IS_KEY_FIELD: True
    })
    """The unique identifier for the scan"""

    def __str__(self):
        return f"Output scan from {self.search_path} @ {self.started.strftime('%Y-%m-%d %H:%M:%S%z') if isinstance(self.started, datetime) else self.started}"


@dataclasses.dataclass
class FoundFile:
    path_to_file: pathlib.Path = dataclasses.field(metadata={
        DB_TYPE_KEY: "TEXT NOT NULL",
        INSERT_CONVERSION_KEY: lambda value: str(value.expanduser().resolve()),
        LOAD_VALUE_FUNCTION: lambda value: pathlib.Path(value)
    })
    """The path to the identified file"""
    cycle: str = dataclasses.field(metadata={
        DB_TYPE_KEY: "TEXT NOT NULL"
    })
    """The cycle that the file belonged to"""
    configuration: str = dataclasses.field(metadata={
        DB_TYPE_KEY: "TEXT NOT NULL"
    })
    """What configuration generated the data"""
    output_type: str = dataclasses.field( metadata={
        DB_TYPE_KEY: "TEXT NOT NULL"
    })
    """What sort of data was generated"""
    domain: str = dataclasses.field(metadata={
        DB_TYPE_KEY: "TEXT NOT NULL"
    })
    """What geographical domain the file is valid for"""
    size: float = dataclasses.field(metadata={
        DB_TYPE_KEY: "REAL NOT NULL"
    })
    """The size in megabytes for the identified data"""
    scan_id: int = dataclasses.field(metadata={
        DB_TYPE_KEY: "INTEGER NOT NULL",
        FOREIGN_KEY_FIELD: {
            FOREIGN_KEY_TABLE_FIELD: Scan.__name__,
            FOREIGN_KEY_COLUMN_FIELD: "scan_id"
        }
    })
    """The id of the scan that found this data"""
    member: typing.Optional[int] = dataclasses.field(default=0, metadata={
        DB_TYPE_KEY: "INTEGER"
    })
    """The optional ensemble member for the file"""
    slice_specification: typing.Optional[str] = dataclasses.field(default="", metadata={
        DB_TYPE_KEY: "TEXT"
    })
    """Slice information for the file if available, such as an f### or tm##"""
    rfc: typing.Optional[str] = dataclasses.field(default="", metadata={
        DB_TYPE_KEY: "TEXT"
    })
    """An additional rfc identifier (expect to be empty most of the time)"""
    found_file_id: typing.Optional[int] = dataclasses.field(default=None, metadata={
        DB_TYPE_KEY: "INTEGER PRIMARY KEY",
        SHOULD_INSERT_KEY: False,
        IS_KEY_FIELD: True
    })
    """A unique identifier for the found file in the database"""

    @classmethod
    def parse(cls, scan_id: int, path: pathlib.Path) -> typing.Optional["FoundFile"]:
        """
        Examine a file and convert it to a FoundFile if it has valid data within its name
        """
        match: typing.Optional[re.Match[str]] = FILENAME_PATTERN.match(path.name)

        if not match:
            return None

        try:
            size: float = round(path.stat().st_size / (1024 ** 2), ndigits=2)
        except Exception as e:
            LOGGER.warning(f"Could not correctly inspect {path}: {e}")
            return None

        return cls(
            scan_id=scan_id,
            size=size,
            path_to_file=path.expanduser().resolve(),
            **match.groupdict()
        )

    def family_key(self) -> str:
        """
        Form a general key for the file that will link it to other similar files
        """
        return ".".join([
            self.configuration.strip(),
            self.output_type.strip(),
            self.slice_specification.strip() if self.slice_specification else "No Slice",
            self.domain.strip(),
            str(self.member)
        ])

    def __hash__(self):
        return hash(self.path_to_file)

    def __str__(self):
        return str(self.path_to_file)


def add_table(database_path: pathlib.Path, data_class: type) -> bool:
    """
    Add a dataclass to the database

    :param database_path: The path to the database file
    :param data_class: The class to add to the database
    :returns: Whether the table was added
    """
    if not dataclasses.is_dataclass(data_class):
        return False

    try:
        definitions: list[str] = [
            f"    {field.name} {field.metadata[DB_TYPE_KEY]}"
            for field in dataclasses.fields(data_class)
        ]
    except KeyError as key_error:
        LOGGER.error(f"Could not form a field definition: {key_error}")
        raise

    definitions.extend([
        f"    FOREIGN KEY ({field.name}) REFERENCES {field.metadata[FOREIGN_KEY_FIELD][FOREIGN_KEY_TABLE_FIELD]}({field.metadata[FOREIGN_KEY_FIELD][FOREIGN_KEY_COLUMN_FIELD]}) ON DELETE CASCADE"
        for field in dataclasses.fields(data_class)
        if FOREIGN_KEY_FIELD in field.metadata
    ])

    script: str = f"""CREATE TABLE IF NOT EXISTS {data_class.__name__} (
{(',' + os.linesep).join(definitions)}
);"""

    LOGGER.debug(
        f"Adding the {data_class.__name__} table:{os.linesep}"
        f"{script}"
    )

    with sqlite3.connect(database=database_path) as connection:
        cursor = connection.cursor()
        cursor.execute(script)

    return True

def setup_database(database_path: pathlib.Path):
    """
    Add all necessary tables to the database

    :param database_path: Where the database should be
    """
    add_table(database_path=database_path, data_class=Scan)
    add_table(database_path=database_path, data_class=FoundFile)


def save_data(database_path: pathlib.Path, instance: DC) -> DC:
    """
    Save an instance of a dataclass to the database

    :param database_path: The path to the database
    :param instance: The instance of the data class to save
    :returns: That same object, but with updated field values
    """
    if not dataclasses.is_dataclass(instance):
        raise TypeError(f"Cannot save a {instance} (type={type(instance)}) - it is not a dataclass that may be saved")

    if isinstance(instance, type):
        raise TypeError(f"Cannot add {instance} (type={type}) to the database - it is not an instance of a dataclass")

    row_id: list[tuple[str, int]] = [
        (field.name, getattr(instance, field.name, None))
        for field in dataclasses.fields(instance)
        if field.metadata.get(IS_KEY_FIELD, False)
    ]

    if len(row_id) != 1:
        raise TypeError(f"Cannot determine the primary identifier for {instance} (type={type(instance)})")

    if row_id[0][1] is not None:
        return update_data(database_path=database_path, instance=instance)

    field_declarations: list[str] = []
    placeholders: list[str] = []
    parameters: dict[str, typing.Any] = {}

    for field in dataclasses.fields(instance):
        conversion: generic.Callable[[typing.Any], typing.Any] = field.metadata.get(INSERT_CONVERSION_KEY, lambda value: value)
        parameters[field.name] = conversion(getattr(instance, field.name))

        placeholders.append(f"    ${field.name}")
        field_declarations.append(f"    {field.name}")

    script: str = f"""INSERT INTO {instance.__class__.__name__} (
{(',' + os.linesep).join(field_declarations)}
) VALUES (
{(',' + os.linesep).join(placeholders)}
) RETURNING *;"""

    LOGGER.debug(
        f"Inserting a {instance.__class__.__name__}:{os.linesep}"
        f"{script}"
    )

    with sqlite3.connect(database=database_path) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.execute(script, parameters)
        result: sqlite3.Row = cursor.fetchone()

        if not result:
            raise Exception(f"Insert failed - could not retrieve the new record.")

        for column in result.keys():
            value = result[column]
            setattr(instance, column, value)

    return instance

def update_data(database_path: pathlib.Path, instance: DC) -> DC:
    """
    Update a preexisting entry in the database and update and value on the local object

    :param database_path: The path to the database to update
    :param instance: The instance of the dataclass whose entry is being updated
    :returns: The same instance but with any updated fields
    """
    if not dataclasses.is_dataclass(instance):
        raise TypeError(f"Cannot save a {instance} (type={type(instance)}) - it is not a dataclass that may be saved")

    row_id: list[tuple[str, int]] = [
        (field.name, getattr(instance, field.name))
        for field in dataclasses.fields(instance)
        if field.metadata.get(IS_KEY_FIELD, False)
    ]

    if len(row_id) != 1:
        raise TypeError(f"Cannot determine the primary identifier for {instance} (type={type(instance)})")

    values_to_insert: dict[str, typing.Any] = {
        row_id[0][0]: row_id[0][1]
    }
    fields_to_update: list[str] = []
    for field in dataclasses.fields(instance):
        conversion_function: generic.Callable[[typing.Any], typing.Any] = field.metadata.get(INSERT_CONVERSION_KEY, lambda val: val)
        values_to_insert[field.name] = conversion_function(getattr(instance, field.name))
        fields_to_update.append(f"    {field.name} = ${field.name}")

    script: str = f"""UPDATE {instance.__class__.__name__} SET
{(',' + os.linesep).join(fields_to_update)}
WHERE {row_id[0][0]} = ${row_id[0][0]}
RETURNING *;"""

    LOGGER.debug(
        f"Updating {instance}{os.linesep}"
        f"{script}"
    )

    with sqlite3.connect(database=database_path) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.execute(script, values_to_insert)
        result: sqlite3.Row = cursor.fetchone()

        if not result:
            raise Exception(f"Update failed - could not retrieve the updated record.")

        for column in result.keys():
            value = result[column]
            setattr(instance, column, value)

    return instance


def scan_output(search_path: pathlib.Path, database_path: pathlib.Path):
    """
    Search the indicated path and save insights to a database

    :param search_path: Where to start looking for data
    :param database_path: Where to save the results
    """
    new_scan: Scan = Scan(
        started=datetime.now().astimezone(),
        search_path=search_path
    )

    save_data(database_path=database_path, instance=new_scan)
    families: dict[str, list[FoundFile]] = collections.defaultdict(list)

    for path in search_path.rglob(SEARCH_GLOB):
        identified_file: typing.Optional[FoundFile] = FoundFile.parse(scan_id=new_scan.scan_id, path=path)

        if not identified_file:
            continue

        families[identified_file.family_key()].append(identified_file)
        LOGGER.debug(f"Found {identified_file} for the {identified_file.family_key()}")
        new_scan.file_count += 1
        new_scan.size += identified_file.size

    new_scan.family_count = len(families)

    for family in families.values():
        for file in family:
            save_data(database_path=database_path, instance=file)

    new_scan.completed = datetime.now().astimezone()
    save_data(database_path=database_path, instance=new_scan)
    LOGGER.info(f"Saved a new scan to {database_path}")



def main(args: typing.Optional[generic.Sequence[str]] = None) -> int:
    """
    The main entry point of the script
    """
    arguments: Arguments = Arguments.parse(args)

    try:
        setup_database(arguments.output_path)
        scan_output(search_path=arguments.search_path, database_path=arguments.output_path)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        LOGGER.critical(f"Could not setup the database: {e}", exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(filename)s #%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z"
    )
    sys.exit(main(sys.argv[1:]))
