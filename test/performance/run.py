#!/usr/bin/env python3
"""Bounded, opt-in Entwine benchmarks. Uses only the Python standard library."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time

CASES = ("analyze-shallow", "analyze-deep", "build-binary", "build-laszip",
         "build-reproject", "pack-binary")
DEFAULT_CASES = CASES[:-1]
METRICS = ("wall_seconds", "create_seconds", "run_seconds", "cpu_seconds",
           "peak_rss_bytes", "points_per_second", "nodes_written", "nodes_read",
           "output_bytes")


def summarize(samples):
    result = {}
    for metric in METRICS:
        values = [s[metric] for s in samples if s.get(metric) is not None]
        if values:
            median = statistics.median(values)
            result[metric] = {
                "median": median, "min": min(values), "max": max(values),
                "mad": statistics.median(abs(v - median) for v in values),
            }
    return result


def compare(current, baseline, threshold=None):
    if not baseline.get("complete"):
        raise ValueError("Baseline is incomplete")
    for key in ("schema_version", "configuration", "environment"):
        if current[key] != baseline[key]:
            raise ValueError("Incompatible baseline: " + key)
    for key in ("build_type", "compiler", "pdal_version", "cxx_flags"):
        if current["build"][key] != baseline["build"][key]:
            raise ValueError("Incompatible baseline: " + key)
    changes = {}
    regressions = []
    for name, case in current["cases"].items():
        old = baseline["cases"][name]["summary"]
        changes[name] = {}
        for metric, stats in case["summary"].items():
            previous = old[metric]["median"]
            change = 100 * (stats["median"] / previous - 1) if previous else None
            changes[name][metric] = change
            # Explicit opt-in gates for latency and memory only; report all metrics.
            if (threshold is not None and change is not None and
                    metric in ("wall_seconds", "peak_rss_bytes") and change > threshold):
                regressions.append(f"{name}: {metric} +{change:.1f}%")
    return {"percent_change": changes, "regressions": regressions}


def digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def save(path, report):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def invoke(executable, request, directory, timeout):
    directory.mkdir()
    request = dict(request, result=str(directory / "result.json"),
                   tmp=str(directory / "tmp"))
    (directory / "tmp").mkdir()
    request_path = directory / "request.json"
    request_path.write_text(json.dumps(request))
    # File-backed output avoids retaining unbounded child logs in memory.
    with (directory / "worker.log").open("w+") as log:
        try:
            subprocess.run([str(executable), str(request_path)], stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            log.seek(max(0, log.tell() - 8000))
            raise RuntimeError(f"{error}\n{log.read()}") from error
    result = json.loads(Path(request["result"]).read_text())
    if request["operation"] == "sample":
        wall = result.get("wall_seconds", 0)
        if not math.isfinite(wall) or wall <= 0 or result.get("points") != request["points"]:
            raise ValueError("Worker returned an invalid benchmark sample")
    return result


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return value


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, nargs="+", help="local input files instead of generated data")
    parser.add_argument("--points", type=positive, default=1000000, help="total synthetic points")
    parser.add_argument("--files", type=positive, default=8)
    parser.add_argument("--distribution", choices=("uniform", "clustered"), default="uniform")
    parser.add_argument("--input-format", choices=("las", "laz"), default="las")
    parser.add_argument("--seed", type=nonnegative, default=42)
    parser.add_argument("--span", type=positive, default=128)
    parser.add_argument("--work-threads", type=positive, default=2)
    parser.add_argument("--clip-threads", type=positive, default=6)
    parser.add_argument("--repetitions", type=positive, default=3)
    parser.add_argument("--warmups", type=nonnegative, default=1)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(DEFAULT_CASES))
    parser.add_argument("--timeout", type=positive, default=300, help="seconds per worker process")
    parser.add_argument("--work-dir", type=Path, help="parent for temporary data, defaults beside output")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-regression-percent", type=float,
                        help="fail on median wall time or RSS regression beyond this percentage")
    args = parser.parse_args()
    if args.span & (args.span - 1):
        parser.error("--span must be a power of two")
    if args.clip_threads < 3:
        parser.error("Entwine requires at least 3 clip threads")
    if not args.input and args.points < args.files:
        parser.error("--points must be at least --files")
    if len(set(args.cases)) != len(args.cases):
        parser.error("--cases cannot contain duplicates")
    if args.max_regression_percent is not None:
        if (not args.baseline or not math.isfinite(args.max_regression_percent) or
                args.max_regression_percent < 0):
            parser.error("a finite nonnegative regression threshold requires --baseline")
    if args.baseline and args.baseline.resolve() == args.output.resolve():
        parser.error("--output must not overwrite --baseline")
    if args.seed >= 2**64:
        parser.error("--seed must fit in an unsigned 64-bit integer")
    return args


def run(args):
    executable = args.executable.resolve(strict=True)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_parent = (args.work_dir or output.parent).resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    build = json.loads(subprocess.check_output([str(executable), "--describe"],
                                              text=True, timeout=args.timeout))
    library = build.pop("library_path")
    if build["build_type"] not in ("Release", "RelWithDebInfo"):
        print("WARNING: use Release or RelWithDebInfo for performance conclusions.", file=sys.stderr)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    report = {"schema_version": 1, "complete": False, "build": build,
              "binary_sha256": digest(executable), "library_sha256": digest(library),
              "created_unix": time.time(),
              "environment": {"system": platform.platform(), "machine": platform.machine(),
                              "processor": platform.processor(), "cpu_count": os.cpu_count(),
                              "host": platform.node()},
              "execution": {"repetitions": args.repetitions, "timeout": args.timeout,
                            "work_dir": str(work_parent)}, "cases": {}}
    try:
        source_root = Path(__file__).resolve().parents[2]
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root,
                                           stderr=subprocess.DEVNULL, text=True, timeout=10).strip()
        changes = subprocess.check_output(["git", "diff", "--no-ext-diff", "HEAD"],
                                          cwd=source_root, timeout=10)
        report["source_state_at_run"] = {"revision": revision,
                                        "tracked_diff_sha256": hashlib.sha256(changes).hexdigest()}
    except (OSError, subprocess.SubprocessError):
        report["source_state_at_run"] = None
    save(output, report)
    try:
        with tempfile.TemporaryDirectory(prefix="entwine-perf-", dir=work_parent) as temporary:
            root = Path(temporary)
            if args.input:
                inputs = [str(p.resolve(strict=True)) for p in args.input]
                if len(set(inputs)) != len(inputs):
                    raise ValueError("Duplicate input paths")
                points = invoke(executable, {"operation": "inspect", "input": inputs},
                                root / "inspect", args.timeout)["points"]
                dataset = {"kind": "local"}
            else:
                dataset = {"kind": "synthetic-v1", "points": args.points, "files": args.files,
                           "distribution": args.distribution, "seed": args.seed,
                           "input_format": args.input_format}
                generated = invoke(executable, dict(dataset, operation="generate",
                                   output=str(root / "input")), root / "generate", args.timeout)
                inputs, points = generated["input"], generated["points"]
            dataset["inputs"] = [{"bytes": Path(p).stat().st_size, "sha256": digest(p)} for p in inputs]
            report["configuration"] = {"dataset": dataset, "points": points, "span": args.span,
                                       "threads": [args.work_threads, args.clip_threads],
                                       "warmups": args.warmups, "cases": args.cases,
                                       "order_seed": args.seed}
            report["cases"] = {name: {"samples": [], "summary": {}} for name in args.cases}
            if baseline:
                # Reject incompatible data/settings/builds before doing expensive work.
                compare(report, baseline)
            save(output, report)
            order = random.Random(args.seed)
            for repetition in range(-args.warmups, args.repetitions):
                cases = list(args.cases)
                order.shuffle(cases)
                for name in cases:
                    warmup = repetition < 0
                    index = repetition + args.warmups + 1 if warmup else repetition + 1
                    print(f"{'warmup' if warmup else 'sample'} {index}: {name}", flush=True)
                    # A fresh process and directory per sample prevent continuation,
                    # stale output, and lifetime-RSS contamination between samples.
                    with tempfile.TemporaryDirectory(prefix="sample-", dir=root) as sample_dir:
                        request = {"operation": "sample", "case": name, "input": inputs,
                                   "output": str(Path(sample_dir) / "ept"), "points": points,
                                   "span": args.span, "threads": [args.work_threads, args.clip_threads],
                                   "analysis_threads": args.work_threads + args.clip_threads}
                        measured = invoke(executable, request, Path(sample_dir) / "worker", args.timeout)
                    if not warmup:
                        case = report["cases"][name]
                        case["samples"].append(measured)
                        case["summary"] = summarize(case["samples"])
                        save(output, report)
            report["complete"] = True
            if baseline:
                report["comparison"] = compare(report, baseline, args.max_regression_percent)
            save(output, report)
    except Exception as error:
        report["complete"] = False
        report["error"] = str(error)
        save(output, report)
        raise
    for name, case in report["cases"].items():
        summary = case["summary"]
        wall = summary["wall_seconds"]
        rss = summary.get("peak_rss_bytes", {}).get("median")
        memory = f"{rss / 1024**2:.1f} MiB" if rss is not None else "RSS unavailable"
        print(f"{name:18} {wall['median']:.4f} s (MAD {wall['mad']:.4f})  "
              f"{summary['points_per_second']['median']:,.0f} points/s  {memory}")
    comparison = report.get("comparison", {})
    for name, changes in comparison.get("percent_change", {}).items():
        delta = changes["wall_seconds"]
        print(f"{name}: median wall time {delta:+.1f}% versus baseline")
    for regression in comparison.get("regressions", []):
        print("REGRESSION: " + regression, file=sys.stderr)
    print("Results: " + str(output))
    return 2 if comparison.get("regressions") else 0


if __name__ == "__main__":
    try:
        sys.exit(run(arguments()))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Benchmark failed: {error}", file=sys.stderr)
        sys.exit(1)
