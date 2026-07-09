# Installation Instructions

This project installs a Python package plus command line entry points for National
Water Model post-processing. A working install needs both the Python
environment and the static resource data that profiles reference at runtime.

The installed commands are:

- `post-process`: runs the core post-processing application.
- `download-input`: downloads NWM input files using `scripts/download_input.py`.
  This command requires the optional `analysis` extra, or an environment that
  otherwise provides its download dependencies.

Most other files under `scripts/` are source-tree utilities. Run them with
`python scripts/<name>.py ...` from an environment where the package is
installed.

## Environment Assumptions

The normal environment is a Unix-like shell on a host that can read and write
large NetCDF files. Python 3.12 is the current development target.

Expected runtime tools and libraries:

- Python 3.12+ with `pip` and `venv`.
- NetCDF/HDF5-compatible Python packages installed by the project, including
  `netCDF4`, `xarray`, `dask`, `pandas`, `geopandas`, `pyproj`, `affine`, and
  `rasterio`.
- NCO command line tools available on `PATH` when profiles use NCO-backed
  operations. At minimum, expect `ncks`, `ncrcat`, and `ncpdq` to matter.
- A writable output directory for final products.
- A writable intermediate directory for temporary work products.
- Static resource data matching the profile set being run.

For HPC and other shared environments, prefer a dedicated virtual environment
and static resources stored on a shared read-only filesystem. Put outputs and
intermediate files on a writable scratch/work filesystem. Leave threading
disabled unless the deployment has been tested for NetCDF/HDF5 thread safety.

Do not run the application as `python post_processing/__main__.py ...`.
Use `post-process ...` or `python -m post_processing ...` so package imports are
resolved correctly.

## Basic Install

From the repository root:

```shell
python3.12 -m venv venv
. venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

If `python3.12` is not available, try another Python 3 executable:

```shell
python3 -m venv venv
. venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The editable install reads dependencies from `pyproject.toml` and installs the
`post-process` and `download-input` commands. If a deployment process still uses
`requirements.txt`, install it before the editable package:

```shell
python -m pip install -r requirements.txt
python -m pip install -e .
```

Optional analysis/download helper dependencies, including the dependencies
needed by `download-input`, are declared as the `analysis` extra:

```shell
python -m pip install -e ".[analysis]"
```

## Static Data

The Python package install does not create valid operational data by itself. The
profiles under `resources/profiles` reference static NetCDF, CSV, JSON, and
shape data. The checked-in `resources/` tree is the default location, but
production systems may point these paths at a managed shared data directory.

Expected static resources:

| Resource | Default Path | Purpose |
| --- | --- | --- |
| Profiles | `resources/profiles` | JSON control files that select operations for each NWM configuration, output type, member, and region. |
| Logging config | `resources/python_log_config.json` | Default Python logging dictionary configuration. |
| Log level overrides | `resources/log_level_override.json` | Optional logger-specific quieting rules. |
| Masks | `resources/masks` | RFC, regional, and gridded masks used by subset/extract operations. |
| RouteLink files | `resources/routelink` | Channel routing network files used when calculating upstream flow. |
| Projection reference files | `resources/projections` | Reference grids/projections used by reprojection operations. |
| Thresholds | `resources/thresholds` | Statistical threshold files used by anomaly-style operations. |
| Upstream reach tables | `resources/upstream` | Reference gage upstream reach CSV files used by helper workflows. |
| Shape data | `resources/shapes` and `resources/shapefiles` | Vector geometry inputs for mask generation and related utilities. |
| Sample input | `resources/sample` | Local examples and test fixtures, not a substitute for production input staging. |

Not every profile needs every resource. For example, channel routing profiles
typically require RouteLink files and masks, while land/forcing profiles often
require projection reference files and gridded masks. The safest check is to run
summary mode for the exact file/profile you intend to process:

```shell
post-process --summarize /path/to/input/nwm.t00z.short_range.channel_rt.f001.conus.nc /path/to/output
```

To inspect profile resource references directly:

```shell
grep -RIn -e "{mask_path}" -e "{routelink_path}" -e "projections" -e "threshold" resources/profiles
```

Common static filenames expected by the shipped profiles include:

- `resources/routelink/RouteLink_CONUS.nc`
- `resources/routelink/RouteLink_AK.nc`
- `resources/routelink/RouteLink_HI.nc`
- `resources/routelink/RouteLink_PRVI.nc`
- `resources/projections/sphere_lambert.nc`
- `resources/projections/mercator.nc`
- `resources/projections/alaska_sphere_stereographic.nc`
- `resources/projections/alaska_mercator.nc`
- `resources/projections/hawaii_sphere_lambert.nc`
- `resources/projections/hawaii_mercator.nc`
- `resources/projections/prvi_sphere_lambert.nc`
- `resources/projections/prvi_mercator.nc`
- RFC masks such as `abrfc.nc`, `cbrfc.nc`, `cnrfc.nc`, `lmrfc.nc`,
  `marfc.nc`, `mbrfc.nc`, `ncrfc.nc`, `nerfc.nc`, `nwrfc.nc`, `ohrfc.nc`,
  `serfc.nc`, and `wgrfc.nc`
- Regional masks such as `alaska.aprfc.nc`, `hawaii.aprfc.nc`, and
  `prvi.serfc.nc`
- Gridded processing masks such as `processing_mask_1000m_ABRFC.nc` and the
  other RFC/region variants

## Default Paths

Configuration is read through `post_processing.configuration.settings`. The
loader accepts environment variable casing flexibly, but use the uppercase names
below in shell and scheduler files.

| Setting | Environment Variable | Default |
| --- | --- | --- |
| Application resources | `PP_RESOURCE_PATH` | `<installed project>/resources` |
| Profiles | `PP_PROFILE_PATH` | `${PP_RESOURCE_PATH}/profiles` |
| Masks | `PP_MASK_PATH` | `${PP_RESOURCE_PATH}/masks` |
| RouteLink files | `PP_ROUTELINK_PATH` | `${PP_RESOURCE_PATH}/routelink` |
| Thresholds | `PP_THRESHOLD_PATH` | `${PP_RESOURCE_PATH}/thresholds` |
| Logging config | `PP_LOG_CONFIG_PATH` | `${PP_RESOURCE_PATH}/python_log_config.json` |
| Log level overrides | `PP_LOG_LEVEL_OVERRIDE_PATH` | `${PP_RESOURCE_PATH}/log_level_override.json` when present |
| Intermediate directory | `PP_INTERMEDIATE_DIRECTORY` | System temp directory |
| NetCDF engine | `PP_DEFAULT_NETCDF_ENGINE` | `h5netcdf` if installed, otherwise `netcdf4` |
| NetCDF cache size | `PP_NETCDF_CACHE_SIZE` | `3` |
| Lazy NetCDF loading | `PP_LAZY_LOAD_NETCDF` | `False` |
| Threading | `PP_ALLOW_THREADING` | `False` |
| Maximum extra workers | `PP_MAXIMUM_ADDITIONAL_THREADS` | Derived from CPU/process environment |
| Verbosity | `PP_VERBOSITY` | `0` |
| Debug mode | `PP_DEBUG` | `False` |

The profile path is created if missing. Most other resource paths are expected
to already exist when the corresponding setting is accessed.

## Changing Paths

For one shell session:

```shell
export PP_RESOURCE_PATH=/shared/nwm-post-processing/resources
export PP_PROFILE_PATH=/shared/nwm-post-processing/resources/profiles
export PP_MASK_PATH=/shared/nwm-post-processing/resources/masks
export PP_ROUTELINK_PATH=/shared/nwm-post-processing/resources/routelink
export PP_THRESHOLD_PATH=/shared/nwm-post-processing/resources/thresholds
export PP_INTERMEDIATE_DIRECTORY=/scratch/$USER/nwm-post-processing/intermediate
```

For a repository-local setup, review and source one of the sample environment
files:

```shell
. environment.sh
```

`hpc_environment.sh` and `dev_environment.sh` are also templates. Read them
before using them in automation; they document intended overrides, but they are
not a substitute for site-specific module loading, scratch paths, or static data
mounts.

You can also place a `.env` file in the application directory. The settings
loader applies it when the package imports configuration:

```shell
PP_RESOURCE_PATH=/shared/nwm-post-processing/resources
PP_PROFILE_PATH=/shared/nwm-post-processing/resources/profiles
PP_INTERMEDIATE_DIRECTORY=/scratch/my-user/nwm-post-processing/intermediate
PP_ALLOW_THREADING=False
PP_DEFAULT_NETCDF_ENGINE=netcdf4
```

Keep `.env` values literal. Do not rely on shell expansion inside `.env` the way
you would in a sourced shell script.

Profiles should continue to use settings placeholders such as `{resource_path}`,
`{mask_path}`, and `{routelink_path}`. That keeps profiles portable between
developer checkouts, HPC deployments, and test fixtures.

## Input and Output Expectations

The main application takes one representative NWM NetCDF file and an output
directory:

```shell
post-process /path/to/input/nwm.t00z.short_range.channel_rt.f001.conus.nc /path/to/output
```

The input file name must match the NWM naming pattern:

```text
nwm.t<cycle>z.<configuration>.<output_type>[_<member>].<f###|tm####>.<region>.nc
```

Examples:

```text
nwm.t00z.short_range.channel_rt.f001.conus.nc
nwm.t12z.long_range.channel_rt_3.f120.conus.nc
nwm.t18z.analysis_assim.channel_rt.tm00.alaska.nc
```

After parsing the representative file name, the application scans that file's
directory for all sibling files in the same cycle, configuration, output type,
member, and region. Stage only the files that belong to the cycle being
processed. Old files with matching names can be picked up as part of the run.

Outputs are controlled by the selected profile. Profiles commonly write NetCDF
products under subdirectories of the destination path, and some operations write
intermediate products under `PP_INTERMEDIATE_DIRECTORY`. Use summary mode to see
the planned control flow and output naming before running a new profile.

## Verification

After installation, confirm the command entry points resolve:

```shell
post-process version
python -m post_processing version
```

If the analysis/download extra is installed, also confirm the downloader starts:

```shell
download-input --help
```

Confirm settings resolve to the intended paths:

```shell
post-process settings
```

Check that required resource directories and common files exist:

```shell
test -d resources/profiles
test -f resources/python_log_config.json
test -d resources/masks
test -d resources/routelink
test -d resources/projections
test -d resources/thresholds
test -f resources/routelink/RouteLink_CONUS.nc
test -f resources/projections/sphere_lambert.nc
```

Check that NCO programs are available if using profiles that call NCO-backed
operations:

```shell
which ncks
which ncrcat
which ncpdq
```

Validate all configured profiles:

```shell
post-process validate
```

Run the unit tests from the repository root:

```shell
python -m unittest discover -s test -p "test_*.py"
```

For an operational smoke test, use summary mode against a real staged input:

```shell
post-process --summarize /path/to/input/nwm.t00z.short_range.channel_rt.f001.conus.nc /path/to/output
```

Then run with a small known-good input cycle:

```shell
post-process /path/to/input/nwm.t00z.short_range.channel_rt.f001.conus.nc /path/to/output
```

Use `--peek` when you want the application to log headers for produced files:

```shell
post-process --peek /path/to/input/nwm.t00z.short_range.channel_rt.f001.conus.nc /path/to/output
```

## Common Problems

`post-process: command not found` means the virtual environment is not active or
the package was not installed. Activate the venv and rerun `python -m pip
install -e .`.

`No module named post_processing` usually means the command is being run outside
the installed environment, or the package was invoked as
`python post_processing/__main__.py`. Use `post-process` or
`python -m post_processing`.

`Cannot accept ... as input` means the representative input file is missing, is
a directory, is not a NetCDF file, or its name does not match the expected NWM
pattern.

`No profiles were found` means no JSON profile matched the parsed
configuration, output type, member, and region. Check the input filename and
`PP_PROFILE_PATH`.

Missing mask, RouteLink, projection, or threshold errors mean the profile was
valid but its static data could not be found at the configured path. Run
`post-process settings`, check the relevant `PP_*_PATH`, and compare the profile
references with the actual files.

NCO-related failures mean the required NCO command line tools are missing or not
available on `PATH`. Load the site module or install NCO in the runtime
environment.

Unexpected extra time steps, reference times, members, or frames can come from
input staging. The cycle discovery step groups matching sibling filenames in the
same directory; it does not validate that stale files belong to the intended
cycle before grouping them. Keep staging directories clean or isolate each run
in its own input directory.

Permission errors usually mean the destination path or
`PP_INTERMEDIATE_DIRECTORY` is not writable. On shared systems, point both at a
scratch/work location owned by the running user.

NetCDF backend errors can often be isolated by changing
`PP_DEFAULT_NETCDF_ENGINE` between `netcdf4` and `h5netcdf`, assuming both
backends are installed and compatible with the target files.
