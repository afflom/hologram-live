#!/usr/bin/env python3
"""Per-invocation CLI measurements; not inference or model acceptance.

An isolated worker observes exactly one completed child. RUSAGE_CHILDREN in
the long-lived benchmark driver would instead retain earlier children.
Authority: https://docs.python.org/3/library/resource.html#resource.RUSAGE_CHILDREN
"""

import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import time


class MeasurementError(RuntimeError):
    def __init__(self, message, sample=None, samples=None):
        super().__init__(message)
        self.sample = sample
        self.samples = samples or ([] if sample is None else [sample])


def _worker(command, timeout):
    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=(os.name == "posix"))
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        stdout, stderr = process.communicate()
    elapsed = (time.perf_counter() - started) * 1000
    rss = None
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        import resource
        raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        rss = raw / (1024 if sys.platform.startswith("linux") else 1024**2)
    return {"status": "timeout" if timed_out else "completed",
            "returncode": process.returncode, "elapsed_ms": elapsed,
            "peak_rss_mib": rss, "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace")}


def run_sample(command, timeout=30):
    if not command or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("a command and positive finite timeout are required")
    worker = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), json.dumps(command), str(timeout)],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    if worker.returncode:
        raise MeasurementError("measurement worker failed: " + worker.stderr[:1024])
    try:
        sample = json.loads(worker.stdout)
    except (ValueError, TypeError) as error:
        raise MeasurementError("measurement worker returned invalid JSON") from error
    if sample["status"] != "completed" or sample["returncode"] != 0:
        raise MeasurementError(
            f"command did not complete successfully ({sample['status']}, "
            f"exit {sample['returncode']}): {sample['stderr'][:1024]}", sample)
    return sample


def measure(command, iterations=25, timeout=30):
    if type(iterations) is not int or iterations <= 0:
        raise ValueError("iterations must be a positive integer")
    samples = []
    for index in range(iterations):
        try:
            sample = run_sample(command, timeout)
        except MeasurementError as error:
            raise MeasurementError(
                f"iteration {index + 1}/{iterations} failed; no successful aggregate: {error}",
                error.sample, samples + error.samples) from error
        samples.append(sample)
    durations = sorted(sample["elapsed_ms"] for sample in samples)
    rss = [sample["peak_rss_mib"] for sample in samples]
    mean = statistics.mean(durations)
    if not math.isfinite(mean) or mean <= 0:
        raise MeasurementError("invalid elapsed-time samples")
    return {
        "iterations": iterations, "mean_ms": mean,
        "median_ms": statistics.median(durations), "min_ms": durations[0],
        "max_ms": durations[-1], "p95_ms": durations[math.ceil(.95 * iterations) - 1],
        "stdev_ms": statistics.stdev(durations) if iterations > 1 else 0.0,
        "peak_rss_mib": max(rss) if all(value is not None for value in rss) else None,
        "mean_peak_rss_mib": statistics.mean(rss) if all(value is not None for value in rss) else None,
        "throughput_ops_sec": 1000 / mean,
        "measurement_scope": "single CLI process spawn-to-exit; not in-process dispatch or inference",
        "rss_scope": "OS child high-water mark per isolated invocation; not simultaneous process-tree total",
        "samples": [{key: value for key, value in sample.items()
                     if key not in ("stdout", "stderr")} for sample in samples],
    }


def run_json(command, timeout=30):
    sample = run_sample(command, timeout)
    def reject_nonfinite(value):
        raise ValueError(f"non-finite JSON number: {value}")
    try:
        result = json.loads(sample["stdout"], parse_constant=reject_nonfinite)
    except ValueError as error:
        raise MeasurementError("command returned invalid JSON", sample) from error
    if not isinstance(result, dict):
        raise MeasurementError("command JSON must be an object", sample)
    return result, sample


def write_report(path, report):
    """Atomically replace diagnostics, never leaving a prior success after failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def report_run(path, collect):
    """Retain failure evidence; completion of diagnostics is not product acceptance."""
    base = {"acceptance": "not-established", "started_at_unix": time.time()}
    write_report(path, {**base, "status": "running"})
    try:
        result = collect()
        write_report(path, {**base, "status": "completed", "diagnostics": result})
    except Exception as error:
        write_report(path, {**base, "status": "failed", "error": str(error),
                            "samples": getattr(error, "samples", [])})
        raise


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("internal worker expects command JSON and timeout")
        print(json.dumps(_worker(json.loads(sys.argv[1]), float(sys.argv[2])), allow_nan=False))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
