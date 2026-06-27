"""Evaluation runner: execute cases against a target and aggregate a report."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .case import CaseResult, CheckOutcome, EvalCase, EvalReport
from .checks import run_check

# A target turns a case into an output string (an LLM completion, the agent
# gateway's rendered answer, etc.).
Target = Callable[[EvalCase], str]


def run_case(case: EvalCase, target: Target, *, judge: Any = None) -> CaseResult:
    try:
        output = target(case)
    except Exception as exc:  # noqa: BLE001 - isolate a single case failure
        return CaseResult(
            case_id=case.id,
            output="",
            outcomes=(
                CheckOutcome(
                    type="target",
                    passed=False,
                    score=0.0,
                    detail=f"target raised: {exc}",
                ),
            ),
            tags=case.tags,
        )
    outcomes = tuple(run_check(spec, output, case=case, judge=judge) for spec in case.checks)
    return CaseResult(case_id=case.id, output=output, outcomes=outcomes, tags=case.tags)


def run_eval(
    cases: Iterable[EvalCase],
    target: Target,
    *,
    judge: Any = None,
) -> EvalReport:
    results = tuple(run_case(case, target, judge=judge) for case in cases)
    return EvalReport(results=results)


__all__ = ["Target", "run_case", "run_eval"]
