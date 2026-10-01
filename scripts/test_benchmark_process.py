"""Real process tests of measurement mechanics, not Hologram feature oracles."""

import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

from benchmark_process import MeasurementError, measure, run_json, run_sample


class MeasurementTests(unittest.TestCase):
    def load_script(self, filename):
        spec = importlib.util.spec_from_file_location("benchmark_under_test", Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dispatch_script_uses_fail_closed_measurement(self):
        module = self.load_script("compare-prism-performance.py")
        module.BINARY_PATH = Path(sys.executable)
        result = module.measure_command(["-c", "print('ok')"], 2)
        self.assertEqual(len(result["samples"]), 2)
        self.assertIn("not in-process dispatch", result["measurement_scope"])
        with self.assertRaises(MeasurementError):
            module.measure_command(["-c", "exit(8)"], 2)

    def test_scaling_script_does_not_suppress_command_failure(self):
        module = self.load_script("compare-ai-scaling.py")
        # A real executable rejects the Hologram arguments; no product is simulated.
        module.BINARY_PATH = Path(sys.executable)
        with self.assertRaises(MeasurementError):
            module.run_ai_compare("7b", 4096, 2048, 16)

    def command(self, source):
        return [sys.executable, "-c", source]

    def test_all_successful_iterations_are_retained(self):
        result = measure(self.command("print('ok')"), iterations=3)
        self.assertEqual(result["iterations"], 3)
        self.assertEqual(len(result["samples"]), 3)
        self.assertGreater(result["mean_ms"], 0)
        self.assertGreater(result["throughput_ops_sec"], 0)
        self.assertEqual(result["p95_ms"], result["max_ms"])
        json.dumps(result, allow_nan=False)

    def test_nonzero_exit_is_never_discarded(self):
        with self.assertRaises(MeasurementError) as caught:
            measure(self.command("import sys; print('broken', file=sys.stderr); sys.exit(7)"), 3)
        self.assertIn("iteration 1/3", str(caught.exception))
        self.assertEqual(caught.exception.sample["returncode"], 7)

    def test_failure_after_success_is_not_a_partial_average(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "count")
            code = ("from pathlib import Path; import sys; p=Path(" + repr(path) + "); "
                    "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
                    "sys.exit(5 if n==1 else 0)")
            with self.assertRaises(MeasurementError) as caught:
                measure(self.command(code), 3)
            self.assertIn("iteration 2/3", str(caught.exception))
            self.assertEqual(Path(path).read_text(), "2")

    def test_timeout_is_failure(self):
        with self.assertRaises(MeasurementError) as caught:
            run_sample(self.command("import time; time.sleep(30)"), timeout=.1)
        self.assertEqual(caught.exception.sample["status"], "timeout")

    def test_missing_executable_is_failure(self):
        with self.assertRaises(MeasurementError):
            run_sample(["/nonexistent/hologram-benchmark-executable"])

    def test_json_requires_success_and_object(self):
        result, sample = run_json(self.command("print('{\"ok\":true}')"))
        self.assertEqual(result, {"ok": True})
        self.assertGreater(sample["elapsed_ms"], 0)
        for source in ("print('not-json')", "print('[]')", "print('{}'); exit(2)",
                       "print('{\"value\": NaN}')", "print('{\"value\": Infinity}')"):
            with self.subTest(source=source), self.assertRaises(MeasurementError):
                run_json(self.command(source))

    def test_invalid_iterations_and_timeouts(self):
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                measure(self.command("pass"), count)
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                run_sample(self.command("pass"), timeout)

    def test_high_memory_sample_does_not_contaminate_later_sample(self):
        high = run_sample(self.command("a=bytearray(96*1024*1024); a[::4096]=b'x'*24576; print(len(a))"))
        low = run_sample(self.command("print('small')"))
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            self.assertGreater(high["peak_rss_mib"], low["peak_rss_mib"] + 64)
        else:
            self.assertIsNone(low["peak_rss_mib"])


if __name__ == "__main__":
    unittest.main()
