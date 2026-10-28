Performance testing
===================

The correctness suite exercises performance-sensitive work, but its Google Test
and CTest durations are not benchmarks. ``test/unit/build.cpp`` mixes indexing,
LAZ encoding, reprojection, continuation, subset merging, and PDAL readback in
the same tests. ``test/unit/info.cpp`` exercises metadata analysis. Most inputs
contain only 100,000 points, with one dataset also split across eight files.
The tests have no repeated samples, throughput measurements, memory accounting,
or saved baselines. ``test/unit/time.cpp`` tests the clock helper by sleeping;
it does not measure Entwine performance. The directory build test is commented
out, and Zstandard coverage is disabled by ``NO_ZSTD``.

Build and run
-------------

The optional benchmark harness uses PDAL and the Python standard library; it
requires no downloaded test data or additional benchmark library. Keep its
optimized build separate from the Debug correctness build:

.. code-block:: bash

   conda activate entwine
   cmake -S . -B build/performance -G Ninja \
     -DCMAKE_BUILD_TYPE=RelWithDebInfo \
     -DCMAKE_PREFIX_PATH="$CONDA_PREFIX" \
     -DWITH_TESTS=ON -DWITH_BENCHMARKS=ON
   cmake --build build/performance --parallel
   cmake --build build/performance --target test
   cmake --build build/performance --target benchmark

The final target writes ``build/performance/test/performance/results.json``.
It runs five workloads, each with one discarded warmup and three measured
samples, using one million points distributed across eight overlapping files.
The regular CTest suite gets two harness correctness tests when benchmarks are
enabled: statistics/error-handling tests and a tiny end-to-end smoke test.
Neither asserts a timing threshold. ``WITH_TESTS=OFF`` also works when only the
benchmark executable and target are needed. The existing ``hobu.sh`` removes
``build/`` on each run, including this benchmark build and its reports; copy
reports elsewhere before using that script again.

Use the runner directly to save a baseline outside the build tree:

.. code-block:: bash

   python3 test/performance/run.py \
     --executable build/performance/test/performance/entwine-benchmark \
     --output /tmp/entwine-before.json
   # Rebuild after a candidate change, then repeat on the same idle machine.
   python3 test/performance/run.py \
     --executable build/performance/test/performance/entwine-benchmark \
     --output /tmp/entwine-after.json --baseline /tmp/entwine-before.json

Comparisons are informational unless ``--max-regression-percent 15`` (for
example) is supplied. That optional gate returns exit code 2 when median wall
time or peak RSS exceeds the threshold; harness/workload errors return 1.
Reports contain raw samples, median, min, max, and median absolute deviation
(MAD), including percent changes for all measured metrics. A baseline with
different inputs, workload settings, host, compiler, build type, compiler flags,
or PDAL version is rejected. Timing thresholds should only be used on stable
hardware after establishing normal variation; they are not statistical
significance tests.

Executable and Entwine library SHA-256 hashes identify the built artifacts.
The Git revision and tracked-diff hash describe the checkout at run time; these
do not prove that an old binary was rebuilt or capture untracked source files.
Always rebuild before comparing a code change.

Workloads and measurements
-------------------------

* ``analyze-shallow`` measures header/schema analysis across the input files.
  Its points/second is the number of points described, not points decoded.
* ``analyze-deep`` reads all points and computes dimension statistics.
* ``build-binary`` measures analysis/initialization separately from indexing,
  statistics, node serialization, and final metadata writes.
* ``build-laszip`` performs the same build with compressed LAZ output, including
  the writer's GPS-time sorting. Compare it with binary output to investigate
  the combined sorting/compression/storage cost.
* ``build-reproject`` uses binary output and reprojects to EPSG:26918, isolating
  the added projection work relative to ``build-binary`` for the generated data.
* ``pack-binary`` is an additional opt-in microbenchmark (select it with
  ``--cases pack-binary``). It packs a synthetic in-memory table with scaled
  XYZ, integer attributes, GPS time, color, and origin/point identifiers. It uses
  the selected input point count, not the input files' point values or schema.
  Fixture construction and writing ``packed.bin`` are outside the timers. RSS
  includes the fixture, output buffer, and allocator high-water mark. This case
  isolates conversion/copying cost from tree insertion and disk I/O.

Wall time uses a monotonic clock. Generation, process startup, output validation,
output size accounting, and cleanup are outside the measured region. Each sample
uses a fresh child process and a new output directory. Every build must insert
the expected number of points, report no input errors, and pass a complete PDAL
EPT readback count check. Input analysis errors fail the run. Invalid results
cannot silently become faster benchmark samples.

On macOS and Linux, the report includes process CPU time for the measured work
and peak resident memory in bytes. RSS is the child process lifetime high-water
mark, including loaded libraries and initialization; it is captured before EPT
readback. On other platforms these resource metrics are null. Build results
also include node reads/writes from ``ChunkCache`` and total output bytes.
``create_seconds`` and ``run_seconds`` separate initialization from the build;
they do not yet split insertion, stats, sorting, encoding, and finalization.

All runs are bounded by the requested repetitions and a per-process timeout
(``--timeout``, default 300 seconds, also including validation). Samples are
interleaved in a reproducible shuffled order. The runner hashes inputs before
measurement and does not flush OS caches: this is a warm filesystem-cache
benchmark, not a cold-storage or remote-storage benchmark. It writes partial
reports after samples, marks failed runs incomplete, and cleans temporary data.
Place ``--work-dir`` on the storage device you want to measure. Generation is
serial and holds one input file's points in memory at a time.

Finding optimization opportunities
----------------------------------

Use ``--cases`` to restrict the workloads and ``--repetitions`` to obtain more
samples. Useful experiments include:

* Sweep ``--files 1``, ``8``, and ``32`` at a fixed total ``--points`` count to
  distinguish per-file overhead, parallel insertion, and contention. Files
  overlap spatially, deliberately stressing shared nodes.
* Compare ``--work-threads 1 --clip-threads 3`` against ``2/6`` and ``4/12``.
  These are requested thread pools, not a process CPU cap. Entwine transfers
  unused work threads to serialization when there are fewer input files.
* Compare ``--distribution uniform`` with ``clustered`` to stress deeper trees
  and concentrated node access. Sweep ``--span 32`` and ``128`` to expose the
  tradeoff between node count, allocation, and serialization overhead.
* Use ``--input-format laz`` to include LAZ decoding in the measured work.
  Generated data includes intensity, classification, color, and unsorted GPS
  times, with a fixed seed and LAS header date for reproducibility.
* Use ``--points 20000000 --files 8`` (and a larger timeout if needed) to exceed
  the current 2,097,152-point clipping interval per file. The default small run
  does not exercise periodic clipping within each file. Node read/write counts
  help establish whether a workload actually causes cache churn.
* Pass ``--input tile1.laz tile2.laz`` for representative local data. The runner
  fingerprints the files and discovers their expected point count first.
  Reprojection requires a valid source SRS; choose appropriate cases for your
  dataset. Inputs that legitimately discard points require a different benchmark
  contract and will fail the strict count check here.

Different workload settings intentionally cannot be treated as an identical
baseline comparison; inspect their reports side by side to study scaling.
Small runs mainly expose fixed costs and do not establish production throughput.
Use ``RelWithDebInfo`` for native profiler attribution when a case reveals a
large cost, and retain reports alongside the exact source revision being tested.

Two tuning gaps identified while auditing the suite need separate investigation:
``config::getCacheSize`` and ``config::getSleepCount`` populate build parameters,
but ``ChunkCache::m_cacheSize`` is fixed at 64 and ``Builder::insert`` uses
``heuristics::sleepCount`` directly. Consequently, changing those configuration
values does not currently produce the expected cache/clipping experiment.
The harness deliberately does not advertise them as working tuning controls.
Continuation, subset/merge, remote storage, and hot-loop microbenchmarks beyond
binary packing remain future coverage areas; correctness tests for some of
these paths already exist.

Checking byte compatibility
---------------------------

Retain a separately built executable/library pair from before a change. Do not
rebuild that directory after editing production sources. The byte verifier runs
the old executable twice and the candidate once against the same local files,
then hashes every output file by relative path, including metadata, hierarchy,
and point data. It fails for missing, added, or modified files and performs no
metadata normalization or point sorting to hide differences:

.. code-block:: bash

   python3 test/performance/verify_bytes.py \
     --before build/benchmark-before/test/performance/entwine-benchmark \
     --after build/performance/test/performance/entwine-benchmark \
     --input /data/tile1.las /data/tile2.las \
     --output /tmp/entwine-byte-check.json --span 32

By default it checks binary output, LAZ output, reprojected binary output, and
the packing microbenchmark. It uses one insertion thread and three serialization
threads to control ordering. Identical outputs in this controlled setting do
not establish deterministic byte output for all multithreaded builds. Override
``--work-threads`` to examine that behavior; differences between the two old
builds are reported separately from differences involving the candidate and
cause the check to fail. Compare with the same dependencies and LAS header date.
The JSON report includes input and executable/library hashes and all output
hashes. Temporary EPT trees are removed after checking.

Performance branch workflow
---------------------------

Keep reusable benchmark infrastructure and compatibility checks on ``benchmark``.
Create a separate branch from that base for each independent optimization. If an
optimization depends on another, start its branch from the prerequisite branch
so the dependent branch includes the prerequisite commits. Record the dependency
and measure the incremental change against its prerequisite.

Keep each branch's implementation, validation, and measurements scoped to that
optimization. Combined experimental results cannot be attributed to individual
branches without separate measurements.

Initial bottleneck investigation (2026-10-28)
--------------------------------------------

A three-second native stack sample of a five-million-point binary build
(``span=32``, eight overlapping input files) identified ``Chunk::insert`` as the
largest active Entwine leaf in the sample: 462 samples, mostly attributed to the
per-tube ``std::map`` lookup at ``tube.map[pos.z]``. Binary packing stacks also
showed repeated PDAL field dispatch, small memory copies, and layout lookups.
These are sampled stack counts across threads, not percentages of elapsed time;
idle worker waits dominate the unfiltered stack list.

An initial combined experiment targets ``io::binary::pack``:

* Precompute copies for non-XYZ fields whose source and destination types match,
  combining adjacent fields only when they are contiguous in both layouts.
* Keep the existing PDAL numeric-conversion path for differing types.
* Move the completed packed buffer out of its table instead of copying it.

These are two independent optimizations, now separated into
``codex/perf-binary-field-copy`` and ``codex/perf-binary-buffer-move``. The
measurements below describe the original combined experiment, not either
individual branch. The ``benchmark`` branch retains the original implementation.

XYZ scale/offset arithmetic, rounding, field order, point order, and tree
construction remain on their original paths. Byte-level unit tests exercise
nonstandard dimension order, scaled and unscaled XYZ, half-value rounding,
uint64 values above double precision, floating-point NaN payloads/negative
zero/infinity, narrowing conversion, and conversion errors.

The initial local results used AppleClang 21, PDAL 2.10.2, macOS arm64,
``RelWithDebInfo``, five million points, eight LAS files, ``span=32``, two work
threads, six clip threads, one warmup, and five measured samples per workload:

.. list-table:: Median before/after measurements
   :header-rows: 1

   * - Metric
     - Before
     - After
   * - Isolated pack wall time
     - 0.4653 s
     - 0.1853 s
   * - Pack process peak RSS (includes fixture)
     - 817.4 MiB
     - 621.8 MiB
   * - Full binary build wall time
     - 3.6146 s
     - 3.4566 s
   * - Full binary build CPU time
     - 7.9852 s
     - 7.2508 s

The roughly 60% packing improvement translates to a much smaller 4.4% full-build
wall-time reduction on this workload. These are sequential before/after batches,
not a randomized paired experiment or a production-wide speedup claim. Both
builds wrote 1,193 nodes with zero cache reads, so cache churn remains unmeasured.
The exact-output check used two of those input files (1.25 million points), one
work thread and three clip threads: all 526 files matched across binary, LAZ,
reprojected binary, and packing workloads, including two runs of the old code.
Repeating the binary check with two insertion threads changed all 169 node data
files even between two runs of the old code; metadata and hierarchy were stable.
The candidate comparison also differed in those 169 files. This establishes
existing nondeterministic data ordering, not byte equality for concurrent
builds; the verifier correctly reports failure for that experiment.

Next profiling priorities are the per-tube tree lookup/allocation and lock cost
in ``Chunk::insert``, followed by ``Clipper::get`` and repeated bounds traversal.
A more compact lookup structure could reduce allocation and pointer chasing,
but must preserve representative selection and point insertion order. LAZ
sorting/encoding and reprojection still warrant their own native profiles before
changing their algorithms; altering sort tie handling, floating-point operation
order, or statistics accumulation can change output bytes.
