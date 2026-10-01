#!/usr/bin/env python3
"""Per-invocation CLI measurements; not inference or model acceptance.

An isolated worker observes exactly one completed child. RUSAGE_CHILDREN in
the long-lived benchmark driver would instead retain earlier children.
Authority: https://docs.python.org/3/library/resource.html#resource.RUSAGE_CHILDREN
"""

import json
import ctypes
import math
import os
from pathlib import Path
from contextvars import ContextVar
import hashlib
import signal
import selectors
import statistics
import subprocess
import sys
import tempfile
import threading
import time


class MeasurementError(RuntimeError):
    def __init__(self, message, sample=None, samples=None):
        super().__init__(message)
        self.sample = sample
        self.samples = samples or ([] if sample is None else [sample])


OUTPUT_LIMIT = 1024 * 1024
_RECORDER = ContextVar("benchmark_recorder", default=None)


def _children():
    return [int(pid) for pid in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()]


def _subreaper(enable):
    # Verification runs in the pinned Linux SDK on every host platform.
    # Linux man-pages: PR_SET_CHILD_SUBREAPER(2const). Never reap other work.
    if not sys.platform.startswith("linux") or threading.active_count() != 1:
        raise ValueError("use a single-threaded process inside the pinned Linux SDK")
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ValueError("measurement supervisor requires exclusive child reaping")
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot read subreaper state")
    if libc.prctl(36, ctypes.c_ulong(int(enable)), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot establish subreaper state")
    return previous.value


def _cleanup_children():
    """Signal only owned, unreaped children; adopted descendants are reaped too."""
    deadline = time.monotonic() + 2
    killed = 0
    while time.monotonic() < deadline:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return True, killed  # ECHILD, not merely a transient empty /proc list.
        for pid in _children():
            try:
                descriptor = os.pidfd_open(pid)
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    killed += 1
                finally:
                    os.close(descriptor)
            except ProcessLookupError:
                pass
        time.sleep(.005)
    return False, killed


def _worker(command, timeout):
    import resource
    _subreaper(True)
    started = time.perf_counter()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    status = "completed"
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while process.poll() is None or selector.get_map():
                remaining = timeout - (time.perf_counter() - started)
                if remaining <= 0:
                    status = "timeout"
                    break
                if process.poll() is not None and _children():
                    status = "descendants-survived"
                    break
                for key, _ in selector.select(min(remaining, .02)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    available = OUTPUT_LIMIT - sum(map(len, buffers.values()))
                    buffers[key.data].extend(data[:available])
                    if len(data) > available:
                        status = "output-limit"
                        break
                if status != "completed":
                    break
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                status = "cleanup-failed"
        clean, killed = _cleanup_children()
        process.stdout.close()
        process.stderr.close()
    if not clean:
        status = "cleanup-failed"
    elif killed and status == "completed":
        status = "descendants-survived"
    return {"status": status, "command": command,
            "returncode": process.returncode, "elapsed_ms": (time.perf_counter() - started) * 1000,
            "cleanup_confirmed": clean, "output_limit_bytes": OUTPUT_LIMIT,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024,
            **{key: data.decode("utf-8", errors="replace") for key, data in buffers.items()}}


def _run_sample(command, timeout=30):
    if not command or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("a command and positive finite timeout are required")
    if _children():
        raise MeasurementError("measurement supervisor must not own unrelated child processes")
    previous = _subreaper(True)
    started = time.perf_counter()
    failure = None
    try:
        worker = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), json.dumps(command), str(timeout)],
            capture_output=True, text=True, timeout=timeout + 5,
        )
    except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
        failure = error
    finally:
        clean, killed = _cleanup_children()
        _subreaper(previous)
    if failure is not None or not clean or killed:
        sample = {"command": command, "status": "worker-failed" if clean else "cleanup-failed",
                  "elapsed_ms": (time.perf_counter() - started) * 1000,
                  "cleanup_confirmed": clean, "error": str(failure), "returncode": None}
        raise MeasurementError("worker execution failed; no successful sample", sample) from failure
    if worker.returncode:
        sample = {"command": command, "status": "worker-failed", "returncode": worker.returncode,
                  "cleanup_confirmed": clean, "stderr": worker.stderr[:1024]}
        raise MeasurementError("measurement worker failed: " + worker.stderr[:1024], sample)
    try:
        sample = json.loads(worker.stdout)
    except (ValueError, TypeError) as error:
        raise MeasurementError("measurement worker returned invalid JSON") from error
    if sample["status"] != "completed" or sample["returncode"] != 0:
        raise MeasurementError(
            f"command did not complete successfully ({sample['status']}, "
            f"exit {sample['returncode']}): {sample['stderr'][:1024]}", sample)
    return sample


def run_sample(command, timeout=30):
    try:
        sample = _run_sample(command, timeout)
    except MeasurementError as error:
        sample = error.sample or {"command": command, "status": "failed", "error": str(error)}
        if _RECORDER.get() is not None:
            _RECORDER.get()(sample)
        raise
    if _RECORDER.get() is not None:
        _RECORDER.get()(sample)
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
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            reject_nonfinite(value)
        return result
    try:
        result = json.loads(sample["stdout"], parse_constant=reject_nonfinite, parse_float=finite_float)
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
    journal = []
    def record(sample):
        entry = {key: value for key, value in sample.items() if key not in ("stdout", "stderr")}
        # Bind captured output without publishing potentially sensitive CLI text.
        for key in ("stdout", "stderr"):
            if key in sample:
                entry[key + "_sha256"] = hashlib.sha256(sample[key].encode()).hexdigest()
        journal.append(entry)
        write_report(path, {**base, "status": "running", "invocations": journal})
    token = _RECORDER.set(record)
    try:
        write_report(path, {**base, "status": "running", "invocations": journal})
        result = collect()
        write_report(path, {**base, "status": "completed", "diagnostics": result,
                            "invocations": journal})
    except Exception as error:
        write_report(path, {**base, "status": "failed", "error": str(error),
                            "samples": getattr(error, "samples", []), "invocations": journal})
        raise
    finally:
        _RECORDER.reset(token)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("internal worker expects command JSON and timeout")
        print(json.dumps(_worker(json.loads(sys.argv[1]), float(sys.argv[2])), allow_nan=False))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
