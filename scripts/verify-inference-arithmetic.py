#!/usr/bin/env python3
"""Kernel-check actual LexLean matrix costs and typed prefix bounds.

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
    error = {"kind": "constructor", "arguments": [], "type_arguments": [],
             "constructor": {"module": "Hologram.Inference", "name": "MatrixCostError.overflow"}}
    return {
        "kind": "constructor",
        "constructor": {"name": "Result.ok" if product <= UINT64_MAX else "Result.error"},
        "arguments": [integer(product) if product <= UINT64_MAX else error],
        "type_arguments": [{"kind": "uint64"}, {"kind": "named", "arguments": [],
                           "member": {"module": "Hologram.Inference", "name": "MatrixCostError"}}],
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


def prefix_theorem(index, total, prefix):
    valid = prefix <= total
    error_type = {"kind": "named", "arguments": [],
                  "member": {"module": "Hologram.Inference", "name": "KVPrefixError"}}
    value = integer(total - prefix) if valid else {
        "kind": "constructor",
        "constructor": {"module": "Hologram.Inference", "name": "KVPrefixError.exceedsTotal"},
        "arguments": [integer(total), integer(prefix)], "type_arguments": []}
    return {
        "kind": "theorem", "name": f"prefix_boundary_{index}",
        "parameters": [], "axioms": ["Quot.sound", "propext"],
        "proof": {"kind": "reflexivity"},
        "statement": {"kind": "eq", "left": {
            "kind": "call",
            "function": {"module": "Hologram.Inference", "name": "kvEffectiveTokens"},
            "arguments": [integer(total), integer(prefix)]},
            "right": {"kind": "constructor",
                      "constructor": {"name": "Result.ok" if valid else "Result.error"},
                      "arguments": [value], "type_arguments": [{"kind": "uint64"}, error_type]}}}


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


def is_false_equation_rejection(result, *, prefix=False):
    """Bind negative evidence to a deliberately false arithmetic equation."""
    expression = ("Hologram.Inference.kvEffectiveTokens 0 1" if prefix else
                  "Hologram.Inference.matmulFlops { m := 0, k := 0, n := 0 }")
    wrong_value = "Except.ok 0" if prefix else "Except.ok 1"
    expected = (
        "Lean rejected `PrismHologram.Checks` (error): Tactic `rfl` failed: "
        "The left-hand side\n"
        f"  {expression}\n"
        "is not definitionally equal to the right-hand side\n"
        f"  {wrong_value}\n\n"
        f"⊢ {expression} = {wrong_value}"
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


def require_general_arithmetic_contract(core):
    """Check statements independently of proof bodies and DAG node numbering."""
    namespace = "PrismHologram.Hologram.Inference."
    prefix = "PrismHologram.Hologram.InferenceProofs.matmulFlops_zero_"
    prefix_name = "PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_full_prefix"
    excess_name = "PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_excess_prefix"
    bounded_name = "PrismHologram.Hologram.InferenceProofs.checkedFromNat_bounded"
    valid_name = "PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_valid_prefix"
    overflow_name = "PrismHologram.Hologram.InferenceProofs.checkedFromNat_overflow"
    multiply_bounded = "PrismHologram.Hologram.InferenceProofs.checkedMultiply_bounded"
    multiply_overflow = "PrismHologram.Hologram.InferenceProofs.checkedMultiply_overflow"
    expected_names = {prefix + "inner", prefix + "columns", prefix_name, excess_name, bounded_name, valid_name,
                      overflow_name, multiply_bounded, multiply_overflow}
    declarations = core["declarations"]
    if len(declarations) != 9 or {row["name"] for row in declarations} != expected_names:
        raise RuntimeError("general arithmetic theorem inventory changed")
    zero_level = {"k": "z"}
    one_level = {"k": "s", "a": zero_level}
    def constant(name, levels=()):
        return {"k": "c", "n": name, "u": list(levels)}
    def apply(function, *arguments):
        for argument in arguments:
            function = {"k": "a", "f": function, "x": argument}
        return function
    uint = constant("UInt64")
    zero = apply(constant("UInt64.ofNat"), {"k": "n", "v": "0"})
    error = constant(namespace + "MatrixCostError")
    result = apply(constant("Except", (zero_level, zero_level)), error, uint)
    success = apply(constant("Except.ok", (zero_level, zero_level)), error, uint, zero)
    def expand(index, budget):
        budget[0] -= 1
        if budget[0] < 0:
            raise RuntimeError("proof statement exceeds inventory inspection bound")
        node = dict(core["nodes"][index])
        for child in {"a": ("f", "x"), "p": ("t", "v")}.get(node["k"], ()):
            node[child] = expand(node[child], budget)
        return node
    for declaration in declarations:
        columns = declaration["name"] == prefix + "columns"
        dimensions = [{"k": "b", "i": 1}, {"k": "b", "i": 0}, zero] if columns else [
            {"k": "b", "i": 1}, zero, {"k": "b", "i": 0}]
        matrix = apply(constant(namespace + "MatrixDimension.mk"), *dimensions)
        goal = apply(constant("Eq", (one_level,)), result,
                     apply(constant(namespace + "matmulFlops"), matrix), success)
        expected = {"k": "p", "n": "m", "b": "e", "t": uint, "v": {
            "k": "p", "n": "k" if columns else "n", "b": "e", "t": uint, "v": goal}}
        if declaration["name"] in (prefix_name, excess_name, valid_name):
            prefix_error = constant(namespace + "KVPrefixError")
            prefix_result = apply(constant("Except", (zero_level, zero_level)), prefix_error, uint)
            prefix_success = apply(constant("Except.ok", (zero_level, zero_level)), prefix_error, uint, zero)
            total = {"k": "b", "i": 0}
            prefix_goal = apply(constant("Eq", (one_level,)), prefix_result,
                                apply(constant(namespace + "kvEffectiveTokens"), total, total), prefix_success)
            expected = {"k": "p", "n": "total", "b": "e", "t": uint, "v": prefix_goal}
            if declaration["name"] in (excess_name, valid_name):
                total, cached = {"k": "b", "i": 2}, {"k": "b", "i": 1}
                error_payload = apply(constant(namespace + "KVPrefixError.exceedsTotal"), total, cached)
                failure = apply(constant("Except.error", (zero_level, zero_level)), prefix_error, uint, error_payload)
                excess_goal = apply(constant("Eq", (one_level,)), prefix_result,
                                    apply(constant(namespace + "kvEffectiveTokens"), total, cached), failure)
                relation = apply(constant("Nat.lt"),
                                 apply(constant("UInt64.toNat"), {"k": "b", "i": 1}),
                                 apply(constant("UInt64.toNat"), {"k": "b", "i": 0}))
                expected = {"k": "p", "n": "total", "b": "e", "t": uint, "v": {
                    "k": "p", "n": "prefix", "b": "e", "t": uint, "v": {
                        "k": "p", "n": "exceeds", "b": "e", "t": relation, "v": excess_goal}}}
                if declaration["name"] == valid_name:
                    natural_difference = apply(constant("Nat.sub"),
                                               apply(constant("UInt64.toNat"), total),
                                               apply(constant("UInt64.toNat"), cached))
                    exact_remaining = apply(constant("UInt64.ofNat"), natural_difference)
                    valid_success = apply(constant("Except.ok", (zero_level, zero_level)), prefix_error, uint, exact_remaining)
                    within = expected["v"]["v"]
                    within["n"] = "within"
                    within["t"] = apply(constant("Nat.le"),
                                        apply(constant("UInt64.toNat"), {"k": "b", "i": 0}),
                                        apply(constant("UInt64.toNat"), {"k": "b", "i": 1}))
                    within["v"] = apply(constant("Eq", (one_level,)), prefix_result,
                                        apply(constant(namespace + "kvEffectiveTokens"), total, cached), valid_success)
        if declaration["name"] in (bounded_name, overflow_name):
            natural = constant("Nat")
            value = {"k": "b", "i": 1}
            option = apply(constant("Option", (zero_level,)), uint)
            checked = apply(constant(namespace + "LexLeanRuntime.checkedFromInt"), uint,
                            constant(namespace + "LexLeanRuntime.instFixedUInt64"),
                            apply(constant("Int.ofNat"), value))
            bounded_success = apply(constant("Option.some", (zero_level,)), uint,
                                    apply(constant("UInt64.ofNat"), value))
            bound = apply(constant("Nat.lt"), {"k": "b", "i": 0}, {"k": "n", "v": "18446744073709551616"})
            expected = {"k": "p", "n": "value", "b": "e", "t": natural, "v": {
                "k": "p", "n": "bounded", "b": "e", "t": bound,
                "v": apply(constant("Eq", (one_level,)), option, checked, bounded_success)}}
            if declaration["name"] == overflow_name:
                expected["v"]["n"] = "overflow"
                expected["v"]["t"] = apply(constant("Nat.le"), {"k": "n", "v": "18446744073709551616"}, {"k": "b", "i": 0})
                expected["v"]["v"] = apply(constant("Eq", (one_level,)), option, checked,
                                           apply(constant("Option.none", (zero_level,)), uint))
        if declaration["name"] in (multiply_bounded, multiply_overflow):
            bounded = declaration["name"] == multiply_bounded
            option = apply(constant("Option", (zero_level,)), uint)
            product = apply(constant("Nat.mul"), apply(constant("UInt64.toNat"), {"k": "b", "i": 2}),
                            apply(constant("UInt64.toNat"), {"k": "b", "i": 1}))
            input_product = apply(constant("Nat.mul"), apply(constant("UInt64.toNat"), {"k": "b", "i": 1}),
                                  apply(constant("UInt64.toNat"), {"k": "b", "i": 0}))
            size = {"k": "n", "v": "18446744073709551616"}
            relation = apply(constant("Nat.lt"), input_product, size) if bounded else apply(constant("Nat.le"), size, input_product)
            output = (apply(constant("Option.some", (zero_level,)), uint, apply(constant("UInt64.ofNat"), product))
                      if bounded else apply(constant("Option.none", (zero_level,)), uint))
            checked = apply(constant(namespace + "LexLeanRuntime.checkedMultiply"), uint,
                            constant(namespace + "LexLeanRuntime.instFixedUInt64"), {"k": "b", "i": 2}, {"k": "b", "i": 1})
            expected = {"k": "p", "n": "left", "b": "e", "t": uint, "v": {
                "k": "p", "n": "right", "b": "e", "t": uint, "v": {
                    "k": "p", "n": "bounded" if bounded else "overflow", "b": "e", "t": relation,
                    "v": apply(constant("Eq", (one_level,)), option, checked, output)}}}
        if (declaration["kind"] != "theorem" or declaration["levels"] != []
                or declaration["policy"] != {"kind": "exact", "axioms": ["Quot.sound", "propext"]}
                or declaration.get("generated", False) is not False
                or expand(declaration["type"], [4096]) != expected):
            raise RuntimeError("general arithmetic theorem statement or policy changed")


def require_zero_rows_contract(model):
    expected = theorem(0, (0, 0, 0))
    expected["name"] = "matmulFlops_zero_rows"
    expected["parameters"] = [{"name": name, "type": {"kind": "uint64"}} for name in ("k", "n")]
    fields = expected["statement"]["left"]["arguments"][0]["fields"]
    for field in fields[1:]:
        field["value"] = {"kind": "var", "name": field["field"]}
    def localize(value):
        if isinstance(value, list):
            return [localize(item) for item in value]
        if isinstance(value, dict):
            return {key: localize(item) for key, item in value.items() if key != "module"}
        return value
    rows = [row for row in model["declarations"] if row.get("name") == "matmulFlops_zero_rows"]
    if rows != [localize(expected)]:
        raise RuntimeError("general zero-row theorem contract changed")


def is_false_general_rejection(result):
    expected = """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.matmulFlops_zero_inner':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.matmulFlops_zero_inner' has type
    ∀ (_ __1 : UInt64),
      (fun __2 => PrismHologram.Hologram.Inference.guardedMatmulFlops __2 { m := _, k := UInt64.ofNat 0, n := __1 })
          (PrismHologram.Hologram.Inference.dimensionIsZero _ || true) =
        (fun __2 => PrismHologram.Hologram.Inference.guardedMatmulFlops __2 { m := _, k := UInt64.ofNat 0, n := __1 })
          true
  but it is expected to have type
    ∀ (_ __1 : UInt64),
      PrismHologram.Hologram.Inference.matmulFlops { m := _, k := UInt64.ofNat 0, n := __1 } =
        Except.ok (UInt64.ofNat 1)"""
    return is_kernel_rejection(result, expected)


def is_false_full_prefix_rejection(result):
    expected = """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_full_prefix':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_full_prefix' has type
    ∀ (_ : UInt64),
      (fun __1 =>
            Option.rec (Except.error (PrismHologram.Hologram.Inference.KVPrefixError.exceedsTotal _ _))
              (fun _ => Except.ok _) (PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt __1))
          ((Int.ofNat _.toNat).sub (Int.ofNat _.toNat)) =
        (fun __1 =>
            Option.rec (Except.error (PrismHologram.Hologram.Inference.KVPrefixError.exceedsTotal _ _))
              (fun _ => Except.ok _) (PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt __1))
          (Int.ofNat 0)
  but it is expected to have type
    ∀ (_ : UInt64), PrismHologram.Hologram.Inference.kvEffectiveTokens _ _ = Except.ok (UInt64.ofNat 1)"""
    return is_kernel_rejection(result, expected)


def is_false_excess_prefix_rejection(result):
    expected = """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_excess_prefix':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_excess_prefix' has type
    ∀ (_ __1 : UInt64),
      _.toNat.lt __1.toNat →
        (fun __3 =>
              Option.rec (Except.error (PrismHologram.Hologram.Inference.KVPrefixError.exceedsTotal _ __1))
                (fun _ => Except.ok _) __3)
            (PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt
              ((Int.ofNat _.toNat).sub (Int.ofNat __1.toNat))) =
          (fun __3 =>
              Option.rec (Except.error (PrismHologram.Hologram.Inference.KVPrefixError.exceedsTotal _ __1))
                (fun _ => Except.ok _) __3)
            none
  but it is expected to have type
    ∀ (_ __1 : UInt64),
      _.toNat.lt __1.toNat → PrismHologram.Hologram.Inference.kvEffectiveTokens _ __1 = Except.ok (UInt64.ofNat 0)"""
    return is_kernel_rejection(result, expected)


def is_false_arithmetic_rejection(result, name):
    expected = {
        "bounded": """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.checkedFromNat_bounded':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.checkedFromNat_bounded' has type
    ∀ (_ : Nat),
      _.lt 18446744073709551616 →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt (Int.ofNat _) = some (UInt64.ofNat _)
  but it is expected to have type
    ∀ (_ : Nat),
      _.lt 18446744073709551616 →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt (Int.ofNat _) = some (UInt64.ofNat 1)""",
        "valid": """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_valid_prefix':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.kvEffectiveTokens_valid_prefix' has type
    ∀ (_ __1 : UInt64),
      __1.toNat.le _.toNat →
        (fun __3 =>
              Option.rec (Except.error (PrismHologram.Hologram.Inference.KVPrefixError.exceedsTotal _ __1))
                (fun _ => Except.ok _) __3)
            (PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt
              ((Int.ofNat _.toNat).sub (Int.ofNat __1.toNat))) =
          (fun __3 =>
              Option.rec (Except.error (PrismHologram.Hologram.Inference.KVPrefixError.exceedsTotal _ __1))
                (fun _ => Except.ok _) __3)
            (some (UInt64.ofNat (_.toNat.sub __1.toNat)))
  but it is expected to have type
    ∀ (_ __1 : UInt64),
      __1.toNat.le _.toNat → PrismHologram.Hologram.Inference.kvEffectiveTokens _ __1 = Except.ok (UInt64.ofNat 1)""",
        "conversion-overflow": """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.checkedFromNat_overflow':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.checkedFromNat_overflow' has type
    ∀ (_ : Nat),
      Nat.le 18446744073709551616 _ →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt (Int.ofNat _) = none
  but it is expected to have type
    ∀ (_ : Nat),
      Nat.le 18446744073709551616 _ →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt (Int.ofNat _) = some (UInt64.ofNat 1)""",
        "multiply-bounded": """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.checkedMultiply_bounded':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.checkedMultiply_bounded' has type
    ∀ (_ __1 : UInt64),
      (_.toNat.mul __1.toNat).lt 18446744073709551616 →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt (Int.ofNat (_.toNat.mul __1.toNat)) =
          some (UInt64.ofNat (_.toNat.mul __1.toNat))
  but it is expected to have type
    ∀ (_ __1 : UInt64),
      (_.toNat.mul __1.toNat).lt 18446744073709551616 →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedMultiply _ __1 = some (UInt64.ofNat 1)""",
        "multiply-overflow": """Lean rejected `PrismHologram.Hologram.InferenceProofs` (error): native core declaration 'PrismHologram.Hologram.InferenceProofs.checkedMultiply_overflow':
  (kernel) declaration type mismatch, 'PrismHologram.Hologram.InferenceProofs.checkedMultiply_overflow' has type
    ∀ (_ __1 : UInt64),
      Nat.le 18446744073709551616 (_.toNat.mul __1.toNat) →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedFromInt (Int.ofNat (_.toNat.mul __1.toNat)) = none
  but it is expected to have type
    ∀ (_ __1 : UInt64),
      Nat.le 18446744073709551616 (_.toNat.mul __1.toNat) →
        PrismHologram.Hologram.Inference.LexLeanRuntime.checkedMultiply _ __1 = some (UInt64.ofNat 1)""",
    }
    return is_kernel_rejection(result, expected[name])


def is_kernel_rejection(result, expected):
    diagnostics = result.get("diagnostics", [])
    return (result.get("spec") == "lexlean/command-result/1" and result.get("command") == "verify"
            and result.get("success") is False and result.get("exit_code") == 1
            and len(diagnostics) == 1 and diagnostics[0].get("code") == "LLV7002"
            and diagnostics[0].get("severity") == "error"
            and diagnostics[0].get("primary", {}).get("path") == "src/Hologram/InferenceProofs.lex.tex"
            and diagnostics[0].get("message", "").split() == expected.split())


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
    semantic_line = next(line for line in source.decode("utf-8").splitlines()
                         if line.startswith("\\semanticdata{"))
    model = json.loads(semantic_line[len("\\semanticdata{"):-1])
    require_zero_rows_contract(model)
    for change in ("omitted", "weakened"):
        changed = json.loads(json.dumps(model))
        row = next(row for row in changed["declarations"] if row.get("name") == "matmulFlops_zero_rows")
        if change == "omitted":
            changed["declarations"].remove(row)
        else:
            row["statement"]["left"]["arguments"][0]["fields"][1]["value"] = integer(0)
        try:
            require_zero_rows_contract(changed)
        except RuntimeError:
            pass
        else:
            raise RuntimeError(f"{change} zero-row theorem contract accepted")
    proof_path = ROOT / "src/Hologram/InferenceProofs.lex.tex"
    if proof_path.is_symlink() or not proof_path.is_file():
        raise RuntimeError("arithmetic proofs must be a regular source file")
    with proof_path.open("rb") as handle:
        proofs = handle.read(4194305)
    if len(proofs) > 4194304:
        raise RuntimeError("arithmetic proofs exceed the compiler source limit")
    proof_text = proofs.decode("utf-8")
    core_line = next(line for line in proof_text.splitlines() if line.startswith("\\coredata{"))
    core = json.loads(core_line[len("\\coredata{"):-1])
    require_general_arithmetic_contract(core)
    inventory_mutations = 2  # The two semantic zero-row checks above.
    for declaration_index in range(len(core["declarations"])):
        for change in ("omitted", "weakened", "policy"):
            changed = json.loads(json.dumps(core))
            if change == "omitted":
                changed["declarations"].pop(declaration_index)
            elif change == "weakened":
                # Remove one actual quantified argument, not a synthetic claim.
                declaration = changed["declarations"][declaration_index]
                declaration["type"] = changed["nodes"][declaration["type"]]["v"]
            else:
                changed["declarations"][declaration_index]["policy"] = {
                    "kind": "allow", "axioms": ["Quot.sound", "propext"]}
            try:
                require_general_arithmetic_contract(changed)
            except RuntimeError:
                inventory_mutations += 1
            else:
                raise RuntimeError(f"{change} theorem contract was accepted")
    values = (0, 1, 2, (1 << 32) - 1, 1 << 32, 1 << 63, UINT64_MAX)
    cases = list(itertools.product(values, repeat=3))
    cases.extend(((1, 1, UINT64_MAX // 2), (1, 1, UINT64_MAX // 2 + 1), (1 << 63, 0, 10)))
    prefix_cases = list(itertools.product(values, repeat=2))
    payload = json.dumps({"spec": "lexlean/semantic-module/1",
                          "declarations": [theorem(index, dims) for index, dims in enumerate(cases)]
                          + [prefix_theorem(index, total, prefix)
                             for index, (total, prefix) in enumerate(prefix_cases)]},
                         sort_keys=True, separators=(",", ":"))
    with tempfile.TemporaryDirectory(prefix="hologram-inference-arithmetic-") as owned:
        workspace = Path(owned)
        (workspace / "src/Hologram").mkdir(parents=True)
        (workspace / "src/Hologram/Inference.lex.tex").write_bytes(source)
        (workspace / "src/Hologram/InferenceProofs.lex.tex").write_bytes(proofs)
        header = ("\\begin{lexlean}{Checks}\n\\useglossary{lexlean.std.bool@1.1.0}\n"
            "\\useglossary{lexlean.std.nat@1.1.0}\n\\importmodule{Hologram.Inference}\n"
            "\\importmodule{Hologram.InferenceProofs}\n"
            "\\title{Boolean}\n\\begin{semanticmodule}\n\\semanticdata{")
        footer = "}\n\\end{semanticmodule}\n\\end{lexlean}\n"
        checks = workspace / "src/Checks.lex.tex"
        checks.write_text(header + payload + footer, encoding="utf-8")
        (workspace / "lexlean.toml").write_text('''spec = "lexlean/project/1"
name = "hologram-inference-arithmetic"
language = "1.1"
module_prefix = "PrismHologram"
source_roots = ["src"]
entrypoints = ["src/Checks.lex.tex", "src/Hologram/InferenceProofs.lex.tex"]
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
        assert verification["modules"] == ["Checks", "Hologram.Inference", "Hologram.InferenceProofs"]
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
                           ("message", rejection["diagnostics"][0]["message"].replace("Except.ok 1", "Except.ok 2"))):
            unrelated = json.loads(json.dumps(rejection))
            unrelated["diagnostics"][0][key] = value
            if is_false_equation_rejection(unrelated):
                raise RuntimeError("unrelated compiler evidence accepted")
        false_prefix = json.loads(payload)
        invalid_index = prefix_cases.index((0, 1))
        wrong_result = false_prefix["declarations"][len(cases) + invalid_index]["statement"]["right"]
        wrong_result["constructor"]["name"] = "Result.ok"
        wrong_result["arguments"] = [integer(0)]
        checks.write_text(header + json.dumps(false_prefix, sort_keys=True,
                                             separators=(",", ":")) + footer, encoding="utf-8")
        prefix_rejected = run("lexlean", arguments, workspace, check=False)
        prefix_rejection = json.loads(prefix_rejected.stdout)
        if prefix_rejected.returncode != 1 or not is_false_equation_rejection(prefix_rejection, prefix=True):
            print(prefix_rejected.stdout, file=sys.stderr)
            raise RuntimeError("compiler did not reject the false prefix-clamping equation")
        # Change only a general theorem's conclusion (zero -> one), keeping
        # its actual model, input dimensions and kernel proof unchanged.
        checks.write_text(header + payload + footer, encoding="utf-8")
        core = json.loads(core_line[len("\\coredata{"):-1])
        nodes = core["nodes"]
        def append(node):
            nodes.append(node)
            return len(nodes) - 1
        def wrong_conclusion(index):
            node = nodes[index]
            if node["k"] == "p":
                return append({**node, "v": wrong_conclusion(node["v"])})
            assert node["k"] == "a"
            result = nodes[node["x"]]
            zero = nodes[result["x"]]
            assert nodes[zero["x"]] == {"k": "n", "v": "0"}
            one = append({"k": "a", "f": zero["f"], "x": append({"k": "n", "v": "1"})})
            wrong_result = append({"k": "a", "f": result["f"], "x": one})
            return append({**node, "x": wrong_result})
        declaration = core["declarations"][0]
        declaration["type"] = wrong_conclusion(declaration["type"])
        mutated = "\\coredata{" + json.dumps(core, sort_keys=True, separators=(",", ":")) + "}"
        (workspace / "src/Hologram/InferenceProofs.lex.tex").write_text(
            proof_text.replace(core_line, mutated), encoding="utf-8")
        proof_rejected = run("lexlean", arguments, workspace, check=False)
        proof_rejection = json.loads(proof_rejected.stdout)
        if proof_rejected.returncode != 1 or not is_false_general_rejection(proof_rejection):
            print(proof_rejected.stdout, file=sys.stderr)
            raise RuntimeError("kernel did not reject the intended false general theorem")
        for message in ("(kernel) unrelated type error", proof_rejection["diagnostics"][0]["message"].replace(
                "Except.ok (UInt64.ofNat 1)", "Except.ok (UInt64.ofNat 2)")):
            unrelated = json.loads(json.dumps(proof_rejection))
            unrelated["diagnostics"][0]["message"] = message
            if is_false_general_rejection(unrelated):
                raise RuntimeError("unrelated kernel rejection accepted")
        core = json.loads(core_line[len("\\coredata{"):-1])
        nodes = core["nodes"]
        declaration = core["declarations"][2]
        binder = dict(nodes[declaration["type"]])
        goal = dict(nodes[binder["v"]])
        success = dict(nodes[goal["x"]])
        zero_value = nodes[success["x"]]
        one_value = append({"k": "a", "f": zero_value["f"], "x": append({"k": "n", "v": "1"})})
        success["x"] = one_value
        goal["x"] = append(success)
        binder["v"] = append(goal)
        declaration["type"] = append(binder)
        mutated = "\\coredata{" + json.dumps(core, sort_keys=True, separators=(",", ":")) + "}"
        (workspace / "src/Hologram/InferenceProofs.lex.tex").write_text(
            proof_text.replace(core_line, mutated), encoding="utf-8")
        prefix_proof_rejected = run("lexlean", arguments, workspace, check=False)
        prefix_proof_rejection = json.loads(prefix_proof_rejected.stdout)
        if prefix_proof_rejected.returncode != 1 or not is_false_full_prefix_rejection(prefix_proof_rejection):
            print(prefix_proof_rejected.stdout, file=sys.stderr)
            raise RuntimeError("kernel did not reject the false full-prefix theorem")
        for message in ("(kernel) unrelated type error", prefix_proof_rejection["diagnostics"][0]["message"].replace(
                "Except.ok (UInt64.ofNat 1)", "Except.ok (UInt64.ofNat 2)")):
            unrelated = json.loads(json.dumps(prefix_proof_rejection))
            unrelated["diagnostics"][0]["message"] = message
            if is_false_full_prefix_rejection(unrelated):
                raise RuntimeError("unrelated full-prefix kernel rejection accepted")
        core = json.loads(core_line[len("\\coredata{"):-1])
        nodes = core["nodes"]
        declaration = core["declarations"][3]
        binders = []
        index = declaration["type"]
        for _ in range(3):
            binder = dict(nodes[index])
            binders.append(binder)
            index = binder["v"]
        goal = dict(nodes[index])
        def constant_index(name):
            return next(index for index, node in enumerate(nodes) if node.get("k") == "c" and node["n"] == name)
        def app(function, argument):
            return append({"k": "a", "f": function, "x": argument})
        zero = app(constant_index("UInt64.ofNat"), append({"k": "n", "v": "0"}))
        success = app(app(app(constant_index("Except.ok"),
                             constant_index("PrismHologram.Hologram.Inference.KVPrefixError")),
                         constant_index("UInt64")), zero)
        goal["x"] = success
        index = append(goal)
        for binder in reversed(binders):
            binder["v"] = index
            index = append(binder)
        declaration["type"] = index
        mutated = "\\coredata{" + json.dumps(core, sort_keys=True, separators=(",", ":")) + "}"
        (workspace / "src/Hologram/InferenceProofs.lex.tex").write_text(
            proof_text.replace(core_line, mutated), encoding="utf-8")
        excess_rejected = run("lexlean", arguments, workspace, check=False)
        excess_rejection = json.loads(excess_rejected.stdout)
        if excess_rejected.returncode != 1 or not is_false_excess_prefix_rejection(excess_rejection):
            print(excess_rejected.stdout, file=sys.stderr)
            raise RuntimeError("kernel did not reject general invalid-prefix clamping")
        for message in ("(kernel) unrelated type error", excess_rejection["diagnostics"][0]["message"].replace(
                "Except.ok (UInt64.ofNat 0)", "Except.ok (UInt64.ofNat 1)")):
            unrelated = json.loads(json.dumps(excess_rejection))
            unrelated["diagnostics"][0]["message"] = message
            if is_false_excess_prefix_rejection(unrelated):
                raise RuntimeError("unrelated excess-prefix kernel rejection accepted")
        arithmetic_rejections = {}
        for name, declaration_index in (("bounded", 4), ("valid", 5), ("conversion-overflow", 6),
                                        ("multiply-bounded", 7), ("multiply-overflow", 8)):
            core = json.loads(core_line[len("\\coredata{"):-1])
            nodes = core["nodes"]
            declaration = core["declarations"][declaration_index]
            binders = []
            index = declaration["type"]
            while nodes[index]["k"] == "p":
                binder = dict(nodes[index])
                binders.append(binder)
                index = binder["v"]
            goal = dict(nodes[index])
            success = dict(nodes[goal["x"]])
            success["x"] = app(constant_index("UInt64.ofNat"), append({"k": "n", "v": "1"}))
            if name in ("conversion-overflow", "multiply-overflow"):
                success["f"] = app(constant_index("Option.some"), constant_index("UInt64"))
            goal["x"] = append(success)
            index = append(goal)
            for binder in reversed(binders):
                binder["v"] = index
                index = append(binder)
            declaration["type"] = index
            mutated = "\\coredata{" + json.dumps(core, sort_keys=True, separators=(",", ":")) + "}"
            (workspace / "src/Hologram/InferenceProofs.lex.tex").write_text(
                proof_text.replace(core_line, mutated), encoding="utf-8")
            rejected = run("lexlean", arguments, workspace, check=False)
            arithmetic_rejections[name] = json.loads(rejected.stdout)
            if rejected.returncode != 1 or not is_false_arithmetic_rejection(arithmetic_rejections[name], name):
                print(rejected.stdout, file=sys.stderr)
                raise RuntimeError(f"kernel did not reject false {name} arithmetic")
            for message in ("(kernel) unrelated type error", arithmetic_rejections[name]["diagnostics"][0]["message"].replace(
                    "UInt64.ofNat 1)", "UInt64.ofNat 2)")):
                unrelated = json.loads(json.dumps(arithmetic_rejections[name]))
                unrelated["diagnostics"][0]["message"] = message
                if is_false_arithmetic_rejection(unrelated, name):
                    raise RuntimeError(f"unrelated {name} kernel rejection accepted")
        report = {"schema": "hologram/inference-arithmetic-regression/1",
                          "source_sha256": hashlib.sha256(source).hexdigest(),
                          "proof_source_sha256": hashlib.sha256(proofs).hexdigest(),
                          "expected_cases": len(cases), "verified_cases": len(cases),
                          "expected_prefix_cases": len(prefix_cases),
                          "verified_prefix_cases": len(prefix_cases),
                          "false_equation_rejected": True,
                          "false_prefix_clamping_rejected": True,
                          "false_general_zero_theorem_rejected": True,
                          "false_general_full_prefix_rejected": True,
                          "false_general_excess_clamping_rejected": True,
                          "false_bounded_conversion_rejected": True,
                          "false_valid_prefix_rejected": True,
                          "false_checked_multiplication_rejected": True,
                          "kernel_rejection_cases": 10,
                          "proof_inventory_mutations_rejected": inventory_mutations,
                          "verification": verification,
                          "scope": "LexLean matrix and prefix boundary equations; general zero-factor, prefix and checked-multiplication proofs",
                          "product_acceptance": "not-established"}
        if options.evidence_directory is not None:
            (options.evidence_directory / "false-equation-result.json").write_text(
                json.dumps(rejection, sort_keys=True) + "\n", encoding="utf-8")
            (options.evidence_directory / "false-prefix-result.json").write_text(
                json.dumps(prefix_rejection, sort_keys=True) + "\n", encoding="utf-8")
            (options.evidence_directory / "false-general-proof-result.json").write_text(
                json.dumps(proof_rejection, sort_keys=True) + "\n", encoding="utf-8")
            (options.evidence_directory / "false-general-prefix-proof-result.json").write_text(
                json.dumps(prefix_proof_rejection, sort_keys=True) + "\n", encoding="utf-8")
            (options.evidence_directory / "false-general-excess-proof-result.json").write_text(
                json.dumps(excess_rejection, sort_keys=True) + "\n", encoding="utf-8")
            for name, result in arithmetic_rejections.items():
                (options.evidence_directory / f"false-{name}-arithmetic-result.json").write_text(
                    json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            (options.evidence_directory / "regression-result.json").write_text(
                json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
