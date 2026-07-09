# Red Herrings

This guide lists functions, classes, control paths, and conditions that can look
like likely causes during debugging but usually are not on the active failure
path. Use it to avoid spending time in code that is present, noisy, or
suspicious-looking but not actually responsible for a given behavior.

This does not mean these areas are perfect. Some are incomplete or fragile. The
point is narrower: when diagnosing a runtime issue, first confirm the suspect is
actually used by the profile, script, or command being run.

## How To Prove Something Is Not Involved

Before investigating a suspicious function or class, ask:

1. Is it imported by the command being run?
2. Is it selected by the active profile?
3. Is it reachable from the observed log line or traceback?
4. Does summary mode show the operation?
5. Does a search show any profile using that operation?

Useful checks:

```bash
post-process --summarize /path/to/input.nc /path/to/output
post-process --validate /path/to/input.nc /path/to/output
grep -RInE '"operation": "file_filter"|"operation": "stream"|"drop_dimension"' resources/profiles
grep -RIn "function_or_class_name" post_processing scripts resources
```

If the operation is absent from summary mode, it is not part of that profile's
normal control flow.

## Incomplete Or Placeholder Paths

### `post_processing.operations.file_filter.FilterOperation`

Why it looks suspicious:

- It has a full-looking set of condition classes.
- It uses filename metadata and filtering logic.
- It contains a bug-looking branch and raises `NotImplementedError` in
  `_validate()`.

Why it is usually a red herring:

- `_validate()` raises immediately, so a profile using it should fail during
  profile loading/validation rather than subtly filter files at runtime.
- It is not used by the standard profiles unless a profile explicitly contains
  `"operation": "file_filter"`.

How to verify:

```bash
grep -RIn '"operation": "file_filter"' resources/profiles test/specifications
post-process --validate /path/to/input.nc /path/to/output
```

What to investigate instead:

- `get_cycle_files()` when too many or too few files are processed.
- Profile `extract`, `group_by`, or `branch` operations if files disappear
  after a specific step.

### Process And MPI Communication Flags

Files:

- `post_processing/work/communication.py`
- `post_processing/work/gateway.py`
- `post_processing/work/tasks/base.py`

Why they look suspicious:

- There are constants for threads, processes, and nodes.
- There are branches mentioning multiprocessing and MPI.
- Some environments expose MPI variables.

Why they are usually a red herring:

- `COMMUNICATE_VIA_THREADS` is `True`.
- `COMMUNICATE_VIA_PROCESSES` and `COMMUNICATE_VIA_NODES` are `False`.
- Process/node gateway branches raise `NotImplementedError`.
- The NetCDF gateway is currently a `ThreadedGateway`.

How to verify:

```bash
python -c "from post_processing.work import communication; print(communication.COMMUNICATE_VIA_THREADS, communication.COMMUNICATE_VIA_PROCESSES, communication.COMMUNICATE_VIA_NODES)"
```

What to investigate instead:

- `settings.allow_threading` and `settings.maximum_additional_threads` for
  normal operation fan-out.
- NetCDF lazy file handles and the gateway queue for IO issues.

### Commented-Out Cleanup In `shutdown_executor`

Why it looks suspicious:

- `shutdown_executor()` imports cleanup hooks for masks and projections.
- The mask/projection cleanup calls are commented out.

Why it is usually a red herring:

- The active cleanup call is `close_gateway`.
- Commented-out cleanup cannot directly run or fail.
- Projection and mask caches can matter, but not because those commented lines
  are executing.

How to verify:

```bash
sed -n '45,80p' post_processing/work/orchestration.py
```

What to investigate instead:

- Actual cache owners, such as reprojection or subsetting modules.
- Whether an executor was shut down at all.

## Debug And Diagnostic Paths

### `Peek` Operation Warning

Why it looks suspicious:

- Logs can say peeking should not be done in production.
- The operation prints summaries of current data.

Why it is usually a red herring:

- `Peek` only runs if the active profile includes a `peek` operation or the CLI
  debug/peek path asks for output inspection.
- It reads/logs state; it should not be transforming normal outputs.

How to verify:

```bash
post-process --summarize /path/to/input.nc /path/to/output | grep -i "peek"
grep -RIn '"operation": "peek"' resources/profiles
```

What to investigate instead:

- The operation immediately before `Peek` in the profile summary.
- The saved file itself if only the printed summary looks odd.

### `DyeTask`

Why it looks suspicious:

- It prints all thread stacks.
- The gateway has special "dye" handling.

Why it is usually a red herring:

- It is a diagnostic task.
- It does not enter normal NetCDF read/write paths unless manually enqueued.

How to verify:

```bash
grep -RInE "DyeTask|Hitting the dye" post_processing scripts
```

What to investigate instead:

- Gateway queue shutdown/cancellation if tasks are not completing.

### Debug Mode Warnings

Why they look suspicious:

- The application warns that debug mode is enabled.
- Some scripts warn when debug mode is not enabled.

Why they are usually a red herring:

- These warnings usually describe intended operating context, not the cause of a
  data transformation error.
- Debug mode changes logging and may allow disabled operations, but it does not
  by itself alter NetCDF coordinates or values.

How to verify:

```bash
post-process --settings /path/to/input.nc /path/to/output | grep -Ei "debug|verbosity"
```

What to investigate instead:

- Profile operations, input discovery, and explicit settings such as paths,
  threading, and NetCDF engine.

## Metadata That Looks Like Data

### `reference_time__date`, `time__date`, And Similar Metadata Keys

Why they look suspicious:

- They appear in filename and directory templates.
- They are loaded from NetCDF coordinate values.
- They can be overwritten when metadata from multiple files is merged.

Why they are often a red herring:

- These are Python metadata keys used for templating and logging.
- They do not rewrite the NetCDF `time` or `reference_time` coordinate arrays.
- Wrong values here can cause bad filenames or directories, but not extra
  coordinate values in an already merged dataset.

How to verify:

```bash
grep -RInE "reference_time__date|time__date" post_processing resources/profiles
ncdump -h /path/to/output.nc | grep -E "reference_time|time"
```

What to investigate instead:

- Input coordinate values when output dimensions are wrong.
- `xarray.open_mfdataset(..., combine="by_coords")` when dimensions expand.

### Global Attributes Such As `model_initialization_time`

Why they look suspicious:

- They can disagree with coordinate values.
- They are human-readable and easy to spot in `ncdump -h`.

Why they are often a red herring:

- Most merge/alignment behavior is driven by coordinate variables, not global
  attributes.
- Global attributes may be copied from one file or overwritten during metadata
  collection without changing data arrays.

How to verify:

```bash
ncdump -h /path/to/file.nc | grep -E "model_initialization_time|reference_time"
```

What to investigate instead:

- The actual `reference_time(reference_time)` coordinate values.
- Whether the profile uses global attributes in output patterns.

## Profile Conditions That Are Easy To Misread

### Disabled Operations In Debug Mode

Why it looks suspicious:

- Disabled operations are present in a profile and can appear in summaries.
- Debug mode logs warnings about disabled operations.

Why it can be a red herring:

- In non-debug runs, disabled operations cause a hard failure before the profile
  proceeds.
- In debug runs, disabled operations are skipped by `call_generic_operations`.
- A disabled operation usually cannot partially transform data.

How to verify:

```bash
post-process --summarize /path/to/input.nc /path/to/output | grep -Ei "disabled|disable"
grep -RIn '"disable": true' resources/profiles
```

What to investigate instead:

- The enabled operation before or after the disabled one.
- Whether debug mode was intentionally enabled.

### Branch Duplicate Warnings

Why it looks suspicious:

- Branch operations warn about duplicate results.
- The message can mention a cycle/date and duplicate output paths.

Why it is often a red herring:

- The warning is about returned paths from different branches colliding.
- It does not necessarily mean NetCDF data was duplicated inside one file.
- Some branch paths intentionally return the original input path or shared
  intermediate paths.

How to verify:

```bash
post-process --summarize /path/to/input.nc /path/to/output
grep -RIn '"operation": "branch"' resources/profiles
```

What to investigate instead:

- Save operation output patterns if final files are overwritten.
- Branch return behavior if downstream operations receive unexpected paths.

### `drop_dimension`

Why it looks suspicious:

- It can remove dimensions from a NetCDF dataset.
- Profiles can set `"drop_dimension": true`.

Why it is often a red herring:

- The default is `False`.
- Many profiles use `drop` to remove variables, not dimensions.
- It only matters when the active profile's drop operation explicitly sets it.

How to verify:

```bash
post-process --summarize /path/to/input.nc /path/to/output | grep -Ei "dimension|drop"
grep -RIn '"drop_dimension": true' resources/profiles
```

What to investigate instead:

- Variable-level `drop` behavior for missing variables.
- Merge coordinate alignment for unexpected dimensions.

## IO And Encoding Suspects

### Default Float Encoding In `utilities.netcdf.submit_write`

Why it looks suspicious:

- It mutates encoding before writing.
- It sets fill values, scale factors, compression, and integer dtype defaults for
  float variables.

Why it is usually a red herring:

- It only applies when a variable has no encoding already.
- It affects how values are stored on disk, not which files are selected or how
  profile operations are ordered.
- It should not create extra dimensions or reference times.

How to verify:

```bash
ncdump -h /path/to/output.nc | grep -E "_FillValue|scale_factor|add_offset|chunksizes|zlib"
```

What to investigate instead:

- Encoding only when values look scaled, packed, rounded, or filled wrong.
- Input discovery and merge logic for shape/coordinate issues.

### `xarray.set_options(file_cache_maxsize=1)`

Why it looks suspicious:

- It globally changes xarray behavior.
- It appears in `get_gateway()`.

Why it is usually a red herring:

- It reduces file cache size to limit open file pressure.
- It should not alter data values, dimensions, or filenames.

How to verify:

```bash
grep -RIn "file_cache_maxsize" post_processing
```

What to investigate instead:

- Lazy datasets that remain open.
- Direct `xarray.open_dataset` calls outside the project NetCDF helpers.

### `.incomplete` Files After A Failure

Why they look suspicious:

- The name can look like a corrupt final output.
- They are created during writes.

Why they are often a symptom, not a cause:

- `_write_to_disk()` writes `target.incomplete`, then atomically replaces the
  final target.
- A leftover `.incomplete` usually means a previous write failed before replace.

How to verify:

```bash
find /path/to/output -name '*.incomplete' -ls
```

What to investigate instead:

- The original write failure.
- Duplicate output target names.
- Permission or filesystem errors.

## Filename And Member Red Herrings

### Member Values As Strings Versus Integers

Why it looks suspicious:

- Some profiles store `"member": "1"`.
- Manifests may parse members from filenames as strings.
- Other code normalizes members to integers.

Why it is usually a red herring for profile selection:

- Profile matching compares string-normalized member values.
- `Profile._validate()` converts digit strings to integers.

How to verify:

```bash
grep -RIn '"member":' resources/profiles/long_range* resources/profiles/medium_range*
```

What to investigate instead:

- Whether the filename itself includes the member suffix.
- Whether the profile's configuration/output/region also match.

### `date` In Paths Versus Date In Filenames

Why it looks suspicious:

- Operational directories often include `nwm.YYYYMMDD`.
- Output filenames may include dates in some profiles.
- Input filenames commonly do not include the date.

Why it can be a red herring:

- Core input grouping uses the filename, not parent directory date.
- A correct-looking parent directory name does not prove every contained file
  belongs to that date.

How to verify:

```bash
python -c "import pathlib, xarray as xr; root=pathlib.Path('/path/to/nwm.20260709');\
for p in sorted(root.glob('*.nc')):\
    ds=xr.open_dataset(p); print(p.name, ds.attrs.get('model_initialization_time'), ds.get('reference_time', None).values if 'reference_time' in ds else None); ds.close()"
```

What to investigate instead:

- Actual coordinate values inside the files.
- Stale same-cycle files in the directory.

## Script-Specific Red Herrings

### `scripts/profile_output_metadata.py` Quantile Comment Block

Why it looks suspicious:

- There is old commented-out quantile code.
- The script still generates stats.

Why it is usually a red herring:

- Commented code does not run.
- Current quantile fields are added through lambdas in `generate_statistics`.

How to verify:

```bash
python scripts/profile_output_metadata.py --help
grep -nE "quantile|DEFAULT_QUANTILES" scripts/profile_output_metadata.py
```

What to investigate instead:

- The scan id and table contents if stats are missing.

### `scripts/download_input.py` Debug Warning

Why it looks suspicious:

- It warns when the environment is not in debug mode.

Why it is usually a red herring:

- The warning is a usage/context warning.
- It does not prevent the script from downloading.

How to verify:

```bash
python scripts/download_input.py --help
```

What to investigate instead:

- Source URL, date, cycle, member, product, and frame pattern.

### `scripts/ensemble-mean.py` DatasetInformation Hashes

Why it looks suspicious:

- Dataset metadata classes define hashes.
- Member discovery compares hashes of parsed filename pieces.

Why it is often a red herring:

- `DatasetInformation.__hash__()` is not the main member discovery gate.
- Adjacent member matching is mostly controlled by `OUTPUT_NAME_PATTERN` and
  parsed filename pieces with `member` removed.

How to verify:

```bash
grep -nE "primary_file_id|DatasetInformation|__hash__" scripts/ensemble-mean.py
```

What to investigate instead:

- Filename pattern mismatch.
- Missing or extra adjacent member files.
- Packed data handling in `StreamingMean` if values look wrong.

## Warnings That Are Often Informational

### "Debug Mode Is Enabled"

Usually means:

- The app is running in developer mode.
- Logs and disabled operation behavior may differ.

Usually does not mean:

- data was transformed differently by itself
- a profile was selected differently by itself

### "No Profiles Were Found"

This is not a downstream operation problem. It means the app did not find an
eligible profile, so later transform code did not run.

Investigate:

- filename parsing
- `PP_PROFILE_PATH`
- profile metadata

### "Not Loading ... Since It Is Not A JSON File"

Usually means:

- profile loader skipped a non-JSON file in the profile directory

Usually does not mean:

- a valid profile was ignored

Investigate only if the intended profile file is not `.json`.

## Conditions That Look Like Causes But Are Only Guards

### `expected_count` In `get_cycle_files`

Why it looks suspicious:

- It can raise when file count differs.

Why it is often a red herring:

- The main application currently calls `get_cycle_files(arguments.source_file)`
  without passing `expected_count`.
- Unless a caller explicitly passes it, this guard is inactive.

How to verify:

```bash
grep -RIn "get_cycle_files(" post_processing scripts
```

### `return_new_paths` In Save Operations

Why it looks suspicious:

- Save can return either new saved paths or original input paths.

Why it is often a red herring:

- It affects downstream operation inputs, not whether a save occurred.
- A final file can be written even when the operation returns original paths.

How to verify:

```bash
post-process --summarize /path/to/input.nc /path/to/output | grep -Ei "save|return"
grep -RIn '"return_new_paths"' resources/profiles
```

Investigate it only when downstream operations receive unexpected paths after a
save step.

### `settings.lazy_load_netcdf`

Why it looks suspicious:

- The setting exists and sounds like it controls xarray loading.

Why it may be a red herring:

- Several active task paths explicitly pass `chunks="auto"` or `chunks=None`
  and control loading locally.
- The specific code path matters more than the setting name.

How to verify:

```bash
grep -RInE "lazy_load_netcdf|chunks=" post_processing
```

What to investigate instead:

- The actual `netcdf.load`, `_load`, or direct `xarray.open_dataset` call in the
  failing path.

## When A Red Herring Stops Being A Red Herring

Any item in this guide can become relevant if:

- a new profile starts using it
- a script calls it directly
- a traceback points to it
- a configuration value routes execution into it
- a recent code change made it active

When in doubt, trace from the command entrypoint to the failing log line. Do not
debug by name similarity alone. Similar words such as `time`,
`reference_time`, `member`, `cache`, `thread`, or `filter` appear in multiple
places with different meanings.
