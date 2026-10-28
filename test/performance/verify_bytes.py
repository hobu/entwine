#!/usr/bin/env python3
"""Compare exact output files from two benchmark executables on identical inputs."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from run import digest, invoke, positive, save

CASES = ("build-binary", "build-laszip", "build-reproject", "pack-binary")


def hashes(root):
    result = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}
    if not result:
        raise ValueError("No output files to compare: " + str(root))
    return result


def differences(left, right):
    return sorted(name for name in left.keys() | right.keys() if left.get(name) != right.get(name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSON report")
    parser.add_argument("--span", type=positive, default=32)
    parser.add_argument("--work-threads", type=positive, default=1)
    parser.add_argument("--clip-threads", type=positive, default=3)
    parser.add_argument("--timeout", type=positive, default=300)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    args = parser.parse_args()
    if args.span & (args.span - 1) or args.clip_threads < 3:
        parser.error("span must be a power of two and clip threads must be at least 3")
    before, after = args.before.resolve(strict=True), args.after.resolve(strict=True)
    inputs = [str(p.resolve(strict=True)) for p in args.input]
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"complete": False, "identical": False, "cases": {},
              "before": str(before), "after": str(after),
              "inputs": [{"path": p, "sha256": digest(p)} for p in inputs],
              "span": args.span, "threads": [args.work_threads, args.clip_threads]}
    for name, executable in (("before", before), ("after", after)):
        build = json.loads(subprocess.check_output([str(executable), "--describe"],
                                                  text=True, timeout=args.timeout))
        report[name + "_build"] = dict(build, executable_sha256=digest(executable),
                                        library_sha256=digest(build["library_path"]))
    save(output, report)
    try:
        with tempfile.TemporaryDirectory(prefix="byte-check-", dir=output.parent) as temp:
            root = Path(temp)
            points = invoke(before, {"operation": "inspect", "input": inputs},
                            root / "inspect", args.timeout)["points"]
            for case in args.cases:
                snapshots = []
                # Detect existing nondeterminism before judging the candidate.
                for name, executable in (("before-1", before), ("before-2", before), ("after", after)):
                    directory = root / (case + "-" + name)
                    request = {"operation": "sample", "case": case, "input": inputs,
                               "output": str(directory / "output"), "points": points,
                               "span": args.span, "threads": report["threads"],
                               "analysis_threads": sum(report["threads"])}
                    print(f"{case}: {name}", flush=True)
                    invoke(executable, request, directory, args.timeout)
                    snapshots.append(hashes(directory / "output"))
                unstable = differences(snapshots[0], snapshots[1])
                changed = differences(snapshots[0], snapshots[2])
                report["cases"][case] = {"baseline_differences": unstable,
                                         "candidate_differences": changed,
                                         "file_hashes": dict(zip(("before-1", "before-2", "after"), snapshots))}
                save(output, report)
            report["complete"] = True
            report["identical"] = all(not c["baseline_differences"] and not c["candidate_differences"]
                                      for c in report["cases"].values())
    except Exception as error:
        report["error"] = str(error)
        save(output, report)
        raise
    save(output, report)
    print("All output files are byte-identical." if report["identical"] else
          "Byte differences detected; inspect baseline repeatability and candidate differences.")
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
