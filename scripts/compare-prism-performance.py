#!/usr/bin/env python3
"""
CLI timing and cost-model diagnostics; not inference/deployment acceptance.

CLI timing includes process startup. Both flags invoke the same executable;
their ratio does not establish generated-runtime superiority. Cost-model and
projection sections still require the independent empirical evidence in H20.
"""

import json
import argparse
import hashlib
import shutil

from benchmark_process import MeasurementError, measure, run_json, report_run, sample_evidence
import sys
import time
from pathlib import Path

BINARY_PATH = Path("target/release/hologram")
if not BINARY_PATH.exists():
    BINARY_PATH = Path("target/debug/hologram")

NUM_ITERATIONS = 25


def measure_command(args: list[str], iterations: int = NUM_ITERATIONS) -> dict:
    result = measure([str(BINARY_PATH)] + args, iterations)
    # Preserve the existing decimal-MB report field; canonical samples use MiB.
    rss = result["peak_rss_mib"]
    result["peak_rss_mb"] = rss * 1024**2 / 1_000_000 if rss is not None else None
    return result


def benchmark_uor_cost_model() -> dict:
    """Benchmark UOR/Prism formal cost model calculations vs standard arithmetic."""
    # Test cases: (m, k, n, total_tokens, prefix_tokens)
    test_cases = [
        {"name": "micro_1x4x4", "m": 1, "k": 4, "n": 4, "total": 100, "prefix": 80},
        {"name": "llama3_single_token_layer", "m": 1, "k": 4096, "n": 4096, "total": 512, "prefix": 256},
        {"name": "llama3_batched_prefill", "m": 32, "k": 4096, "n": 4096, "total": 2048, "prefix": 1024},
    ]

    results = []
    for tc in test_cases:
        args = [
            "--prism",
            "ai",
            "cost-model",
            "--m", str(tc["m"]),
            "--k", str(tc["k"]),
            "--n", str(tc["n"]),
            "--total-tokens", str(tc["total"]),
            "--prefix-tokens", str(tc["prefix"]),
            "--json",
        ]
        data, sample = run_json([str(BINARY_PATH)] + args)
        flops = data.get("matmul_flops")
        eff_tokens = data.get("effective_tokens")
        prefix_savings_pct = round((1.0 - (eff_tokens / tc["total"])) * 100, 1)

        results.append({
            "measurement_scope": "unvalidated cost-model output, not actual tensor execution",
            "name": tc["name"],
            "dimensions": f"{tc['m']}x{tc['k']}x{tc['n']}",
            "matmul_flops": flops,
            "total_tokens": tc["total"],
            "prefix_tokens": tc["prefix"],
            "effective_tokens": eff_tokens,
            "kv_cache_savings_pct": prefix_savings_pct,
            "cli_sample": sample_evidence(sample),
            "reported_is_optimal": data.get("is_optimal"),
        })

    return {"cases": results}


def benchmark_cluster_projections(build_dir: Path) -> dict:
    """Time real syntax/schema validators, not deployment reconciliation."""
    compose_path = build_dir.resolve() / "projections/compose.json"
    k8s_path = build_dir.resolve() / "projections/kubernetes.json"
    for path in (compose_path, k8s_path):
        if not path.is_file():
            raise MeasurementError(f"required projection missing: {path}")
    identity = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (compose_path, k8s_path)}
    validator = shutil.which("kubernetes-validator")
    if validator is None:
        raise MeasurementError("locked kubernetes-validator unavailable")
    lock = json.loads(Path("standards.lock").read_text())
    entries = [entry for entry in lock["oracles"]
               if entry["executable"] == "kubernetes-validator"]
    if len(entries) != 1:
        raise MeasurementError("ambiguous or missing Kubernetes oracle lock")
    digest = hashlib.sha256(Path(validator).read_bytes()).hexdigest()
    if digest != entries[0]["wrapper_sha256"]:
        raise MeasurementError("installed Kubernetes oracle does not match standards.lock")
    compose = measure(["docker", "compose", "-f", str(compose_path), "config"], 10)
    kubernetes = measure([validator, str(k8s_path)], 50)
    if identity != {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (compose_path, k8s_path)}:
        raise MeasurementError("projection changed during measurement")
    return {"measurement_scope": "syntax/schema validation, not running-service reconciliation",
            "subjects_sha256": identity, "kubernetes_wrapper_sha256": digest,
            "compose": compose, "kubernetes": kubernetes}


def benchmark_working_set_containment() -> dict:
    """Benchmark working set containment across 7B, 13B, and 70B models."""
    scenarios = [
        {"model": "7b", "ctx": 4096, "prefix": 2048, "budget_gb": 16, "label": "Llama-2-7B @ 4k Context (50% Prefix)"},
        {"model": "7b", "ctx": 32768, "prefix": 16384, "budget_gb": 16, "label": "Llama-2-7B @ 32k Context (50% Prefix)"},
        {"model": "7b", "ctx": 131072, "prefix": 104857, "budget_gb": 16, "label": "Llama-2-7B @ 128k Context (80% Prefix)"},
        {"model": "13b", "ctx": 4096, "prefix": 2048, "budget_gb": 32, "label": "Llama-2-13B @ 4k Context (50% Prefix)"},
        {"model": "13b", "ctx": 32768, "prefix": 16384, "budget_gb": 32, "label": "Llama-2-13B @ 32k Context (50% Prefix)"},
        {"model": "13b", "ctx": 131072, "prefix": 104857, "budget_gb": 32, "label": "Llama-2-13B @ 128k Context (80% Prefix)"},
        {"model": "70b", "ctx": 4096, "prefix": 2048, "budget_gb": 64, "label": "Llama-3.1-70B @ 4k Context (50% Prefix)"},
        {"model": "70b", "ctx": 32768, "prefix": 16384, "budget_gb": 64, "label": "Llama-3.1-70B @ 32k Context (50% Prefix)"},
        {"model": "70b", "ctx": 131072, "prefix": 65536, "budget_gb": 64, "label": "Llama-3.1-70B @ 128k Context (50% Prefix)"},
        {"model": "70b", "ctx": 131072, "prefix": 104857, "budget_gb": 64, "label": "Llama-3.1-70B @ 128k Context (80% Prefix)"},
    ]

    results = []
    for sc in scenarios:
        args = [
            "--prism",
            "ai",
            "compare",
            "--model", sc["model"],
            "--context-length", str(sc["ctx"]),
            "--prefix-tokens", str(sc["prefix"]),
            "--memory-budget-gb", str(sc["budget_gb"]),
            "--json",
        ]
        data, sample = run_json([str(BINARY_PATH)] + args)
        results.append({"inputs": sc, "unvalidated_cost_model": data, "cli_sample": sample_evidence(sample)})

    return {"scenarios": results}


def collect(build_dir):
    # Require explicit, intact projections before running any workload.
    cluster_results = benchmark_cluster_projections(build_dir)
    print("=== Hologram Live CLI diagnostics (not inference performance acceptance) ===")
    print(f"Binary: {BINARY_PATH} ({BINARY_PATH.stat().st_size / 1_000_000:.1f} MB)")
    print(f"Iterations per test: {NUM_ITERATIONS}\n")

    benchmarks = [
        ("doctor", ["doctor"], ["--prism", "doctor"]),
        ("help", ["--help"], ["--prism", "--help"]),
        ("status", ["status"], ["--prism", "status"]),
    ]

    dispatch_results = {}
    for name, standard_args, prism_args in benchmarks:
        print(f"Running benchmark for '{name}'...")
        std_res = measure_command(standard_args)
        prism_res = measure_command(prism_args)
        speedup = round(std_res["mean_ms"] / prism_res["mean_ms"], 2) if prism_res["mean_ms"] > 0 else 1.0

        dispatch_results[name] = {
            "standard": std_res,
            "prismpm": prism_res,
            "speedup_factor": speedup,
        }
        print(f"  Standard : {std_res['mean_ms']} ms (RSS: {std_res['peak_rss_mb']} MB)")
        print(f"  PrismPM  : {prism_res['mean_ms']} ms (RSS: {prism_res['peak_rss_mb']} MB)")
        print(f"  CLI elapsed-time ratio only: {speedup}x\n")

    print("Evaluating UOR Formal Inference Cost-Model...")
    uor_results = benchmark_uor_cost_model()

    print("Evaluating Working Set Containment (7B, 13B, 70B across 4k, 32k, 128k)...")
    containment_results = benchmark_working_set_containment()

    cli_benchmarks = {
        name: {
            "standard_mean_ms": data["standard"]["mean_ms"],
            "prism_mean_ms": data["prismpm"]["mean_ms"],
            "speedup": data["speedup_factor"],
            "speedup_factor": data["speedup_factor"],
            "standard_rss_mb": data["standard"]["peak_rss_mb"],
            "prism_rss_mb": data["prismpm"]["peak_rss_mb"],
            "throughput_ops_sec": data["prismpm"]["throughput_ops_sec"],
        }
        for name, data in dispatch_results.items()
    }

    full_report = {
        "acceptance": "not-established",
        "measurement_scope": "CLI process timings only; cost-model formulas are not measured inference performance",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "binary": str(BINARY_PATH),
        "binary_size_bytes": BINARY_PATH.stat().st_size,
        "dispatch_benchmarks": dispatch_results,
        "cli_benchmarks": cli_benchmarks,
        "uor_cost_model": uor_results,
        "working_set_containment": containment_results,
        "cluster_projections": cluster_results,
    }

    return full_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True,
                        help="exact planned build directory; never selected by modification time")
    parser.add_argument("--output", type=Path, default=Path("target/performance-comparison.json"))
    args = parser.parse_args()
    try:
        report_run(args.output, lambda: collect(args.build_dir))
    except Exception as error:
        print(f"diagnostics failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
