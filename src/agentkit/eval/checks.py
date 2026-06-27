"""Deterministic checks + check dispatch for the evaluation harness."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from agentkit.core.safety import find_pii, find_prompt_injection

from .case import CheckOutcome, CheckSpec, EvalCase


def _ok(spec: CheckSpec, passed: bool, detail: str = "") -> CheckOutcome:
    return CheckOutcome(
        type=spec.type,
        passed=passed,
        score=1.0 if passed else 0.0,
        detail=detail,
        weight=spec.weight,
    )


def _contains(spec: CheckSpec, output: str) -> CheckOutcome:
    needle = str(spec.value or "")
    passed = needle in output
    return _ok(spec, passed, "" if passed else f"missing substring: {needle!r}")


def _not_contains(spec: CheckSpec, output: str) -> CheckOutcome:
    needle = str(spec.value or "")
    passed = needle not in output
    return _ok(spec, passed, "" if passed else f"unexpected substring: {needle!r}")


def _icontains(spec: CheckSpec, output: str) -> CheckOutcome:
    needle = str(spec.value or "").lower()
    passed = needle in output.lower()
    return _ok(spec, passed, "" if passed else f"missing (ci) substring: {needle!r}")


def _regex(spec: CheckSpec, output: str) -> CheckOutcome:
    pattern = str(spec.value or "")
    passed = re.search(pattern, output) is not None
    return _ok(spec, passed, "" if passed else f"regex did not match: {pattern!r}")


def _equals(spec: CheckSpec, output: str) -> CheckOutcome:
    expected = str(spec.value if spec.value is not None else "").strip()
    passed = output.strip() == expected
    return _ok(
        spec, passed, "" if passed else f"expected {expected!r}, got {output.strip()[:120]!r}"
    )


def _min_length(spec: CheckSpec, output: str) -> CheckOutcome:
    n = int(spec.value or 0)
    passed = len(output) >= n
    return _ok(spec, passed, "" if passed else f"length {len(output)} < {n}")


def _max_length(spec: CheckSpec, output: str) -> CheckOutcome:
    n = int(spec.value or 0)
    passed = len(output) <= n
    return _ok(spec, passed, "" if passed else f"length {len(output)} > {n}")


def _no_pii(spec: CheckSpec, output: str) -> CheckOutcome:
    findings = find_pii(output)
    passed = not findings
    labels = ", ".join(sorted({f.label for f in findings}))
    return _ok(spec, passed, "" if passed else f"PII leaked: {labels}")


def _no_injection(spec: CheckSpec, output: str) -> CheckOutcome:
    findings = find_prompt_injection(output)
    passed = not findings
    labels = ", ".join(sorted({f.label for f in findings}))
    return _ok(spec, passed, "" if passed else f"injection markers: {labels}")


DETERMINISTIC: dict[str, Callable[[CheckSpec, str], CheckOutcome]] = {
    "contains": _contains,
    "not_contains": _not_contains,
    "icontains": _icontains,
    "regex": _regex,
    "equals": _equals,
    "min_length": _min_length,
    "max_length": _max_length,
    "no_pii": _no_pii,
    "no_injection": _no_injection,
}


def run_check(
    spec: CheckSpec,
    output: str,
    *,
    case: EvalCase | None = None,
    judge: Any = None,
) -> CheckOutcome:
    """Evaluate one check against ``output`` (deterministic or LLM-as-judge)."""
    if spec.type == "judge":
        if judge is None:
            return CheckOutcome(
                type="judge",
                passed=False,
                score=0.0,
                detail="judge not configured (run with --judge)",
                weight=spec.weight,
                skipped=True,
            )
        rubric = spec.rubric or str(spec.value or "")
        result = judge.score(output=output, rubric=rubric, user=getattr(case, "user", ""))
        passed = result.score >= spec.min_score
        detail = f"score={result.score:.1f}/5 (min {spec.min_score:.1f}): {result.reason}"
        return CheckOutcome(
            type="judge",
            passed=passed,
            score=max(0.0, min(1.0, result.score / 5.0)),
            detail=detail,
            weight=spec.weight,
        )
    fn = DETERMINISTIC.get(spec.type)
    if fn is None:
        return CheckOutcome(
            type=spec.type,
            passed=False,
            score=0.0,
            detail=f"unknown check type: {spec.type}",
            weight=spec.weight,
        )
    return fn(spec, output)


__all__ = ["DETERMINISTIC", "run_check"]
