# Scripts

This directory contains standalone utilities used around the post-processing
application. They are intended for development, diagnostics, data collection,
or one-off support work rather than the main production entrypoint.

Run scripts from the repository root unless noted otherwise:

```bash
python scripts/<script>.py --help
```

Most scripts import the package configuration, so environment variables such as
`PP_*` settings may affect paths, logging, resources, and debug behavior.

## collect_output_metadata.py

Recursively scans a directory for post-processing output files and records basic
metadata in a SQLite database.

### Usage

```bash
python scripts/collect_output_metadata.py \
  --search /path/to/output/root \
  --output /path/to/post_processing_scan.db
```

Defaults:

- `--search`: current working directory
- `--output`: `./YYYYMMDD_HHMM.post_processing_scan.db`

### Expected Input

The search path may be any directory. The script recursively looks for
`nwm.*.nc` and then only records files whose names match:

```text
nwm.t{cycle}z.{configuration}.{output_type}[_member].[{f###|tm###}.]{domain}[.{rfc}].nc
```

Only these output types are recognized by the filename parser:

- `channel_rt`
- `land`
- `forcing`
- `reservoir` / `reservoir.full`

### Expected Output

The output is a SQLite database with two tables:

- `Scan`: one row describing the scan itself
- `FoundFile`: one row per matching file

Useful columns include `configuration`, `output_type`, `domain`, `member`,
`slice_specification`, `rfc`, and `size`.

### Safe Expectations

- The script does not open NetCDF files. It uses filenames and file sizes only.
- It is safe for large file trees in the sense that it does not load file
  contents, but it still walks the whole tree with `Path.rglob`.
- It creates tables if they do not exist.

### Edge Cases And Gotchas

- Anything that does not match the hard-coded filename pattern is ignored.
- Dates are not parsed from paths, only from the scan timestamp.
- `family_key()` includes the slice/frame, so `f001` and `f002` are different
  families.
- `member` defaults to `0` for non-ensemble files.
- Running against an existing database appends a new `Scan` and new `FoundFile`
  rows. It does not deduplicate by path across scans.

### Subverting Behavior

- To scan alternate naming schemes, change `FILENAME_PATTERN`.
- To collect different file types, change `SEARCH_GLOB`.
- To add database fields, update the `Scan` or `FoundFile` dataclasses and their
  field metadata.

## profile_output_metadata.py

Reads a database created by `collect_output_metadata.py` and generates grouped
file-size statistics into another SQLite database.

### Usage

```bash
python scripts/profile_output_metadata.py /path/to/post_processing_scan.db \
  --scan_id 1 \
  --output-path /path/to/stats_for_post_processing_scan.db
```

Defaults:

- `--table`: `FoundFile`
- `--scan_id`: `1`
- `--column`: `size`
- `--output-path`: `./stats_for_{input_database_stem}.db`

### Expected Input

A SQLite database with a table compatible with `FoundFile`, normally created by
`collect_output_metadata.py`.

### Expected Output

A SQLite database containing `{table_name}_stats`, usually `FoundFile_stats`.
Rows are grouped by:

```text
configuration, output_type, domain, member, rfc
```

Generated fields include count, mean, standard deviation, median, minimum,
maximum, and configured percentile columns.

### Safe Expectations

- The script reads the selected scan into pandas and writes one stats table.
- Existing output tables are replaced.
- The save path is written through a temporary `*.tmp` database, then moved into
  place.

### Edge Cases And Gotchas

- The default `scan_id` is `1`; if the scan database has multiple scans, pass the
  intended id explicitly.
- Quantile generation exists through lambdas in `generate_statistics`, but there
  is commented-out older quantile code. Treat the current output as basic grouped
  stats plus the lambda percentile fields, not a polished reporting interface.
- The SQL table name and column name are interpolated directly. Use trusted table
  and column names.
- Empty scans or wrong scan ids will usually fail later in pandas rather than
  giving a friendly message.

### Subverting Behavior

- Use `--table` and `--column` to profile a different numeric table/column.
- Edit `GROUP_BY_COLUMNS`, `DEFAULT_STATS`, or `DEFAULT_QUANTILES` for different
  aggregation behavior.

## download_input.py

Downloads NWM source files from a NOMADS-style Apache directory listing for use
as post-processing input.

### Usage

```bash
python scripts/download_input.py short_range 00 channel_rt conus \
  --date 20260708 \
  --directory /path/to/sample
```

Long range requires a member:

```bash
python scripts/download_input.py long_range 12 channel_rt conus \
  --date 20260708 \
  --member 1 \
  --frame '00[1-3]'
```

The default source URL is:

```text
https://nomads.ncep.noaa.gov/pub/data/nccf/com/nwm/v3.0
```

It may be overridden with `--url` or `PP_DOWNLOAD_INPUT_BASE_URL`.

### Expected Input

CLI arguments identify a product family:

```text
configuration cycle output_type region
```

The script expects the remote server to expose paths like:

```text
{source_url}/nwm.{YYYYMMDD}/{configuration_directory}/
```

and links named like:

```text
nwm.t{cycle}z.{configuration}.{output_type}[_member].{f###|tm###}.{region}.nc
```

### Expected Output

Downloaded files are written under:

```text
{destination}/nwm.{YYYYMMDD}/
```

Downloads are written to a temporary `*.tmp` file first, then atomically moved to
the final name.

### Safe Expectations

- Existing files are skipped unless `--overwrite` is passed.
- Downloaded files are streamed in chunks and are not held in memory.
- TLS verification uses the default OpenSSL certificate path unless overridden.
- `--capath false` disables TLS verification and logs a warning.

### Edge Cases And Gotchas

- The script warns when the application is not in debug mode; it is intended for
  development support, not production workflows.
- The frame selector must match the script's numeric-pattern validator. It is
  inserted into a filename regex after `f` or `tm`.
- `form_configuration_link()` encodes current assumptions about NOMADS directory
  names. New NWM products or directory layouts may require code changes.
- Long range requires members 1-4. Medium range non-forcing requires members
  1-6. Medium range forcing rejects members.
- Progress percentage uses chunk count rather than exact bytes read, so it can
  be approximate.
- HTTP status is not explicitly checked after `GET`; a server error page can be
  downloaded if the server responds unexpectedly.

### Subverting Behavior

- Use `--url` to point at a mirror or local Apache listing.
- Use `--frame` to download a subset of frames.
- Use `--capath false` only for controlled diagnostics when TLS inspection or
  certificate problems block access.
- Set `PP_DOWNLOAD_INPUT_BASE_URL` to change the default source globally.

## ensemble-mean.py

Creates an ensemble mean product from adjacent post-processed ensemble member
files.

### Usage

```bash
python scripts/ensemble-mean.py \
  /path/to/nwm.t12z.long_range.channel_rt_1.conus.nc \
  /path/to/nwm.t12z.long_range.channel_rt.conus.nc \
  4
```

Arguments:

```text
input_path  one member file used to discover adjacent members
output_path file to write
expected    number of members expected
```

Optional flags:

- `--overwrite`: allow writing over an existing output file
- `--allow-overcount`: allow more discovered members than `expected`

### Expected Input

Input files must live in the same directory and match:

```text
nwm.t{cycle}z.{configuration}.{output_type}_{member}.[{f###|tm###}.]{region}.nc
```

The script discovers sibling files with the same parsed identifiers except for
`member`.

Only floating data variables are averaged. Non-floating data variables are copied
from the first dataset.

### Expected Output

The output is a NetCDF file with:

- coordinates copied from the first member
- global attributes copied from the first member
- non-floating data variables copied from the first member
- floating data variables replaced with the ensemble mean

For averaged variables, `long_name` is prefixed with `Ensemble Mean for ...`
when present.

### Safe Expectations

- The implementation is intended to avoid loading every member at once.
- Each variable is accumulated incrementally in `StreamingMean`.
- Existing output files are protected unless `--overwrite` is used.

### Edge Cases And Gotchas

- The current `StreamingMean.update()` is manual and fragile. It streams by
  member, but allocates multiple full-size temporary arrays per variable/member.
- Packed NetCDF values are opened with `mask_and_scale=False`; scale/fill
  metadata may be in attrs rather than encoding depending on xarray/backend
  behavior. Validate means carefully for packed integer variables.
- Shape handling attempts to resize accumulators, but tuple comparison is
  lexicographic. Ensemble members should practically be treated as requiring
  identical dimensions.
- Coordinate equality is not deeply validated across members. Coordinates are
  copied from the first member.
- Files are discovered by filename, not by reading ensemble metadata.
- The filename pattern only supports one-digit member ids.

### Subverting Behavior

- Use `--allow-overcount` to proceed when extra matching members are present.
- Use any member as `input_path`; discovery is based on siblings.
- To support custom names, change `OUTPUT_NAME_PATTERN`.
- To average non-floating packed variables differently, revise
  `VariableInformation.is_combinable` and `StreamingMean`.

### Maintenance Notes

This script is a candidate for simplification. A dask-backed xarray approach
using a member concat dimension can be easier to maintain, provided chunking is
controlled. If keeping the manual approach, prefer exact shape/coordinate
validation and avoid full-size scratch arrays in `update()`.

## mask_from_geopackage.py

Converts a GeoPackage layer into a NetCDF mask-like dataset.

### Usage

```bash
python scripts/mask_from_geopackage.py \
  /path/to/source.gpkg \
  /path/to/mask.nc \
  layer_name \
  feature_id \
  --fields nws_lid name
```

With filtering:

```bash
python scripts/mask_from_geopackage.py source.gpkg mask.nc layer_name feature_id \
  --query "rfc == 'ABRFC'"
```

### Expected Input

A GeoPackage readable by geopandas, a layer name, and one or more columns to use
as dimensions. Optional fields become data variables.

### Expected Output

A NetCDF file where:

- each requested dimension column becomes a coordinate
- each requested field becomes a data variable
- geometry is not preserved unless explicitly included as a field and supported
  by xarray serialization

### Safe Expectations

- The script loads the whole layer into memory.
- `--query` is passed to pandas/geopandas `.query()`.
- The output path is written directly with `to_netcdf`.

### Edge Cases And Gotchas

- Validation uses `assert`, so running Python with optimizations can disable
  some checks.
- Multiple dimensions are accepted, but field variables are created with
  `dims=dimensions` while using a one-dimensional column array. That only works
  cleanly for simple one-dimensional uses unless the data shape already matches.
- There is no CRS/projection handling in the output.
- There is no overwrite guard.
- The script imports geopandas at module import time, so environments without
  geopandas cannot even show help successfully.

### Subverting Behavior

- Call `main()` or `convert_dataframe_to_xarray()` from Python and pass custom
  field attributes/encoding; the CLI does not expose those options.
- Use `--query` for quick subsets instead of creating a temporary GeoPackage.

## analyze_timing.py

Parses timing logs emitted by the application and prints grouped runtime
statistics.

### Usage

```bash
python scripts/analyze_timing.py
```

There are no CLI options. It reads the configured logging JSON from
`settings.logging_config_path`, finds the handler named `timing_file`, then
loads files matching that configured filename under `settings.application_path`.

### Expected Input

Timing logs must match the hard-coded format:

```text
YYYY-MM-DDTHH:MM:SS-0000 | file_path | line_number | code_path | function_name | call | status | PT#S | pid
```

This corresponds to the timing log formatter used by this project.

### Expected Output

The script prints the top 15 grouped rows to stdout. Default grouping is:

```text
file_path, code_path, line_number, function_name, status
```

Default aggregations include min, max, average, total, and count.

### Safe Expectations

- It filters to records from roughly the last day by default.
- It ignores `function_name == "run"` by default.
- It filters out groups whose minimum duration is under 8 seconds.
- It parses logs in a small process pool.

### Edge Cases And Gotchas

- No CLI means custom filtering requires editing the script or importing its
  functions from Python.
- Logs that do not match `LINE_PATTERN` cause parsing errors.
- `pandas.concat` will fail if no logs are found or every parse returns no data.
- The timestamp regex expects an offset like `-0000`, not all ISO-8601 variants.
- The fallback settings object only defines a couple of paths; running outside
  the configured project may still fail.

### Subverting Behavior

- Import `get_all_timing()` and `get_stats()` from Python for custom grouping,
  filters, pid selection, minimum duration, or ignored functions.
- Edit `get_default_*()` helpers for persistent local defaults.
