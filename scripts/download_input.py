#!/usr/bin/env python3
"""
Downloads NWM output to be used as input data for post processing
"""
import typing
import argparse
import logging
import re
import pathlib
import sys
import os
import ssl

from urllib.parse import urljoin
from datetime import datetime
from datetime import timedelta

from html.parser import HTMLParser

import requests

from post_processing.configuration import settings
from post_processing.enums import Configuration
from post_processing.enums import ModelOutputType
from post_processing.work import starmap

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format=settings.log_format,
        datefmt=settings.date_format
    )

    # The urllib3 connection pool logger can be very chatty - set it to only record warnings or worse in order to
    # keep our logs free of unnecessary third party diagnostics
    connectionpool_logger: logging.Logger = logging.getLogger("urllib3.connectionpool")
    connectionpool_logger.setLevel(logging.WARNING)

LOGGER: logging.Logger = logging.getLogger(pathlib.Path(__file__).stem)
"""The primary logger for this file"""


_DEFAULT_SOURCE_URL: str = os.environ.get(
    "PP_DOWNLOAD_INPUT_BASE_URL",
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nwm/v3.0"
)
"""The default location for where to find NWM output"""


_VALID_CONFIGURATIONS: typing.List[str] = [
    "analysis_assim",
    "analysis_assim_extend",
    "analysis_assim_extend_no_da",
    "analysis_assim_alaska",
    "analysis_assim_alaska_no_da",
    "analysis_assim_hawaii",
    "analysis_assim_long",
    "analysis_assim_long_no_da",
    "analysis_assim_no_da",
    "analysis_assim_puertorico",
    "analysis_assim_puertorico_no_da",
    "forcing_analysis_assim",
    "forcing_analysis_assim_alaska",
    "forcing_analysis_assim_hawaii",
    "forcing_analysis_assim_puertorico",
    "forcing_medium_range",
    "forcing_medium_range_alaska",
    "forcing_medium_range_blend",
    "forcing_medium_range_blend_alaska",
    "forcing_short_range",
    "forcing_short_range_alaska",
    "forcing_short_range_hawaii",
    "forcing_short_range_puertorico",
    "long_range_mem1",
    "long_range_mem2",
    "long_range_mem3",
    "long_range_mem4",
    "medium_range_alaska_mem1",
    "medium_range_alaska_mem2",
    "medium_range_alaska_mem3",
    "medium_range_alaska_mem4",
    "medium_range_alaska_mem5",
    "medium_range_alaska_mem6",
    "medium_range_alaska_no_da",
    "medium_range_blend",
    "medium_range_blend_alaska",
    "medium_range_mem1",
    "medium_range_mem2",
    "medium_range_mem3",
    "medium_range_mem4",
    "medium_range_mem5",
    "medium_range_mem6",
    "medium_range_no_da",
    "short_range",
    "short_range_alaska",
    "short_range_hawaii",
    "short_range_hawaii_no_da",
    "short_range_puertorico",
    "short_range_puertorico_no_da"
]
"""Configurations that may be downloaded - excludes types not used in post processing"""

_VALID_OUTPUT_TYPE: typing.List[str] = [
    "channel_rt",
    "land",
    "forcing"
]
"""Model Output Types that may be downloaded - excludes types not used in post processing"""

NUMERIC_PATTERN_PATTERN: re.Pattern = re.compile(r"^(?:(?:\\d|\[(?:\d-\d|\d+)]|\d)(?:\?|[*+]|\{\d+(?:,\d*)?})?)+$")
"""
A pattern that indicates that a given string indicates a regular expression for identifying a series of digits.

Broken out, it translates to:
 - from beginning to end,
 - at least one:
    - '\d' pattern or [#-#], like [0-9], or [###], like [1356] to isolate what specific digits to accept, or just a literal number
    - followed by at most one of:
        - *
        - +
        - ?
        - {#}, like {3}, meaning 'match on exactly 3 of the previous'
        - {#,}, like {5,}, meaning 'match on at least 5 of the previous'
        - {#,#}, like {2,6}, meaning 'match on 2 to six of the previous'
    
Grouping is not supported - patterns like: '\d{4}0(4|[0-2][679])+' won't work

Covers inputs like:
    - '\d+'
    - '\d'
    - '\d\d+'
    - '5'
    - '6+'
    - '[0-9]'
    - '005?'
    - '[13579]{2,}'
"""


class Arguments:
    """
    Command line parameter parser and handler
    """

    def __init__(self, *args: str):
        self.source_url: str = None
        """Where to retrieve NWM output"""
        self.configuration: Configuration = None
        """What NWM configuration to download"""
        self.certificate_path: str = ssl.get_default_verify_paths().openssl_cafile
        """The path to certificates to use rather than the default mozilla certs"""
        self.cycle: str = None
        """What cycle of the configuration to download """
        self.date: str = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y%m%d")
        """The day of the model run (ex. 20250311)"""
        self.output_type: ModelOutputType = None
        """What type of output to pull down"""
        self.region: typing.Literal['conus', 'alaska', 'hawaii', 'puertorico'] = None
        """The location for the data to retrieve"""
        self.member: typing.Optional[int] = None
        """The ensemble member to download"""
        self.destination: pathlib.Path = settings.resource_path / "sample"
        """Where to store the downloaded data"""
        self.overwrite: bool = False
        """Whether to overwrite preexisting data"""
        self.log_level: typing.Literal['INFO', 'DEBUG', 'ERROR'] = 'INFO'
        """The level of messages that may be logged"""
        self.frame: str = r"\d+"
        """The pattern to use to indicate what frame or frames to retrieve ('\d+' for all or '00' for just 00)"""

        self.__parse(args=args)
        self.__validate()

    def __validate(self):
        """
        Raise exceptions if contained arguments are not valid
        """
        if self.destination.is_file():
            raise FileExistsError(f"Cannot use {self.destination} as an output location - it is a file and must be a directory")
        
        if not re.match(r"^\d{8}$", self.date):
            raise ValueError(f"'{self.date}' is not a valid date format - it should be 8 digits and nothing else")
        
        if not self.cycle:
            raise ValueError("No cycle was provided")
        
        if not re.match(r"^[0-2][0-9]$", self.cycle):
            raise ValueError(f"'{self.cycle}' is not a valid cycle - it must be a 2 digit string between 00 and 23")
        
        if int(self.cycle) > 23:
            raise ValueError(f"'{self.cycle}' is too high - the maximum value is 23")
        
        if self.configuration not in (Configuration.LongRange, Configuration.MediumRange, Configuration.MediumRangeBlend) and self.member:
            raise ValueError(f"Ensemble members are only valid for long or medium range configurations. Received '{self.configuration}'")
        
        if self.member and self.member < 1:
            raise ValueError(f"The minimum ensemble member is 1 - received {self.member}")

        if self.configuration == Configuration.LongRange and self.member is None:
            raise ValueError(f"A member must be given if selecting long range data")
        if self.configuration == Configuration.LongRange and self.member > 4:
            raise ValueError(f"The maximum ensemble member for long range data is 4 - received {self.member}")

        if self.configuration == Configuration.MediumRange and self.output_type == ModelOutputType.Forcing and self.member is not None:
            raise ValueError(f"Cannot select a member when using medium range forcing data")

        if self.configuration == Configuration.MediumRange and self.output_type != ModelOutputType.Forcing and self.member is None:
            raise ValueError(f"A member must be given if selecting medium range data")
        if self.configuration == Configuration.MediumRange and self.output_type != ModelOutputType.Forcing and self.member > 6:
            raise ValueError(f"The maximum ensemble member for medium range data is 6 - received {self.member}")

        if not NUMERIC_PATTERN_PATTERN.match(self.frame):
            raise argparse.ArgumentTypeError(
                f"The frame string '{self.frame}' is not valid - "
                f"it MUST be a regex pattern that ONLY matches on numbers, like '\d' or '\d\d' or '0+' or '00'"
            )

        if str(self.certificate_path).strip().lower() in ("none", ""):
            self.certificate_path = None
        elif self.certificate_path.strip().lower() == "false":
            LOGGER.warning(f"TLS verification disabled - only perform this operation when performing diagnostics")
            self.certificate_path = False

        if self.source_url is None:
            self.source_url = _DEFAULT_SOURCE_URL

        verify_source_url(self.source_url, self.certificate_path)

    def __parse(self, args: typing.Sequence[str]):
        """
        Parse input parameters and set values

        :param args: Input from the command line
        """
        parser: argparse.ArgumentParser = argparse.ArgumentParser(
            description="Download NWM output to be used as input for post processing"
        )

        parser.add_argument(
            "-u",
            "--url",
            dest="source_url",
            type=str,
            default=None,
            help="Where to get the data"
        )

        parser.add_argument(
            "configuration",
            type=Configuration,
            choices=[configuration for configuration in Configuration],
            help="What NWM configuration to download",
        )

        parser.add_argument(
            "-c",
            "--capath",
            type=str,
            default=self.certificate_path,
            dest="certificate_path",
            help="Where to find TLS certificates needed to access servers containing data"
        )

        parser.add_argument(
            "cycle",
            type=str,
            help="2-digit number between '00' and '23' used to indicate what cycle to download - corresponds to t##z where ## is the cycle"
        )

        parser.add_argument(
            "-d",
            "--date",
            default=self.date,
            type=str,
            help=f"The 8 digit date, such as {self.date}"
        )

        parser.add_argument(
            "output_type",
            type=ModelOutputType,
            choices=[output_type for output_type in ModelOutputType],
            help="The model output type to download"
        )

        parser.add_argument(
            "region",
            type=str,
            choices=('conus', 'alaska', 'hawaii', 'puertorico'),
            help="Where the output was configured to model"
        )

        parser.add_argument(
            "-o",
            "--directory",
            dest="destination",
            type=pathlib.Path,
            default=self.destination,
            help="Where to store the results"
        )

        parser.add_argument(
            '-f',
            '--overwrite',
            dest="overwrite",
            action="store_true",
            help="Whether to overwrite preexisting data"
        )

        parser.add_argument(
            "-l",
            "--log-level",
            type=str,
            default=self.log_level,
            choices=["INFO", "ERROR", "DEBUG"],
            help="What level messages can be logged"
        )

        parser.add_argument(
            "-F",
            "--frame",
            dest="frame",
            type=str,
            default=self.frame,
            help="A pattern used to constrain what frames to include"
        )

        parser.add_argument(
            "--member",
            "-m",
            dest="member",
            type=int,
            default=self.member,
            help="The member of an ensemble"
        )

        parameters: argparse.Namespace = parser.parse_args(args=args) if args else parser.parse_args()

        for key, value in vars(parameters).items():
            if hasattr(self, key):
                setattr(self, key, value)


class ApacheDirectoryListingParser(HTMLParser):
    """
    Parses Apache Directory Listing HTML to find links to items available for download
    """
    def __init__(self, *, convert_charrefs = True):
        super().__init__(convert_charrefs=convert_charrefs)
        self.in_anchor: bool = False
        """Whether or not the parser is currently interpreting an <a> tag"""
        self.href: typing.Optional[str] = None
        """The current hyperlink reference interpreted by the parser"""
        self.current_text: typing.List[str] = []
        """The lines of text currently being parsed"""
        self.links: typing.Dict[str, str] = {}
        """Identified links"""

    def handle_starttag(self, tag, attrs):
        """
        Apply special handling for if an <a> tag is encountered

        :param tag: The name of the tag passed in (a, div, table, article, etc)
        :param attrs: Encountered attributes on the parsed tag
        :returns: The results of the parent handle_starttag(tag, attrs) function. Expect `None`
        """
        # Capture the link if in an <a>. We're going to want to capture the text held within, so clear out the list
        if tag == 'a':
            self.in_anchor = True
            self.href = dict(attrs).get("href")
            self.current_text = []
        return super().handle_starttag(tag, attrs)
    
    def handle_data(self, data):
        """
        Interpret lines of text held within a tag

        :param data: Data encountered within a tag
        """
        # If we're in an <a>, we want to store all the data held within - it will help provide a name for the link
        if self.in_anchor:
            self.current_text.append(data.strip())
        return super().handle_data(data)
    
    def handle_endtag(self, tag):
        """
        Collect collected information and store metadata if at the end of an <a>

        :param tag: The type of tag that was reached
        """
        if tag == 'a' and self.in_anchor:
            full_text = ''.join(self.current_text)
            if self.href and not self.href.endswith("/"):
                self.links[full_text] = self.href
            self.in_anchor = False
            self.href = None
            self.current_text = []
        return super().handle_endtag(tag)


def get_directory_links(
    url: str,
    certificate_path: str,
    session: typing.Optional[requests.Session] = None
) -> typing.Dict[str, str]:
    """
    Get the links to files within an apache directory listing

    :param url: The URL of the apache directory listing
    :param certificate_path: The path to required TLS certificates
    :param session: An optional requests session that can maintain connections
    :returns: A mapping of file names to their address
    """
    if isinstance(session, requests.Session):
        with session.get(url=url, verify=certificate_path) as response:
            raw_markup: bytes = response.content
    else:
        with requests.get(url=url, verify=certificate_path) as response:
            raw_markup: bytes = response.content
    markup: str = raw_markup.decode()

    parser: ApacheDirectoryListingParser = ApacheDirectoryListingParser()
    parser.feed(data=markup)
    return parser.links


def form_configuration_link(
    configuration: Configuration,
    output_type: ModelOutputType,
    region: str,
    member: typing.Optional[int]
) -> str:
    """
    Form the part of the apache directory listing URL that contains the desired NWM output

    :param configuration: The configuration of the data to pull
    :param output_type: The type of data that was output
    :param region: Where the output was configured to model
    :param member: The ensemble member of interest
    :returns: The part of the directory listing url that contains the desired output
    """
    link: str = configuration.value

    if output_type == ModelOutputType.Forcing:
        link = f"forcing_{link}"

    if region != "conus" and "no_da" in configuration.value:
        no_da_index: int = link.index("no_da")
        link = f"{link[:no_da_index]}{region}_{link[no_da_index:]}"
    elif region != "conus":
        link = f"{link}_{region}"

    if member:
        link = f"{link}_mem{member}"

    return link


def download_file(
    http_session: requests.Session,
    url: str,
    filename: str,
    directory: pathlib.Path,
    certificate_path: str,
    overwrite: bool = False,
    chunk_size: int = 8192,
) -> typing.Optional[pathlib.Path]:
    """
    Download the file at the URL and store it within the directory

    :param http_session: A connection pool session
    :param url: Where the file is
    :param filename: What to name the file
    :param directory: What directory to store the file in
    :param certificate_path: The path to the certificate to use to communicate with the server
    :param overwrite: Whether to overwrite a preexisting file
    :param chunk_size: The number of bytes to stream in at once
    :returns: The path to the downloaded file
    """
    directory.mkdir(parents=True, exist_ok=True)
    path: pathlib.Path = directory / filename

    # Write the incoming data into a temporary file, then save it to the desired location.
    # This will ensure that it doesn't save incomplete data.
    holding_path: pathlib.Path = directory / f"{filename}.{os.getpid()}.tmp"
    holding_path.unlink(missing_ok=True)

    # No works need to be done if the file exists and we don't intend to overwrite
    if path.exists() and not overwrite:
        LOGGER.info(f"Not downloading '{filename}' - it is already present")
        return None

    # If the path is a preexisting directory, we're dealing with invalid input parameters
    if path.is_dir():
        raise ValueError(f"Cannot save '{filename}' to '{path}' - it is already a directory")
    
    LOGGER.info(f"Downloading '{filename}'")

    # Keep track of the length of the longest line. Progress text will be writing over itself. If a shorter line
    # comes after a longer line, the output will be jumbled
    max_line_length: int = 0

    with http_session.get(url=url, stream=True, verify=certificate_path) as response:
        # Try to capture the expected length of the response. It is not guaranteed to come through
        content_length: int = int(response.headers.get("Content-Length", -1))
        data_length: int = 0
        update_pattern: str = "\rDownloading " + pathlib.Path(url).name + ": {progress:.1f}%"
        data_length_update: str = "\rDownloaded {progress}B"
        # Open the temporary file and write data to it iteratively - this will provide slightly faster download speeds
        # and help keep track of progress
        with holding_path.open("wb") as file:
            for chunk_index, chunk in enumerate(response.iter_content(chunk_size=chunk_size)):
                file.write(chunk)
                data_length += len(chunk)

                # If an expected number of bytes was passed via the header, we can mark progress,
                # otherwise we just have to accept that data was retrieved.
                if content_length > 0:
                    progress: int = chunk_size * chunk_index
                    percentage: float = (progress / content_length) * 100
                    update: str = update_pattern.format(progress=percentage).ljust(max_line_length)
                else:
                    # Just update the amount of data downloaded if we can't track progress
                    update: str = data_length_update.format(progress=data_length).ljust(max_line_length)

                max_line_length = max(max_line_length, len(update) + 1)
                sys.stdout.write(update)
                sys.stdout.flush()


        new_message = f"\rSaving to {path}...".ljust(max_line_length)
        max_line_length = max(max_line_length, len(new_message) + 1)

        sys.stdout.write(new_message)
        sys.stdout.flush()

        # Move the temporary data into the intended location. The operating system will ensure that this is a
        # thread and process safe operation
        holding_path.replace(path)

        sys.stdout.write(f"\r{filename} has been downloaded!".ljust(max_line_length + 1))
        sys.stdout.flush()

        # This process has been writing and overwriting stdout over and over again. Force a newline into the stream
        # to keep stdout messaging while clearing room for the next operation
        if content_length > 0:
            sys.stdout.write("\n")
            sys.stdout.flush()

    return path



def download_input(
    source_url: str,
    configuration: Configuration,
    cycle: str,
    date: str,
    output_type: ModelOutputType,
    region: str,
    member: typing.Optional[int],
    destination: pathlib.Path,
    certificate_path: str,
    frame_pattern: str = r"\d+",
    overwrite: bool = False
) -> typing.Sequence[pathlib.Path]:
    """
    The main application logic

    :param source_url: Where to download from
    :param configuration: The NWM configuration to download
    :param cycle: The NWM cycle to download
    :param date: The date of the data to download
    :param output_type: The type of output to download
    :param region: Where the output was configured to model
    :param member: The ensemble member to download
    :param destination: Where to put the data
    :param certificate_path: The path to the certificate needed to download data
    :param frame_pattern: A regex pattern to inject into the regex that matches on file name.
        Must be some variation of '\d' or '000*'. This will match on terms like 'tm00' or 'f018'
    :param overwrite: Whether to overwrite preexisting data

    :returns: A list of the paths to the files that were downloaded
    """
    configuration_part: str = form_configuration_link(
        configuration=configuration,
        output_type=output_type,
        region=region,
        member=member
    )
    listing_address: str = urljoin(source_url, f"/nwm.{date}/{configuration_part}/")
    """The address of the Apache directory listing where all the files may be found"""

    links_in_listing: typing.Dict[str, str] = get_directory_links(
        url=listing_address,
        certificate_path=certificate_path
    )
    """Links to each file within the listing for the date and configuration"""

    if not links_in_listing:
        raise FileNotFoundError(
            f"Could not find any data at '{listing_address}' for date '{date}', {configuration}, {output_type}, {region}, t{cycle}z"
        )

    # Now that we have the links for every item for the given configuration for that day, 
    # use the cycle and output type to find the right links to request
    model_output_type: str = output_type.value
    if member:
        model_output_type = f"{model_output_type}_{member}"

    desired_link_pattern: re.Pattern = re.compile(
        rf"nwm\.t{cycle}z\.{configuration.value}\.{model_output_type}\.(?:tm|f){frame_pattern}\.{region}\.nc"
    )

    pertinent_links: typing.Dict[str, str] = {
        filename: f"{listing_address}{address}"
        for filename, address in links_in_listing.items()
        if desired_link_pattern.match(filename)
    }

    if not pertinent_links:
        raise FileNotFoundError(
            f"None of the identified links were identified as NWM data, per the pattern '{desired_link_pattern.pattern}'.{os.linesep}"
            f"All found links:{os.linesep}"
            f"    - {(os.linesep + '    - ').join([str(key) + ': ' + str(value) for key, value in links_in_listing.items()])}"
        )

    download_directory: pathlib.Path = destination / f"nwm.{date}"
    download_directory.mkdir(parents=True, exist_ok=True)

    with requests.Session() as session:
        downloaded_files: typing.Sequence[pathlib.Path] = starmap(
            download_file,
            [
                {
                    "http_session": session,
                    "url": url,
                    "filename": filename,
                    "directory": download_directory,
                    "certificate_path": certificate_path,
                    "overwrite": overwrite,
                }
                for filename, url in pertinent_links.items()
            ]
        )

        downloaded_files = list(filter(lambda path: path is not None, downloaded_files))
    return downloaded_files


def verify_source_url(source_url: str, certificate_path: typing.Optional[typing.Union[str, bool]]):
    """
    Ensure that the given URL can be reached for data downloading

    :param source_url: The URL to test
    :param certificate_path: The path to certificates that may be needed to communicate with the server
    :raises Exception: Raised if a head request failed
    """
    if not source_url.endswith("/"):
        source_url += "/"
    with requests.head(source_url, verify=certificate_path) as response:
        if response.status_code < 400:
            return
    raise Exception(f"There is not an accessible default source url at {source_url}. The certificate path was {certificate_path}")



def main() -> int:
    """
    The entrypoint for this script
    """
    arguments: Arguments = Arguments()

    # Set the log level in order to give end users the chance to show more or less information
    LOGGER.setLevel(logging.getLevelName(arguments.log_level))

    # Mark the start time so we can track how long it took to download data
    start: datetime = datetime.now()

    try:
        downloaded_files: typing.Sequence[pathlib.Path] = download_input(
            source_url=arguments.source_url,
            configuration=arguments.configuration,
            cycle=arguments.cycle,
            date=arguments.date,
            output_type=arguments.output_type,
            region=arguments.region,
            member=arguments.member,
            destination=arguments.destination,
            overwrite=arguments.overwrite,
            certificate_path=arguments.certificate_path,
            frame_pattern=arguments.frame,
        )

        # List all newly downloaded files
        if downloaded_files:
            print(f"{len(downloaded_files)} files were downloaded:")
            for downloaded_file in downloaded_files:
                print(f"    - {downloaded_file}")
        else:
            print("No files were downloaded")
    except BaseException as exception:
        LOGGER.critical(f"'{__file__} failed: {exception}", exc_info=True)
        return 1
    LOGGER.info(f"Data downloaded in {datetime.now() - start}")
    return 0

if __name__ == "__main__":
    if not settings.debug:
        LOGGER.warning(
            "This environment is not in debug mode. Do not run this script within a testing or production environment as this is "
            "intended for development purposes only."
        )
    sys.exit(main())
