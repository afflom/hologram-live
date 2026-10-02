"""Real process tests of measurement mechanics, not Hologram feature oracles."""

import json
import importlib.util
import hashlib
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest

from benchmark_process import MeasurementError, measure, run_json, run_sample, report_run


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

    def test_cost_and_containment_do_not_suppress_command_failure(self):
        module = self.load_script("compare-prism-performance.py")
        module.BINARY_PATH = Path(sys.executable)
        for collect in (module.benchmark_uor_cost_model, module.benchmark_working_set_containment):
            with self.subTest(collect=collect.__name__), self.assertRaises(MeasurementError):
                collect()

    def test_missing_projection_is_a_recorded_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text('{"status":"completed"}')
            result = subprocess.run([sys.executable,
                str(Path(__file__).with_name("compare-prism-performance.py")),
                "--build-dir", directory, "--output", str(report)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("required projection missing", result.stderr)
            self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def test_pinned_sdk_oracle_mismatch_cannot_validate_projections(self):
        module = self.load_script("compare-prism-performance.py")
        with tempfile.TemporaryDirectory() as directory:
            projections = Path(directory) / "projections"
            projections.mkdir()
            # Deliberately invalid inputs must never be accepted, even if a
            # local validator advertises the same executable name.
            for filename in ("compose.json", "kubernetes.json"):
                (projections / filename).write_text("{}")
            with self.assertRaises(MeasurementError):
                module.benchmark_cluster_projections(Path(directory))

    def test_failure_replaces_prior_report_and_retains_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report_run(report, lambda: measure(self.command("pass"), 2))
            success = json.loads(report.read_text())
            self.assertEqual(success["status"], "completed")
            self.assertEqual(success["acceptance"], "not-established")
            with self.assertRaises(MeasurementError):
                report_run(report, lambda: measure(self.command("exit(7)"), 2))
            failure = json.loads(report.read_text())
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["samples"][0]["returncode"], 7)
            self.assertNotIn("diagnostics", failure)
            self.assertEqual(list(Path(directory).iterdir()), [report])

    def test_failure_in_later_scenario_retains_all_prior_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            def collect():
                measure(self.command("print('first scenario')"), 2)
                run_json(self.command("print('{\"scenario\":2}')"))
                run_sample(self.command("exit(9)"))
            with self.assertRaises(MeasurementError):
                report_run(report, collect)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual([s["returncode"] for s in evidence["invocations"]], [0, 0, 0, 9])
            self.assertIn("stdout_sha256", evidence["invocations"][0])
            self.assertNotIn("stdout", evidence["invocations"][0])

    def test_failed_reports_never_reintroduce_raw_output_through_exceptions(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            secret = Path(directory) / "child-output"
            payload = "private-child-output-must-not-enter-report"
            secret.write_text(payload)
            # The command contains only a path; its output is read by a real
            # subprocess, not supplied to the recorder as synthetic success.
            read = "from pathlib import Path; import sys; value=Path(" + repr(str(secret)) + ").read_text(); "
            for code, collect in [
                (read + "print(value); print(value,file=sys.stderr); exit(7)", measure),
                (read + "print(value)", run_json),
            ]:
                with self.subTest(collector=collect.__name__), self.assertRaises(MeasurementError):
                    report_run(report, lambda: collect(self.command(code)))
                text = report.read_text()
                self.assertNotIn(payload, text)
                data = json.loads(text)
                self.assertEqual(data["status"], "failed")
                self.assertEqual(data["error_type"], "MeasurementError")
                self.assertEqual(data["acceptance"], "not-established")
                self.assertNotIn("diagnostics", data)
                for sample in data["samples"] + data["invocations"]:
                    self.assertNotIn("stdout", sample)
                    self.assertNotIn("stderr", sample)
                    self.assertNotIn("error", sample)
                    self.assertEqual(sample["stdout_sha256"], hashlib.sha256((payload + "\n").encode()).hexdigest())

    def test_interruptions_replace_old_success_and_preserve_exception_and_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            for interruption in (KeyboardInterrupt("private diagnostic"), SystemExit(9), GeneratorExit()):
                report.write_text('{"status":"completed","diagnostics":{"stale":true}}')
                def collect():
                    run_sample(self.command("print('completed before interruption')"))
                    raise interruption
                with self.subTest(kind=type(interruption).__name__), self.assertRaises(type(interruption)) as caught:
                    report_run(report, collect)
                self.assertIs(caught.exception, interruption)
                text = report.read_text()
                self.assertNotIn("private diagnostic", text)
                data = json.loads(text)
                self.assertEqual(data["status"], "interrupted")
                self.assertEqual(data["error_type"], type(interruption).__name__)
                self.assertEqual(len(data["invocations"]), 1)
                self.assertNotIn("diagnostics", data)
                previous = report.read_bytes()
                run_sample(self.command("pass"))
                self.assertEqual(report.read_bytes(), previous, "recorder context was restored")

    def test_actual_sigint_reaps_active_measurement_and_records_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            marker = Path(directory) / "child-pid"
            code = ("import os,signal,time; from pathlib import Path; "
                    f"Path({str(marker)!r}).write_text(str(os.getpid())); "
                    f"os.kill({os.getpid()},signal.SIGINT); time.sleep(30)")
            started = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                report_run(report, lambda: run_sample(self.command(code), timeout=2))
            self.assertLess(time.monotonic() - started, 4)
            self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())
            data = json.loads(report.read_text())
            self.assertEqual(data["status"], "interrupted")
            self.assertEqual(data["error_type"], "KeyboardInterrupt")
            self.assertEqual(len(data["invocations"]), 1)
            self.assertEqual(data["invocations"][0]["status"], "interrupted")
            self.assertTrue(data["invocations"][0]["cleanup_confirmed"])
            self.assertNotIn("diagnostics", data)

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
            self.assertEqual([s["returncode"] for s in caught.exception.samples], [0, 5])
            self.assertEqual(Path(path).read_text(), "2")

    def test_timeout_is_failure(self):
        with self.assertRaises(MeasurementError) as caught:
            run_sample(self.command("import time; time.sleep(30)"), timeout=.1)
        self.assertEqual(caught.exception.sample["status"], "timeout")

    def test_detached_descendants_are_reaped_on_timeout_and_root_exit(self):
        for root_sleeps in (True, False):
            with self.subTest(root_sleeps=root_sleeps), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "pid"
                child = "import time; time.sleep(30)"
                code = ("import subprocess,sys,time; from pathlib import Path; "
                        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
                        f"Path({str(marker)!r}).write_text(str(p.pid)); "
                        + ("time.sleep(30)" if root_sleeps else "sys.exit(0)"))
                started = time.monotonic()
                with self.assertRaises(MeasurementError) as caught:
                    run_sample(self.command(code), timeout=.3)
                self.assertLess(time.monotonic() - started, 3)
                self.assertTrue(caught.exception.sample["cleanup_confirmed"])
                self.assertIn(caught.exception.sample["status"], ("timeout", "descendants-survived"))
                self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())

    def test_worker_death_preserves_failure_and_reaps_target(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "pid"
            code = ("import os,signal,time; from pathlib import Path; "
                    f"Path({str(marker)!r}).write_text(str(os.getpid())); "
                    "os.kill(os.getppid(),signal.SIGKILL); time.sleep(30)")
            with self.assertRaises(MeasurementError) as caught:
                measure(self.command(code), 2, timeout=.3)
            self.assertTrue(caught.exception.sample["cleanup_confirmed"])
            self.assertEqual(len(caught.exception.samples), 1)
            self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())

    def test_continuous_output_is_bounded_and_failed(self):
        code = "import os\nwhile True: os.write(1,b'x'*65536)"
        with self.assertRaises(MeasurementError) as caught:
            run_sample(self.command(code), timeout=2)
        sample = caught.exception.sample
        self.assertEqual(sample["status"], "output-limit")
        self.assertTrue(sample["cleanup_confirmed"])
        self.assertLessEqual(len(sample["stdout"]) + len(sample["stderr"]), sample["output_limit_bytes"])

    def test_stopped_worker_timeout_retains_prior_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = Path(directory) / "count"
            marker = Path(directory) / "pid"
            code = ("import os,signal,time; from pathlib import Path; "
                    f"p=Path({str(counter)!r}); n=int(p.read_text()) if p.exists() else 0; "
                    "p.write_text(str(n+1)); "
                    f"Path({str(marker)!r}).write_text(str(os.getpid())); "
                    "os.kill(os.getppid(),signal.SIGSTOP) if n else None; "
                    "time.sleep(30) if n else None")
            with self.assertRaises(MeasurementError) as caught:
                measure(self.command(code), 2, timeout=.2)
            self.assertEqual(len(caught.exception.samples), 2)
            self.assertEqual(caught.exception.samples[0]["returncode"], 0)
            self.assertTrue(caught.exception.sample["cleanup_confirmed"])
            self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())

    def test_missing_executable_is_failure(self):
        with self.assertRaises(MeasurementError):
            run_sample(["/nonexistent/hologram-benchmark-executable"])

    def test_json_requires_success_and_object(self):
        result, sample = run_json(self.command("print('{\"ok\":true}')"))
        self.assertEqual(result, {"ok": True})
        self.assertGreater(sample["elapsed_ms"], 0)
        for source in ("print('not-json')", "print('[]')", "print('{}'); exit(2)",
                       "print('{\"value\": NaN}')", "print('{\"value\": Infinity}')",
                       "print('{\"value\": 1e999}')", "print('{\"nested\": [{\"value\": -1e999}]}')"):
            with self.subTest(source=source), self.assertRaises(MeasurementError):
                run_json(self.command(source))

    def test_output_digest_binds_original_bytes_and_json_requires_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            def collect():
                for value in (254, 255):
                    run_sample(self.command(f"import os; os.write(1,bytes([{value}]))"))
            report_run(report, collect)
            invocations = json.loads(report.read_text())["invocations"]
            for sample, value in zip(invocations, (254, 255)):
                self.assertEqual(sample["stdout_sha256"], hashlib.sha256(bytes([value])).hexdigest())
                self.assertEqual(sample["stdout_captured_bytes"], 1)
                self.assertFalse(sample["stdout_utf8_valid"])
            self.assertNotEqual(invocations[0]["stdout_sha256"], invocations[1]["stdout_sha256"])
        with self.assertRaisesRegex(MeasurementError, "invalid UTF-8"):
            run_json(self.command("import os; os.write(1,b'{\"value\":\"\\xff\"}')"))

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
