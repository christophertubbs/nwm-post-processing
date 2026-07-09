# Debugging Guide

This guide is for diagnosing and fixing issues in National Water Model Post
Processing. It assumes the reader may be new to this project and to NetCDF/NWM
post-processing workflows.

The project reads large NWM NetCDF files, applies profile-defined operations,
and writes WMS/RFC-oriented outputs. Most failures come from one of these areas:

- the input files are not the files the profile expects
- configuration paths point somewhere unexpected
- NetCDF/xarray IO leaves lazy file handles around
- profiles select the wrong operations or cannot render output names
- masks, RouteLink files, or projection references are missing or incompatible
- threading/process settings interact badly with NetCDF/HDF5

## First Response Checklist

Before changing code, capture the basics.

1. Record the exact command.
2. Record the exact source file path and output directory.
3. Record the environment or sourced configuration file.
4. Run summary mode:

```bash
post-process --summarize /path/to/input.nc /path/to/output
```

5. Run settings output if configuration is suspicious:

```bash
post-process --settings /path/to/input.nc /path/to/output
```

6. Validate profiles after profile edits:

```bash
post-process --validate /path/to/input.nc /path/to/output
```

7. If output values look wrong, inspect both inputs and outputs with `ncdump`,
   `xarray`, or `post-process --peek` if available in the workflow.

Use logs. A large fraction of the project already wraps failures with
`Failure in:` plus the profile operation that failed. Start at the first such
message.

## Useful Commands

### Inspect File Names In A Cycle

```bash
ls -lh /path/to/input/nwm.t12z.long_range.channel_rt_1.*.conus.nc
```

### Inspect NetCDF Header

```bash
ncdump -h /path/to/file.nc | less
```

### Inspect Dimensions And Coordinates With Xarray

```bash
python -c "import xarray as xr; p='/path/to/file.nc'; ds=xr.open_dataset(p); print(ds); ds.close()"
```

### Check Reference Times Across A Directory

```bash
python -c "import pathlib, xarray as xr; root=pathlib.Path('/path/to/input');\
for p in sorted(root.glob('nwm.t12z.long_range.channel_rt_1.*.conus.nc')):\
    ds=xr.open_dataset(p); print(p.name, ds.get('reference_time', 'missing').values); ds.close()"
```

### Search For A Profile

```bash
grep -RInE '"configuration": "long_range"|"output_type": "channel_rt"|"member": "1"' resources/profiles
```

### Search Code Quickly

Use recursive `grep`:

```bash
grep -RInE "reference_time|open_mfdataset|RouteLink|Failure in" post_processing scripts resources
```

## Input Discovery Problems

### Symptom: The Wrong Files Were Processed

Likely causes:

- The source file's directory contains old files for the same cycle/product.
- NWM filenames do not include a date, so same-cycle files from multiple days can
  look compatible.
- The selected source file matches a profile but its siblings are stale.

How to check:

- List the source file directory.
- Compare `time`, `reference_time`, and global attributes such as
  `model_initialization_time`.
- Read `post_processing.utilities.common.get_cycle_files`; it groups by filename
  fields: cycle, configuration, output type, member, and region.

Possible fixes:

- Process from a clean input directory.
- Separate input by date/cycle/member.
- Add a guard that validates all input `reference_time` values before merge.
- Add expected-count checks when product expectations are fixed.

Project style:

- Keep grouping logic explicit and filename-driven unless replacing it with a
  clear typed object.
- If adding validation, produce a message that lists every offending file.

### Symptom: No Cycle Files Found

Likely causes:

- The source path is wrong.
- The source is a directory, not a file.
- The filename does not match `NWM_FILENAME_PATTERN`.
- The product uses a naming scheme not covered by the project.

How to check:

```bash
python -c "from post_processing.utilities.common import NWM_FILENAME_PATTERN; import pathlib; p=pathlib.Path('/path/to/file.nc'); print(bool(NWM_FILENAME_PATTERN.match(p.name)))"
```

Possible fixes:

- Use a normal NWM filename.
- Update `NWM_FILENAME_PATTERN` only if the new naming scheme is valid project
  input.
- Add tests for any filename pattern change.

## Profile Selection And Validation

### Symptom: No Profiles Were Found

Likely causes:

- `PP_PROFILE_PATH` points to the wrong directory.
- The profile has a mismatched `configuration`, `output_type`, `region`, or
  `member`.
- Member type differs between manifest and profile in an unexpected way.
- The profile JSON is invalid and failed to load.

How to check:

```bash
post-process --settings /path/to/input.nc /path/to/output
post-process --validate /path/to/input.nc /path/to/output
grep -RInE '"configuration"|"output_type"|"region"|"member"' resources/profiles
```

Possible fixes:

- Correct the profile metadata.
- Correct `PP_PROFILE_PATH`.
- Keep ensemble member values consistent with existing profiles.

Project style:

- Profiles should use settings placeholders such as `{mask_path}` and
  `{routelink_path}` rather than hardcoded deployment paths.
- Do not add duplicate profiles to work around a path problem.

### Symptom: Profile Summary Looks Wrong

Likely causes:

- The selected profile is not the one you expected.
- A profile has disabled operations.
- A branch or nested operation is malformed.
- `member` metadata is not what you think it is.

How to check:

```bash
post-process --summarize /path/to/input.nc /path/to/output
```

Possible fixes:

- Fix profile metadata.
- Reorder operations only after confirming data dependencies.
- Validate nested operation dictionaries.

## Merge And Coordinate Problems

### Symptom: Output Has Extra `reference_time` Values

Likely causes:

- One or more input files have a different `reference_time`.
- Old same-cycle files are in the input directory.
- `xarray.open_mfdataset(..., combine="by_coords")` aligned distinct
  coordinate values instead of failing.

How to check:

```bash
python -c "import pathlib, xarray as xr; root=pathlib.Path('/path/to/input');\
for p in sorted(root.glob('*.nc')):\
    ds=xr.open_dataset(p); rt=ds.get('reference_time');\
    print(p.name, None if rt is None else rt.values); ds.close()"
```

Possible fixes:

- Clean the input directory.
- Reject mixed `reference_time` before merge.
- If a single input file is wrong, fix the upstream product or exclude it.

Project style:

- Do not silently choose one reference time from many.
- Prefer an explicit validation error over auto-dropping coordinates.

### Symptom: Output Has Unexpected Dimensions

Likely causes:

- Multi-file xarray merge aligned coordinates by value.
- Input variables have different shapes across files.
- A dimension adjustment expanded variables in one file but not another.
- A mask extraction changed a dimension name or coordinate.

How to check:

```bash
ncdump -h /path/to/output.nc | sed -n '1,80p'
```

Compare all candidate inputs:

```bash
python -c "import pathlib, xarray as xr; root=pathlib.Path('/path/to/input');\
for p in sorted(root.glob('*.nc')):\
    ds=xr.open_dataset(p); print(p.name, dict(ds.sizes)); ds.close()"
```

Possible fixes:

- Validate shapes and coordinates before merge.
- Fix the profile step that introduces the extra dimension.
- Use `full_load=True` in transforms that otherwise leave file handles around.

## NetCDF IO And File Handle Problems

### Symptom: Segfaults, HDF5 Errors, Or Random IO Failures

Likely causes:

- NetCDF/HDF5 thread-safety issues.
- Lazy xarray datasets outliving their file context.
- Too much threading around NetCDF reads/writes.
- File handles remain open while a file is overwritten.

How to check:

- Disable or reduce threading.
- Check `PP_ALLOW_THREADING`.
- Look for code using `xarray.open_dataset` directly instead of
  `post_processing.utilities.netcdf`.
- Search for returned lazy `Dataset` or `DataArray` objects from tasks.

Possible fixes:

- Use `post_processing.utilities.netcdf` helpers.
- Use `with netcdf.load(...) as dataset:` where possible.
- Use `.load()`, `.compute()`, or `.copy(deep=True)` before returning data that
  must be detached from a file.
- Prefer one controlled gateway write over many direct writes.

Project style:

- Do not introduce ad hoc direct NetCDF IO in profile operations.
- If direct xarray is needed, isolate it and document why.

### Symptom: `.incomplete` File Already Exists

Likely causes:

- A previous write failed.
- Two tasks tried to write the same target.
- Output names are not unique.

How to check:

```bash
find /path/to/output -name '*.incomplete' -ls
```

Possible fixes:

- Inspect the incomplete file and logs.
- Remove the `.incomplete` file only after confirming it is not an active write.
- Fix the profile output pattern so concurrent outputs do not collide.

Project style:

- Keep temporary-file write behavior. It prevents consumers from reading partial
  final outputs.
- Do not replace this with direct writes unless there is a very specific reason.

### Symptom: Permission Denied On Write

Likely causes:

- Output directory is not writable.
- Parent directory does not exist or has wrong permissions.
- A shared filesystem has ACL or group ownership issues.

How to check:

```bash
ls -ld /path/to/output /path/to/output/parent
touch /path/to/output/.write-test && rm /path/to/output/.write-test
```

Possible fixes:

- Correct the output path.
- Use a writable intermediate/output directory.
- Fix group/ACL permissions outside the application.

## Configuration Problems

### Symptom: Settings Do Not Match Environment Variables

Likely causes:

- A `.env` file overrides sourced shell environment values.
- Environment variable casing differs and multiple candidates exist.
- A path contains shell variables in `.env`; `.env` does not expand them.

How to check:

```bash
post-process --settings /path/to/input.nc /path/to/output
env | sort | grep -E '^PP_'
```

Possible fixes:

- Move machine-specific secrets/paths into `.env`.
- Use `environment.sh` for values that need shell expansion.
- Remove duplicate differently-cased variables.

Project style:

- Prefer settings over hardcoded paths.
- Keep `.env` local and private.

### Symptom: Resource, Mask, RouteLink, Or Threshold Path Missing

Likely causes:

- `PP_RESOURCE_PATH`, `PP_MASK_PATH`, `PP_ROUTELINK_PATH`, or
  `PP_THRESHOLD_PATH` points to the wrong location.
- Required files were not installed on the host.
- A profile references a literal path that only works on one machine.

How to check:

```bash
post-process --settings /path/to/input.nc /path/to/output
ls -lh /configured/path
```

Possible fixes:

- Correct the environment setting.
- Install/copy required resources.
- Change profile path templates to use settings placeholders.

## Mask And Subset Problems

### Symptom: Mask File Missing

Likely causes:

- Wrong `mask_path`.
- Profile references a mask that does not exist.
- Glob pattern expands differently than expected.

How to check:

```bash
ls -lh resources/masks
grep -RInE "masks|mask_path" resources/profiles
```

Possible fixes:

- Create or install the mask.
- Correct the profile path.
- Avoid overly broad globs unless intentionally selecting many masks.

Project style:

- A missing mask should be a hard failure, not silently skipped.

### Symptom: Extracted Output Is Empty Or Missing Stations

Likely causes:

- Mask coordinate name does not match the input coordinate.
- Feature ids differ between input and mask.
- The wrong RFC/domain mask was selected.
- `identifier_pattern` did not capture the intended region/RFC metadata.

How to check:

```bash
ncdump -h /path/to/mask.nc
ncdump -h /path/to/input.nc
```

Inspect coordinate overlap with xarray.

Possible fixes:

- Rebuild the mask with correct feature ids.
- Fix `mask_coordinate`, `dimension`, or `identifier_pattern`.
- Add a validation step that reports overlap counts.

## RouteLink And Upstream Flow Problems

### Symptom: Upstream Flow Calculation Fails

Likely causes:

- RouteLink file is missing.
- RouteLink variable names do not match profile kwargs.
- Input `feature_id` ordering differs from RouteLink assumptions.
- RouteLink data is not aligned to input features.

How to check:

```bash
ncdump -h /path/to/RouteLink_CONUS.nc | less
ncdump -h /path/to/input.nc | less
```

Search the profile for:

```text
routelink_path
routelink_from_variable
routelink_to_variable
routelink_feature_variable
```

Possible fixes:

- Correct the RouteLink path and variable names.
- Use the expected RouteLink file for the region.
- Add diagnostics around feature id overlap.

Project style:

- Keep heavy array operations in NumPy/xarray, not Python loops over features.
- Avoid hidden global state except for clearly managed caches.

## Reprojection Problems

### Symptom: Reprojection Fails Or Output Coordinates Look Wrong

Likely causes:

- Reference projection dataset is missing.
- CRS variable name or projection string attribute is wrong.
- x/y coordinate variable names do not match.
- Affine transform contains NaN or infinite values.

How to check:

```bash
ncdump -h /path/to/input.nc | grep -E "crs|x|y|lat|lon|spatial"
ncdump -h /path/to/reference_projection.nc | grep -E "crs|x|y|lat|lon|spatial"
```

Possible fixes:

- Correct profile reprojection settings.
- Use the intended regional projection file.
- Validate coordinate monotonicity and CRS attributes.

Project style:

- Keep CRS/projection attribute handling explicit.
- Preserve global references to spatial reference variables when renaming.

## Unit Conversion Problems

### Symptom: Unit Conversion Refuses A Variable

Likely causes:

- Variable has no `units` in attrs or encoding.
- Units are categorical or numeric codes.
- Conversion factor is not registered.
- Variable is not numeric.

How to check:

```bash
python -c "import xarray as xr; ds=xr.open_dataset('/path/to/file.nc'); v=ds['streamflow']; print(v.dtype, v.attrs, v.encoding); ds.close()"
```

Possible fixes:

- Add or correct units upstream.
- Add a supported conversion factor in `post_processing.transform.unit_conversion`.
- Skip conversion for categorical variables.

Project style:

- Do not guess units.
- Fail loudly when unit metadata is missing.

## NCO Problems

### Symptom: NCO Command Failed Or NCO Is Missing

Likely causes:

- Required NCO executables are not installed or not on `PATH`.
- The generated command is invalid for the file.
- NCO modified metadata in an unexpected way.

How to check:

```bash
which ncks ncap2 ncatted
ncks --version
```

Look for `Netcdf command failed:` in logs.

Possible fixes:

- Load the right module in HPC environments.
- Correct the NCO operation configuration.
- Prefer xarray/native Python operations when they are already available and
  performant enough.

Project style:

- Keep subprocess calls traceable.
- Do not build dynamic script names or shell pipelines to hide control flow.

## Ensemble Mean Problems

### Symptom: Ensemble Mean Does Not Find Members

Likely causes:

- Member files are not adjacent.
- Filenames do not match `ensemble-mean.py` pattern.
- Member id is missing.
- More or fewer members exist than expected.

How to check:

```bash
ls -lh /path/to/output/nwm.t*z.*_*.*.nc
python scripts/ensemble-mean.py /path/to/member.nc /tmp/out.nc 4
```

Possible fixes:

- Put all intended members in one directory.
- Correct filenames.
- Use `--allow-overcount` only when extra matching members are understood.

### Symptom: Ensemble Mean Values Look Wrong

Likely causes:

- Packed integer data was opened with `mask_and_scale=False`.
- Scale/fill metadata is in attrs rather than encoding.
- Coordinates differ across members but are copied from the first member.
- Shape mismatch was padded instead of rejected.

How to check:

```bash
ncdump -h /path/to/member.nc | grep -E "scale_factor|add_offset|_FillValue|missing_value"
```

Compare a manual small-slice mean with the script output.

Possible fixes:

- Decode packed values correctly.
- Require exact shape and coordinate equality.
- Consider replacing manual streaming with controlled dask/xarray concat and
  mean logic.

Project style:

- Prefer correctness and explicit validation over clever low-memory shortcuts.

## Download Script Problems

### Symptom: `download_input.py` Finds No Data

Likely causes:

- Wrong date or cycle.
- Product is not available on the source server.
- Source URL changed.
- Directory naming assumptions no longer match NOMADS.
- Frame regex is too narrow or invalid.

How to check:

```bash
python scripts/download_input.py --help
curl -I https://nomads.ncep.noaa.gov/pub/data/nccf/com/nwm/v3.0/
```

Possible fixes:

- Correct date/cycle/member/product.
- Use `--url` or `PP_DOWNLOAD_INPUT_BASE_URL` for a mirror.
- Update `form_configuration_link()` if the remote layout changed.

Project style:

- Keep download tooling clearly marked as development/support tooling.
- Do not turn ad hoc download assumptions into production behavior without tests.

## Timing And Performance Problems

### Symptom: Runtime Is Much Slower Than Expected

Likely causes:

- Full loads where lazy/chunked data would be better.
- Lazy data computed repeatedly.
- Too much compression or expensive encoding.
- Repeatedly loading masks/RouteLinks instead of using caches.
- Threading disabled or too limited for safe non-IO work.

How to check:

- Enable timing logs if configured.
- Use `scripts/analyze_timing.py`.
- Add targeted timing with `timed_function()`.
- Compare file sizes and encodings.

Possible fixes:

- Move repeated work out of per-file loops.
- Use vectorized NumPy/xarray operations.
- Tune encoding/compression intentionally.
- Avoid broad refactors until a timing profile identifies the bottleneck.

Project style:

- Measure before optimizing.
- Keep performance improvements local and explain the tradeoff.

### Symptom: Memory Usage Explodes

Likely causes:

- Loading too many full datasets at once.
- Holding references to xarray datasets after contexts close.
- Creating multiple full-size temporary arrays.
- Using `open_mfdataset` on many huge files without chunk control.

How to check:

- Inspect recent code for `.load()`, `.compute()`, and `copy(deep=True)`.
- Check whether data is accumulated in Python lists.
- Look at array shapes and dtypes.

Possible fixes:

- Stream by file or by chunk.
- Drop unneeded variables early.
- Avoid full-size scratch arrays.
- Close datasets explicitly.

Project style:

- Keep memory behavior obvious in code.
- Add comments only where the ownership/lifetime is non-obvious.

## Logging Problems

### Symptom: Logs Are Too Noisy

Likely causes:

- Root logger level is too low.
- Third-party loggers are not quieted.
- Debug mode is enabled.
- JSON/file handlers are collecting more than intended.

How to check:

```bash
post-process --settings /path/to/input.nc /path/to/output
grep -nE "handlers|loggers|level" resources/python_log_config.json
```

Possible fixes:

- Use `PP_LOG_CONFIG_PATH`.
- Use `PP_LOG_LEVEL_OVERRIDE_PATH`.
- Quiet specific third-party loggers.

Project style:

- Use dedicated module loggers.
- Do not replace structured logging with print statements in application code.

### Symptom: Logs Do Not Show Enough Detail

Likely causes:

- Log level is too high.
- Verbosity is low.
- The failing code swallows context.

How to check:

- Increase log level and verbosity.
- Search for `Failure in:` wrappers in logs.

Possible fixes:

- Add targeted debug logs near operation boundaries.
- Include paths, variable names, dimensions, and profile operation names.
- Avoid logging huge arrays or full datasets unless explicitly peeking.

## Testing Problems

### Symptom: Tests Cannot Find Sample Data

Likely causes:

- Test specification paths are wrong.
- Generated test data was not created.
- Environment paths override defaults.

How to check:

```bash
python -m unittest discover -s test -p "test_*.py"
```

Possible fixes:

- Fix test specification paths.
- Generate required sample data.
- Clear unexpected `PP_*` overrides.

Project style:

- Use focused tests for new failure modes.
- Keep tests deterministic.
- Do not write tests that depend on a developer's local absolute paths.

## Code Style For Fixes

Follow `StyleAndApproach.md`. The most important rules for debugging fixes are
below.

### Prefer Functions Over Classes

Use classes for stateful concepts and transfer objects. Use functions for
operations. This makes work easier to parallelize and easier to test.

### Keep Data Flat

Prefer simple dataclasses, mappings, and sequences over deeply nested objects.
Flat data moves more safely across threads/processes and is easier to log.

### Avoid New Dependencies

The target environments can be restrictive. Do not add dependencies unless the
maintainers agree they are necessary.

### Use Descriptive Names

Avoid abbreviations such as `df`, `idx`, `x`, `y`, or `t` unless the identifier
is an established domain name. Prefer names like `streamflow_data`,
`feature_index`, or `reference_time`.

### Avoid Magic Literals

Move repeated strings and numbers into constants with docstrings. This is
especially important for variable names such as `streamflow`, `feature_id`,
`time`, and `reference_time`.

### Keep Control Flow Traceable

Do not hide core behavior behind dynamic script names, shell pipelines, or
string-built function calls. Profile function dispatch exists, but new code
should keep call paths explicit.

### Use Project NetCDF Helpers

Prefer `post_processing.utilities.netcdf` for NetCDF reads and writes. Respect
work directories and temporary `.incomplete` writes.

### Type Hint And Document

Add type hints to new functions and return values. Add docstrings to modules,
functions, classes, and important constants.

### Logging

Use a module logger:

```python
LOGGER: logging.Logger = logging.getLogger(pathlib.Path(__file__).stem)
```

or the project logging helper when appropriate. Log enough context to diagnose
paths, variables, dimensions, and operation names.

### Error Messages

Good errors should state:

- what operation failed
- what path or variable was involved
- what was expected
- what was actually found
- what the user can inspect next

Avoid generic `Exception("failed")` messages.

### Tests

When fixing a bug, add a test if the behavior can be isolated. At minimum, add a
small test for:

- filename parsing changes
- profile deserialization changes
- shape/coordinate validation
- data transformations with known tiny arrays

## When To Change Code Vs Data

Change data or configuration when:

- a source file is malformed
- a directory contains stale inputs
- a resource path points to the wrong place
- a deployment omitted required masks/RouteLinks/projections

Change code when:

- the application silently accepts invalid inputs
- an error message hides the real cause
- valid NWM files are rejected
- file handles or temporary files are mishandled
- the same workaround is needed repeatedly across environments

## Debugging Mindset

Prefer narrowing the problem over guessing. The project is built as a pipeline:

```text
input discovery -> profile selection -> operation sequence -> NetCDF IO -> output save
```

Find the first stage where reality differs from expectation. Fix that stage
with explicit validation, clear errors, and code that follows the surrounding
patterns.
