# Work Package

`post_processing.work` is the concurrency and task-execution layer for this
project. It exists because the post-processing workflow often needs to read,
transform, and write large NetCDF files while also using threads or process
workers for CPU-heavy operations. NetCDF, xarray, HDF5, file handles, and lazy
Dask arrays can interact badly when many parts of the code open and write files
at the same time. This package centralizes that behavior.

The short version:

```text
profile operation
  -> calls a helper such as post_processing.utilities.netcdf.write/load/select
  -> helper builds a DataTask
  -> Gateway queues that task
  -> Gateway thread executes it
  -> caller waits with cycle_future/cycle_futures
  -> result is returned or an exception is surfaced
```

This package does not know NWM product semantics. It knows how to fan out Python
calls, schedule NetCDF IO tasks, wait for results, and shut things down without
leaving too many open file handles behind.

## Problem Domain

The rest of the project works with National Water Model output. Those files are
large NetCDF datasets. Common operations include:

- opening one or many NetCDF files with xarray
- selecting a variable or a coordinate subset
- applying a Python function to a dataset or data array
- writing a transformed dataset to a new NetCDF file
- doing many of those operations in parallel across files

The hard part is not just "run things concurrently." The hard part is doing it
while keeping memory pressure, file handles, and library thread-safety problems
under control.

## Package Map

```text
post_processing/work/
  __init__.py              public re-exports for common orchestration helpers
  communication.py         queue/signal factory for the chosen concurrency mode
  exceptions.py            gateway-specific exception classes
  gateway.py               queue-backed IO gateway and threaded implementation
  orchestration.py         fan-out and future polling helpers
  tasks/
    __init__.py            package marker
    base.py                DataTask base class and PendingTaskResult factory
    reading.py             NetCDF read/select/transform task classes
    writing.py             NetCDF write and read-transform-write task classes
```

`__pycache__` directories are runtime artifacts and are not part of the source
contract.

## Expected Control Flow

### Direct Fan-Out Work

Many profile operations build a list or mapping of argument sets and call one of
the orchestration helpers:

```text
operation builds args
  -> starmap_executor(function, args, executor, fallback_to_threads=True)
    -> if executor exists, submit all calls to it
    -> if executor is missing and fallback is true, use threads
    -> otherwise run sequentially with starmap
  -> cycle_futures waits for all submitted jobs
  -> return list/dict of results, or raise grouped exceptions
```

Use this path for ordinary Python functions that are safe to call directly in
the selected executor.

### NetCDF IO Work

NetCDF IO normally goes through `post_processing.utilities.netcdf`, which wraps
this package:

```text
netcdf.write(dataset, target)
  -> submit_write()
  -> SaveTask(dataset, target)
  -> Gateway.enqueue(task)
  -> ThreadedGateway.listen()
  -> SaveTask.execute()
  -> _write_to_disk()
  -> cycle_future() waits for completion
```

The same pattern applies for loading, selecting, transforming, and
read-transform-write operations.

### Why A Gateway?

The gateway serializes task execution through a queue and a dedicated thread.
That gives the codebase one place to control NetCDF opening/writing behavior
instead of letting every worker thread write independently.

The current implementation is thread-based only. Process and MPI/node
communication are represented in the code but are not implemented.

## Expected Inputs And Outputs

### Orchestration Inputs

`starmap`, `starmap_threaded`, and `starmap_executor` accept either:

- a sequence of positional argument sequences
- a sequence of keyword argument mappings
- a sequence of `(args, kwargs)` pairs
- a mapping of keys to any of the above

Examples:

```python
starmap(function, [{"path": path, "value": 10}])
starmap(function, [(path, 10)])
starmap(function, [((path,), {"value": 10})])
starmap(function, {"first": {"path": path}})
```

If a mapping is provided, results are returned as a mapping with the same keys.
If a sequence is provided, results are returned as a list.

### Task Inputs

Every gateway task is a `DataTask`. At minimum, it has:

- `target`: usually a `Path`, or sometimes a list of paths
- `engine`: NetCDF backend engine, defaulting to project settings
- `kwargs`: task-specific keyword arguments
- `future`: a Future-like object used to report result or failure

Read tasks expect readable NetCDF files. Write tasks expect xarray datasets and
writable output directories.

### Task Outputs

Task result types vary:

- `SaveTask` returns the written `Path`
- `OperateOnDatasetTask` returns the written `Path`
- `LoadTask` returns an `xarray.Dataset`
- `LoadVariableTask` returns an `xarray.DataArray`
- `ArrayLoadTask` returns a NumPy array
- `SelectTask` returns a fully loaded/deep-copied `xarray.DataArray`
- transform tasks return whatever their callable returns

Callers usually receive these through `cycle_future()` or `cycle_futures()`.

## Public Entry Points

### `__init__.py`

`post_processing.work.__init__` re-exports the most commonly used orchestration
helpers:

- `starmap`
- `starmap_threaded`
- `starmap_executor`
- `cycle_future`
- `cycle_futures`
- `cycle_future_list`
- `cycle_future_mapping`

This is why other modules often import from `post_processing.work` directly.

## orchestration.py

This file handles generic work distribution and result collection. It does not
know anything about NetCDF.

### `starmap`

Runs a function over many argument sets. It is eager: it returns only after all
calls finish.

Behavior:

- if `settings.allow_threading` is true and `thread_count > 0`, it delegates to
  `starmap_threaded`
- otherwise it runs calls sequentially in the current thread
- it supports both list-style and mapping-style argument collections

Gotcha: strings are sequences in Python, so the implementation treats strings
specially to avoid expanding them into characters. Be careful when passing other
custom sequence-like objects.

### `starmap_threaded`

Creates a `ThreadPoolExecutor`, submits all calls through `starmap_executor`,
waits, then closes the pool.

Use it for short-lived thread fan-out where creating a dedicated executor is not
worth the ceremony.

Gotcha: if threading is disabled in settings, this function can still be called
directly. It logs a warning only at very verbose levels.

### `starmap_executor`

Submits work to a provided executor. This is the preferred helper when a profile
already has an executor, such as a process pool.

Control flow:

```text
for each arg set:
  executor.submit(function, ...)
cycle_futures(...)
if exceptions:
  raise condense_exceptions(...)
return results
```

If `executor is None`:

- `fallback_to_threads=True` uses `starmap_threaded`
- otherwise it uses sequential `starmap`

Gotcha: when exceptions occur, successful results are discarded and a grouped
exception is raised.

### `cycle_future`

Polls one Future-like object until it completes. It returns:

```python
(result, None)
```

or:

```python
(None, exception)
```

It does not raise normal task exceptions by itself. Callers decide whether to
raise the returned exception.

### `cycle_futures`, `cycle_future_list`, `cycle_future_mapping`

Poll many Future-like objects until all are done. These helpers are similar in
spirit to `concurrent.futures.as_completed`, but support:

- list and mapping inputs
- result transforms as values arrive
- exception handlers
- timeout/backoff polling

List transforms receive:

```python
transform(result, results_so_far)
```

Mapping transforms receive:

```python
transform(key, result, results_so_far)
```

Gotcha: these helpers keep cycling timed-out futures until they complete. A
future that never completes will keep the caller waiting forever.

### `shutdown_executor`

Attempts to clean up worker-side state before shutting down an executor. It
currently queues repeated `close_gateway` calls into the executor to close
worker gateways. Some older cleanup hooks for masks/projections are present but
commented out.

Use this when the main application is done with a shared executor.

## communication.py

This file chooses the communication primitive used by the gateway.

Current settings:

```python
COMMUNICATE_VIA_THREADS = True
COMMUNICATE_VIA_PROCESSES = False
COMMUNICATE_VIA_NODES = False
```

### `get_signal`

Returns a `threading.Event` today. It may be initialized as set or unset.

### `get_queue`

Returns a `queue.Queue` today.

Gotchas:

- process communication is not implemented
- MPI/node communication is not implemented
- changing the constants is not enough to make process or node communication
  work; the relevant branches raise `NotImplementedError`

## gateway.py

The gateway is a queue-backed task runner. It is mainly used by
`post_processing.utilities.netcdf`.

### `Gateway`

Abstract base class for queueing and executing `DataTask` objects.

Important methods:

- `enqueue(task)`: submit a task and return its future
- `listen()`: queue polling loop
- `execute_task(task)`: mark future running, execute task, set result
- `start()`: implementation-specific startup
- `shutdown()`: implementation-specific shutdown
- `can_queue_task(task)`: implementation-specific queue safety check

The queue accepts a `None` sentinel to request shutdown. It also recognizes
`DyeTask` as a diagnostic marker.

If a task cannot be queued and `run_unqueable_tasks` is true, the gateway runs it
directly. This is mainly to prevent deadlocks when code inside the gateway
thread tries to queue more work into the same gateway.

### `ThreadedGateway`

The current concrete implementation. It starts one thread whose target is
`listen()`.

Queue behavior:

```text
caller thread
  -> enqueue(task)
  -> queue.put(task)
  -> returns task.future

gateway thread
  -> queue.get()
  -> task.execute()
  -> future.set_result(...) or future.set_exception(...)
```

Shutdown behavior:

- clears the run signal
- tries to enqueue `None`
- drains queued jobs and marks their futures with cancellation-style exceptions
- joins the gateway thread

Gotcha: the shutdown drain path assumes queued entries have futures. If the
queue contains `None`, the code selects a `GatewayError` message but then still
attempts to use `job.future`; this path is inside broad shutdown handling but is
worth treating carefully during maintenance.

### `get_gateway`

Factory for the configured gateway implementation. It also sets:

```python
xarray.set_options(file_cache_maxsize=1)
```

That reduces xarray file-cache pressure during repeated IO.

## exceptions.py

Defines gateway-specific exception classes:

- `GatewayError`
- `WriteCancelledByGatewayError`
- `LoadCanceledByGatewayError`

Tasks choose which cancellation exception they produce by overriding
`get_associated_error_type()`.

These exceptions mostly communicate that work was cancelled or interrupted by
gateway shutdown, not necessarily that the underlying NetCDF operation itself
was invalid.

## tasks/base.py

Defines the base task contract.

### `get_pending_task_result`

Returns a Future-like object for the configured communication mode. Today this
is `concurrent.futures.Future`.

Process and node modes are not implemented.

### `DataTask`

Base class for all gateway tasks.

Fields:

- `target`: file path or list of paths
- `engine`: NetCDF backend engine
- `kwargs`: task-specific options
- `future`: where result/exception state is reported
- `_stack`: short creation stack for diagnostics

Methods/properties:

- `__call__()`: subclasses implement actual work
- `execute()`: calls `__call__`
- `status`: `Pending`, `Running`, `Complete`, or `Cancelled`
- `explanation`: string including the task and captured stack
- `get_associated_error_type()`: cancellation exception type

### `DyeTask`

Diagnostic task. When executed, it prints stack traces for all current threads.
The gateway also treats the first dye encounter as a logging marker.

## tasks/reading.py

Read-oriented task classes and the shared `_load()` helper live here.

### `_load`

Central xarray loader used by read tasks and read-transform-write tasks.

Single path behavior:

- retries up to 5 times
- sets `cache=False`
- lazily opens with `xarray.open_dataset` unless `full_load=True`
- for `full_load=True`, uses `xarray.load_dataset` or a `netCDF4` datastore path

Multiple path behavior:

- uses `xarray.open_mfdataset`
- uses `combine="by_coords"`
- loads and closes only if `full_load=True`

Gotchas:

- `open_mfdataset(..., combine="by_coords")` preserves distinct coordinates.
  If input files include multiple `reference_time` values, the merged dataset
  can gain a larger `reference_time` dimension.
- Lazy results can retain file connections. Returning lazy xarray objects from
  transform tasks should be done intentionally.
- `full_load=True` costs memory but reduces lingering file-handle risk.

### `LoadTask`

Loads a dataset and returns an `xarray.Dataset`.

Inputs:

- `target`
- `full_load`
- `engine`
- load kwargs

Output:

- an xarray dataset, lazy by default

### `LoadVariableTask`

Loads one variable and returns an `xarray.DataArray`.

If `full_load=True`, the variable is loaded and deep-copied before return.

Gotcha: with `full_load=False`, the returned data array may still depend on the
source dataset/file.

### `ArrayLoadTask`

Loads one variable and returns a NumPy array copy.

Use this when the caller wants raw array data detached from xarray/file handles.

### `SelectTask`

Loads a variable, selects by coordinate labels with `.sel()`, computes the
selection, deep-copies it, and returns the selected data array.

Inputs:

- `variable_name`
- `criteria`: mapping of dimension/coordinate name to labels
- `method`: optional xarray selection method
- `drop`: whether to drop selected coordinates

Gotcha: criteria keys must be dimensions in `variable.sizes`, not just arbitrary
coordinates.

### `TransformDatasetTask`

Loads a dataset, calls a named function with the dataset and keyword arguments,
and returns the function result.

The function must be callable and must not be a lambda. This matters because
these tasks may be sent through process executors elsewhere, and named functions
are easier to serialize and diagnose.

Gotcha: if the result is an xarray object, the task logs that lazy connections
may remain.

### `TransformVariableTask`

Loads a dataset, selects a variable, optionally applies `.sel()`, optionally
filters the data array, calls a named function, and returns the result.

Inputs:

- `variable_name`
- `function`
- optional `selector`
- optional `selector_method`
- `drop_unselected`
- optional `data_filter`
- `full_load`

Both `function` and `data_filter` must be named callables, not lambdas.

## tasks/writing.py

Write-oriented task classes live here.

### `_write_to_disk`

Writes an xarray dataset to NetCDF safely:

```text
target.parent.mkdir(...)
temporary_output_path = target + ".incomplete"
dataset.compute().to_netcdf(temporary_output_path)
os.replace(temporary_output_path, target)
```

This protects consumers from seeing a partially written final file. The final
rename/replace is atomic on normal local filesystems.

Gotchas:

- if `target.name + ".incomplete"` already exists, writing fails
- every write calls `dataset.compute()`, so lazy data is realized before writing
- on unusual filesystems, atomic replace semantics may differ

### `SaveTask`

Writes a provided dataset to `target` and returns the target path.

Used by `netcdf.submit_write()` and `netcdf.write()`.

Cancellation type:

- `WriteCancelledByGatewayError`

### `OperateOnDatasetTask`

Loads a target dataset, calls a dataset-to-dataset function, writes the altered
dataset to `output_path`, then returns `output_path`.

This is used for operations such as "open a file, drop variables, write a new
file" without exposing the intermediate dataset to the caller.

Inputs:

- `target`: source path
- `function`: named dataset transformation function
- `output_path`
- `read_arguments`
- `write_arguments`
- `kwargs`: function arguments

Gotcha: `chunks` is removed from `read_arguments`; this task controls chunking
internally.

## Relationship To `utilities.netcdf`

Most code should not instantiate tasks directly. Prefer
`post_processing.utilities.netcdf`, which does the common setup:

- opens the shared gateway lazily
- applies default encodings before writes
- builds the correct task type
- enqueues the task
- optionally waits for completion in blocking wrapper functions

Common mapping:

```text
netcdf.write                 -> SaveTask
netcdf.load                  -> LoadTask
netcdf.load_variable         -> LoadVariableTask
netcdf.load_array            -> ArrayLoadTask
netcdf.select                -> SelectTask
netcdf.submit_dataset_*      -> TransformDatasetTask / OperateOnDatasetTask
netcdf.submit_variable_*     -> TransformVariableTask
```

Blocking helpers such as `netcdf.write()` call `submit_*()` and then
`cycle_future()`.

## Common Gotchas

### Lazy Data And File Handles

xarray datasets are often lazy. Returning a lazy `Dataset` or `DataArray` from a
task can keep a file handle alive after the task scope exits. Use
`full_load=True`, `.compute()`, or `.copy(deep=True)` when the caller needs a
detached result.

### Multi-File Loading Aligns By Coordinates

`_load()` uses `open_mfdataset(..., combine="by_coords")` for multiple paths.
That is useful when files represent different times, but it can surprise you if
inputs contain inconsistent scalar/length-one coordinates. xarray may expand a
dimension instead of failing.

### Named Functions Are Required In Transform Tasks

`TransformDatasetTask` and `TransformVariableTask` reject lambdas. Define a
normal function at module scope instead.

### Gateway Shutdown Cancels Queued Work

If the gateway shuts down while tasks are queued, those task futures receive
gateway cancellation exceptions. Callers waiting through `cycle_future()` should
check and raise or handle returned exceptions.

### Queue Size Is Backpressure

Gateway queue length is controlled by environment-backed defaults:

- `PP_NETCDF_QUEUE_LENGTH` through `gateway.DEFAULT_QUEUE_LENGTH`
- `PP_NETCDF_WAIT_SECONDS` through `gateway.DEFAULT_WAIT_SECONDS`
- `PP_WRITER_QUEUE_LENGTH` and `PP_WRITER_WAIT_SECONDS` in
  `utilities.netcdf`

Small queues reduce memory/file pressure but can block producers more often.

### Threaded Only Means Threaded Only

The communication abstraction looks broader than it currently is. Processes and
nodes are placeholders. Do not expect `COMMUNICATE_VIA_PROCESSES=True` or
`COMMUNICATE_VIA_NODES=True` to work without implementation work.

### Temporary `.incomplete` Files Matter

Write failures can leave `.incomplete` files behind. Future writes to the same
target will fail until the temporary file is inspected and removed. This is
intentional: blindly overwriting an incomplete artifact could hide a previous
failure.

## How To Add A New Task

1. Subclass `DataTask[T]`.
2. Add fields for everything the task needs.
3. Implement `__call__()` and return a clear result type.
4. Override `get_associated_error_type()` if cancellation should be specific.
5. Keep xarray/file-handle ownership explicit.
6. Add a helper in `utilities.netcdf` or the calling module to construct and
   enqueue the task.

For transform tasks, prefer named module-level functions over closures or
lambdas.

## How To Choose A Helper

Use `starmap` when:

- the work is simple
- sequential execution is acceptable
- you want settings-controlled optional threading

Use `starmap_threaded` when:

- the work is safe in threads
- you need a short-lived thread pool

Use `starmap_executor` when:

- a profile or caller already owns an executor
- you want process/thread executor behavior selected outside the helper

Use the NetCDF gateway path when:

- opening/writing NetCDF files
- returning xarray data from disk
- applying a small dataset/data-array function inside controlled IO handling

## Operational Checklist

Before using or modifying this package, check:

- Are returned xarray objects lazy or fully loaded?
- Is the function safe to run in a thread or process?
- Will paths be unique, especially `.incomplete` write paths?
- Will multi-file input coordinates align as expected?
- Does the caller check exceptions returned by `cycle_future()`?
- Is gateway shutdown called at application exit?

The main application already calls cleanup through its shutdown path. New
standalone scripts that use this package should do the same if they keep a
gateway or executor alive.

