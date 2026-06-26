#!/usr/bin/env python3
"""
Create a statistical profile defining each family of outputs captured by 'collect_output_metadata'
"""
import typing
import collections.abc as generic
import argparse
import sqlite3
import logging
import os
import sys
import pathlib
import dataclasses

import pandas
from pandas.core.groupby.generic import DataFrameGroupBy
from pandas.core.groupby.generic import SeriesGroupBy

LOGGER: logging.Logger = logging.getLogger(pathlib.Path(__file__).stem)

HELP_KEY: typing.Final[str] = "help"
FLAG_KEY: typing.Final[str] = "flag"

DEFAULT_STATS: typing.Final[dict[str, str]] = {
    "file_count": "count",
    "mean_size": "mean",
    "std_size": "std",
    "median_size": "median",
    "minimum_size": "min",
    "maximum_size": "max"
}

DEFAULT_QUANTILES: typing.Final[generic.Sequence[float]] = [
    0.05,
    0.25,
    0.75,
    0.9,
    0.99
]

GROUP_BY_COLUMNS: typing.Final[generic.Sequence[str]] = [
    "configuration",
    "output_type",
    "domain",
    "member",
    "rfc"
]

@dataclasses.dataclass
class Arguments:
    database_path: pathlib.Path = dataclasses.field(
        metadata={
            HELP_KEY: "Where to find the sqlite database with file scans",
            FLAG_KEY: ["database_path"]
        }
    )
    output_path: typing.Optional[pathlib.Path] = dataclasses.field(
        default=None,
        metadata={
            HELP_KEY: "Where to find the generated statistics",
            FLAG_KEY: ["-o", "--output-path"]
        }
    )
    file_table: typing.Optional[str] = dataclasses.field(
        default="FoundFile",
        metadata={
            HELP_KEY: "The table in the database that contains file information",
            FLAG_KEY: ["-t", "--table"]
        }
    )
    scan_id: typing.Optional[int] = dataclasses.field(
        default=1,
        metadata={
            HELP_KEY: "The scan to create statistics for",
            FLAG_KEY: ["-i", "--scan_id"]
        }
    )
    size_column: typing.Optional[str] = dataclasses.field(
        default="size",
        metadata={
            HELP_KEY: "The column containing size information",
            FLAG_KEY: ["-c", "--column"]
        }
    )

    def __post_init__(self):
        if not self.output_path:
            self.output_path = pathlib.Path.cwd() / f"stats_for_{self.database_path.stem}.db"

    @classmethod
    def get_parser(cls) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = argparse.ArgumentParser(
            description=__doc__,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )

        for field in dataclasses.fields(cls):
            field_arguments: dict[str, typing.Any] = {
                "help": field.metadata.get(HELP_KEY, field.name)
            }

            if field.default is not dataclasses.MISSING:
                field_arguments["type"] = typing.get_args(field.type)[0] if typing.get_args(field.type) else field.type
                field_arguments['default'] = field.default
                field_arguments['dest'] = field.name
            elif field.default_factory is not dataclasses.MISSING:
                field_arguments['type'] = field.default_factory
                field_arguments['dest'] = field.name
            else:
                field_arguments["type"] = typing.get_args(field.type)[0] if typing.get_args(field.type) else field.type
            try:
                parser.add_argument(
                    *field.metadata[FLAG_KEY],
                    **field_arguments
                )
            except Exception as e:
                import json
                LOGGER.error(
                    f"Could not add a parameter for the '{field.name}' field in the arguments parser. "
                    f"Here are the parameters:{os.linesep}{json.dumps({name: str(value) for name, value in field_arguments.items()}, indent=4)}{os.linesep}"
                    f"Error: {e}"
                )
                raise

        return parser

    @classmethod
    def parse(cls, args: typing.Optional[generic.Sequence[str]] = None) -> "Arguments":
        parser: argparse.ArgumentParser = cls.get_parser()
        parsed_input: argparse.Namespace = parser.parse_args(args or None)
        instance: cls = cls(**vars(parsed_input))
        return instance


def load_database(database_path: pathlib.Path, table_name: str, scan_id: int) -> pandas.DataFrame:
    with sqlite3.connect(database_path) as connection:
        file_table: pandas.DataFrame = pandas.read_sql(
            sql=f"SELECT * FROM {table_name} WHERE scan_id = ?;",
            con=connection,
            params=[scan_id]
        )
        return file_table


def generate_statistics(
    database_path: pathlib.Path,
    table_name: str,
    column_name: str,
    scan_id: int,
    stats: typing.Optional[dict[str, typing.Union[str, generic.Callable]]] = None,
    quantiles: typing.Optional[generic.Sequence[float]] = None,
):
    if not stats:
        stats = DEFAULT_STATS.copy()

    if not quantiles:
        quantiles = list(DEFAULT_QUANTILES)

    stats.update({
        f"p{int(quantile * 100):02d}": lambda values, q=quantile: values.quantile(q)
        for quantile in quantiles
    })

    database: pandas.DataFrame = load_database(database_path=database_path, table_name=table_name, scan_id=scan_id)
    grouping: DataFrameGroupBy = database.groupby(by=GROUP_BY_COLUMNS, dropna=False)
    column_to_analyze: SeriesGroupBy = grouping[column_name]

    basic_stats: pandas.DataFrame = typing.cast(pandas.DataFrame, column_to_analyze.agg(**stats))
    """
    quantile_stats: pandas.DataFrame = column_to_analyze.quantile(typing.cast(pandas.Series, quantiles)).unstack().rename(columns={
        quantile: f"p{str(int(quantile * 100)).zfill(2)}"
        for quantile in quantiles
    })
    all_statistics: pandas.DataFrame = basic_stats.join(quantile_stats, how="inner")
    
    all_statistics['scan_id'] = scan_id
    return all_statistics
    """
    basic_stats['scan_id'] = scan_id
    return basic_stats


def save_statistics(statistics: pandas.DataFrame, table_name: str, output_path: pathlib.Path):
    output_table_name: str = f"{table_name}_stats"
    temporary_output_path: pathlib.Path = output_path.parent / f"{output_path.name}.tmp"
    with sqlite3.connect(temporary_output_path) as connection:
        statistics.to_sql(
            name=output_table_name,
            con=connection,
            if_exists="replace"
        )
        temporary_output_path.replace(output_path)
        LOGGER.info(f"Statistics saved to {output_path}::{output_table_name}")


def main(args: typing.Optional[generic.Sequence[str]] = None) -> int:
    try:
        arguments: Arguments = Arguments.parse(args)
    except KeyboardInterrupt:
        LOGGER.info(f"Keyboard interrupt detected - now exiting")
        return 0
    except SystemExit as system_exit:
        return system_exit.code
    except BaseException as exception:
        LOGGER.critical(f"Could not parse CLI arguments: {exception}", exc_info=True)
        return 1

    try:
        statistics: pandas.DataFrame = generate_statistics(
            database_path=arguments.database_path,
            table_name=arguments.file_table,
            column_name=arguments.size_column,
            scan_id=arguments.scan_id,
        )
    except KeyboardInterrupt:
        LOGGER.info(f"Keyboard interrupt detected - now exiting")
        return 0
    except BaseException as exception:
        LOGGER.critical(
            f"Could not generate statistics from "
            f"{arguments.output_path}::{arguments.file_table}.{arguments.size_column}: {exception}",
            exc_info=True
        )
        return 1

    try:
        save_statistics(
            statistics=statistics,
            table_name=arguments.file_table,
            output_path=arguments.output_path
        )
    except KeyboardInterrupt:
        LOGGER.info("Keyboard interrupt detected - now exiting")
        return 0
    except BaseException as exception:
        LOGGER.critical(f"Could not save statistics: {exception}", exc_info=True)
        return 1

    return 0

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s] %(name)s %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z"
    )
    sys.exit(main(sys.argv[1:]))
