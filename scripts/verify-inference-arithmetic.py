#!/usr/bin/env python3
"""Kernel-check the actual LexLean matrix model against exact boundary products.

This is a model regression, not generated-runtime or full H19 acceptance.
Only test inputs and workspace metadata are generated here; Lean is generated
exclusively by LexLean from the unchanged copied product source.
"""

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
UINT64_MAX = (1 << 64) - 1


def integer(value):
    return {"kind": "integer", "representation": "uint64", "value": str(value)}


def expected_product(m, k, n):
    # Python integers retain the complete mathematical product, including zero
    # after a factor whose intermediate fixed-width product would overflow.
    product = 2 * m * k * n
    return {
        "kind": "constructor",
        "constructor": {"name": "Option.some" if product <= UINT64_MAX else "Option.none"},
        "arguments": [integer(product)] if product <= UINT64_MAX else [],
        "type_arguments": [{"kind": "uint64"}],
    }


def theorem(index, dimensions):
    return {
        "kind": "theorem", "name": f"matrix_boundary_{index}",
        "parameters": [], "axioms": ["Quot.sound", "propext"],
        "proof": {"kind": "reflexivity"},
        "statement": {
            "kind": "eq",
            "left": {
                "kind": "call",
                "function": {"module": "Hologram.Inference", "name": "matmulFlops"},
                "arguments": [{
                    "kind": "record",
                    "type": {"module": "Hologram.Inference", "name": "MatrixDimension"},
                    "fields": [{"field": field, "value": integer(value)}
                               for field, value in zip(("m", "k", "n"), dimensions)],
                }],
            },
            "right": expected_product(*dimensions),
        },
    }


def run(program, args, workspace, *, check=True):
    executable = shutil.which(program)
    if executable is None:
        raise RuntimeError(f"required SDK executable missing: {program}")
    result = subprocess.run([executable, *args], cwd=workspace, check=False,
                            timeout=300, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        result.check_returncode()
    return result


def is_false_equation_rejection(result):
    """Bind negative evidence to the deliberately false zero-product equation."""
    expected = (
        "Lean rejected `PrismHologram.Checks` (error): Tactic `rfl` failed: "
        "The left-hand side\n"
        "  Hologram.Inference.matmulFlops { m := 0, k := 0, n := 0 }\n"
        "is not definitionally equal to the right-hand side\n"
        "  some 1\n\n"
        "⊢ Hologram.Inference.matmulFlops { m := 0, k := 0, n := 0 } = some 1"
    )
    diagnostics = result.get("diagnostics", [])
    return (
        result.get("spec") == "lexlean/command-result/1"
        and result.get("command") == "verify"
        and result.get("success") is False
        and result.get("exit_code") == 1
        and len(diagnostics) == 1
        and diagnostics[0].get("code") == "LLV7002"
        and diagnostics[0].get("severity") == "error"
        and diagnostics[0].get("primary", {}).get("path") == "src/Checks.lex.tex"
        # Lean's pretty-printer may insert a blank line before the goal.
        # Compare all tokens, not a substring or merely the diagnostic code.
        and diagnostics[0].get("message", "").split() == expected.split()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-directory", type=Path,
                        help="new directory for actual positive verification artifacts and negative diagnostics")
    options = parser.parse_args()
    if options.evidence_directory is not None and options.evidence_directory.exists():
        raise RuntimeError("evidence directory must not already exist")
    source_path = ROOT / "src/Hologram/Inference.lex.tex"
    if source_path.is_symlink() or not source_path.is_file():
        raise RuntimeError("matrix model must be a regular source file")
    with source_path.open("rb") as handle:
        source = handle.read(4194305)
    if len(source) > 4194304:
        raise RuntimeError("matrix model exceeds the compiler source limit")
    values = (0, 1, 2, (1 << 32) - 1, 1 << 32, 1 << 63, UINT64_MAX)
    cases = list(itertools.product(values, repeat=3))
    cases.extend(((1, 1, UINT64_MAX // 2), (1, 1, UINT64_MAX // 2 + 1), (1 << 63, 0, 10)))
    payload = json.dumps({"spec": "lexlean/semantic-module/1",
                          "declarations": [theorem(index, dims) for index, dims in enumerate(cases)]},
                         sort_keys=True, separators=(",", ":"))
    with tempfile.TemporaryDirectory(prefix="hologram-inference-arithmetic-") as owned:
        workspace = Path(owned)
        (workspace / "src/Hologram").mkdir(parents=True)
        (workspace / "src/Hologram/Inference.lex.tex").write_bytes(source)
        header = ("\\begin{lexlean}{Checks}\n\\useglossary{lexlean.std.bool@1.1.0}\n"
            "\\useglossary{lexlean.std.nat@1.1.0}\n\\importmodule{Hologram.Inference}\n"
            "\\title{Boolean}\n\\begin{semanticmodule}\n\\semanticdata{")
        footer = "}\n\\end{semanticmodule}\n\\end{lexlean}\n"
        checks = workspace / "src/Checks.lex.tex"
        checks.write_text(header + payload + footer, encoding="utf-8")
        (workspace / "lexlean.toml").write_text('''spec = "lexlean/project/1"
name = "hologram-inference-arithmetic"
language = "1.1"
module_prefix = "PrismHologram"
source_roots = ["src"]
entrypoints = ["src/Checks.lex.tex"]
build_root = ".lexlean"
lockfile = "lexlean.lock"
lean_workspace = "."
lean_toolchain = "leanprover/lean4:v4.32.1"

[[lexicon_source]]
package = "lexlean.std.bool"
kind = "builtin"

[[lexicon_source]]
package = "lexlean.std.nat"
kind = "builtin"

[limits]
max_file_bytes = 4194304
max_total_source_bytes = 67108864
max_primitive_atoms = 2000000
max_token_lattice_edges = 4000000
max_parse_states = 4000000
max_ir_nodes = 2000000
max_scope_depth = 1024
max_import_depth = 128
max_diagnostics = 256
max_child_output_bytes = 16777216
child_timeout_ms = 300000
''', encoding="utf-8")
        (workspace / "lakefile.toml").write_text('name = "hologram_inference_arithmetic"\nversion = "0.1.0"\n', encoding="utf-8")
        (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.32.1\n", encoding="utf-8")
        run("lake", ["update"], workspace)
        run("lexlean", ["lock"], workspace)
        arguments = ["--diagnostic-format", "json", "verify"]
        verification = json.loads(run("lexlean", arguments, workspace).stdout)
        assert verification["spec"] == "lexlean/command-result/1"
        assert verification["success"] is True and verification["exit_code"] == 0
        assert verification["modules"] == ["Checks", "Hologram.Inference"]
        if options.evidence_directory is not None:
            shutil.copytree(workspace, options.evidence_directory)
        # The same compiler/kernel path must reject a deliberately false
        # equation, not merely accept the generated test file's syntax.
        false_equation = json.loads(payload)
        false_equation["declarations"][0]["statement"]["right"]["arguments"] = [integer(1)]
        checks.write_text(header + json.dumps(false_equation, sort_keys=True,
                                             separators=(",", ":")) + footer, encoding="utf-8")
        rejected = run("lexlean", arguments, workspace, check=False)
        rejection = json.loads(rejected.stdout)
        if rejected.returncode != 1 or not is_false_equation_rejection(rejection):
            print(rejected.stdout, file=sys.stderr)
            raise RuntimeError("compiler did not reject the intended false matrix equation")
        # Mutate genuine compiler evidence: unrelated errors, sources, or
        # equations must never be promoted into this negative acceptance claim.
        for key, value in (("message", "unrelated elaboration failure"),
                           ("primary", {"path": "src/Hologram/Inference.lex.tex"}),
                           ("code", "LLV7001"),
                           ("message", rejection["diagnostics"][0]["message"].replace("some 1", "some 2"))):
            unrelated = json.loads(json.dumps(rejection))
            unrelated["diagnostics"][0][key] = value
            if is_false_equation_rejection(unrelated):
                raise RuntimeError("unrelated compiler evidence accepted")
        report = {"schema": "hologram/inference-arithmetic-regression/1",
                          "source_sha256": hashlib.sha256(source).hexdigest(),
                          "expected_cases": len(cases), "verified_cases": len(cases),
                          "false_equation_rejected": True,
                          "verification": verification,
                          "scope": "LexLean matrix boundary equations",
                          "product_acceptance": "not-established"}
        if options.evidence_directory is not None:
            (options.evidence_directory / "false-equation-result.json").write_text(
                json.dumps(rejection, sort_keys=True) + "\n", encoding="utf-8")
            (options.evidence_directory / "regression-result.json").write_text(
                json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
