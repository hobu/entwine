"""Correctness checks for benchmark statistics, compatibility, and failures."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import run
import verify_bytes


def report(wall=1, rss=100):
    samples = [{"wall_seconds": wall, "peak_rss_bytes": rss, "nodes_read": 0}]
    return {"schema_version": 1, "complete": True,
            "configuration": {"points": 100, "threads": [2, 6]},
            "environment": {"host": "test"},
            "build": {"build_type": "Release", "compiler": "test", "pdal_version": "test",
                      "cxx_flags": "-O3"},
            "cases": {"build-binary": {"samples": samples, "summary": run.summarize(samples)}}}


class StatisticsTests(unittest.TestCase):
    def test_median_and_mad_resist_one_outlier(self):
        summary = run.summarize([{"wall_seconds": v} for v in [1, 2, 3, 4, 100]])
        self.assertEqual(summary["wall_seconds"], {"median": 3, "min": 1, "max": 100, "mad": 1})

    def test_unavailable_metrics_are_not_zero(self):
        self.assertNotIn("peak_rss_bytes", run.summarize([{"peak_rss_bytes": None}]))

    def test_comparison_is_informational_by_default(self):
        result = run.compare(report(2), report())
        self.assertEqual(result["percent_change"]["build-binary"]["wall_seconds"], 100)
        self.assertEqual(result["regressions"], [])
        self.assertIsNone(result["percent_change"]["build-binary"]["nodes_read"])

    def test_opt_in_gate_checks_memory_as_well_as_time(self):
        self.assertEqual(len(run.compare(report(1.3, 130), report(), 20)["regressions"]), 2)
        self.assertEqual(run.compare(report(.8, 80), report(), 20)["regressions"], [])

    def test_incompatible_baselines_are_rejected(self):
        for key in ("schema_version", "configuration", "environment"):
            baseline = report()
            baseline[key] = "changed"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                run.compare(report(), baseline)
        for key in ("build_type", "compiler", "pdal_version", "cxx_flags"):
            baseline = report()
            baseline["build"][key] = "changed"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                run.compare(report(), baseline)

    def test_revisions_can_differ(self):
        baseline = report()
        current = copy.deepcopy(baseline)
        current["build"]["entwine_version"] = "new"
        self.assertEqual(run.compare(current, baseline)["regressions"], [])

    def test_incomplete_baseline_rejected(self):
        baseline = report()
        baseline["complete"] = False
        with self.assertRaisesRegex(ValueError, "incomplete"):
            run.compare(report(), baseline)


class WorkerTests(unittest.TestCase):
    def test_failed_run_is_persisted_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable, source, output = root / "worker", root / "input.las", root / "report.json"
            executable.write_bytes(b"worker")
            source.write_bytes(b"data")
            build = dict(report()["build"], library_path=str(executable))

            def describe(argv, **kwargs):
                if argv[-1] == "--describe":
                    return json.dumps(build)
                raise FileNotFoundError("git unavailable")

            argv = ["run.py", "--executable", str(executable), "--output", str(output),
                    "--input", str(source), "--cases", "build-binary", "--warmups", "0"]
            with patch.object(sys, "argv", argv):
                args = run.arguments()
            with patch.object(run.subprocess, "check_output", side_effect=describe), \
                    patch.object(run, "invoke", side_effect=[{"points": 100}, RuntimeError("bad build")]):
                with self.assertRaisesRegex(RuntimeError, "bad build"):
                    run.run(args)
            saved = json.loads(output.read_text())
            self.assertFalse(saved["complete"])
            self.assertEqual(saved["error"], "bad build")
            self.assertEqual(saved["cases"]["build-binary"]["samples"], [])

    def test_failed_worker_cannot_become_a_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            error = subprocess.CalledProcessError(1, "worker")
            with patch.object(run.subprocess, "run", side_effect=error):
                with self.assertRaises(RuntimeError):
                    run.invoke(Path("worker"), {"operation": "sample"}, Path(temp) / "sample", 1)

    def test_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            error = subprocess.TimeoutExpired("worker", 7)
            with patch.object(run.subprocess, "run", side_effect=error) as call:
                with self.assertRaises(RuntimeError):
                    run.invoke(Path("worker"), {"operation": "sample"}, Path(temp) / "sample", 7)
                self.assertEqual(call.call_args.kwargs["timeout"], 7)

    def test_incomplete_point_count_cannot_become_a_sample(self):
        def fake_worker(argv, **kwargs):
            request = json.loads(Path(argv[1]).read_text())
            Path(request["result"]).write_text(json.dumps({"wall_seconds": 1, "points": 99}))
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(run.subprocess, "run", side_effect=fake_worker):
                with self.assertRaisesRegex(ValueError, "invalid benchmark sample"):
                    run.invoke(Path("worker"), {"operation": "sample", "points": 100},
                               Path(temp) / "sample", 1)


class ByteComparisonTests(unittest.TestCase):
    def test_detects_changed_added_and_missing_files(self):
        self.assertEqual(verify_bytes.differences({"a": "1", "b": "2"},
                                                  {"a": "3", "c": "4"}), ["a", "b", "c"])

    def test_hashes_do_not_normalize_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            metadata = root / "ept.json"
            metadata.write_text('{"points":1}')
            first = verify_bytes.hashes(root)
            metadata.write_text('{ "points": 1 }')
            self.assertEqual(verify_bytes.differences(first, verify_bytes.hashes(root)), ["ept.json"])

    def test_missing_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "No output files"):
                verify_bytes.hashes(Path(temp))


if __name__ == "__main__":
    unittest.main()
