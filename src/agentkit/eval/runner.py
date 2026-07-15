"""Evaluation runner: execute cases against a target and aggregate a report."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .case import CaseResult, CheckOutcome, EvalCase, EvalReport
from .checks import run_check

# A target turns a case into an output string (an LLM completion, the agent
# gateway's rendered answer, etc.).
Target = Callable[[EvalCase], str]


def run_case(
    case: EvalCase,
    target: Target,
    *,
    judge: Any = None,
    require_judge: bool = False,
    attempt: int = 1,
) -> CaseResult:
    if not case.checks:
        return CaseResult(
            case_id=case.id,
            output="",
            outcomes=(
                CheckOutcome(
                    type="configuration",
                    passed=False,
                    score=0.0,
                    detail="case has no checks",
                ),
            ),
            tags=case.tags,
            attempt=attempt,
        )
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
            attempt=attempt,
        )
    outcomes = tuple(
        run_check(
            spec,
            output,
            case=case,
            judge=judge,
            require_judge=require_judge,
        )
        for spec in case.checks
    )
    return CaseResult(
        case_id=case.id,
        output=output,
        outcomes=outcomes,
        tags=case.tags,
        attempt=attempt,
    )


def run_eval(
    cases: Iterable[EvalCase],
    target: Target,
    *,
    judge: Any = None,
    require_judge: bool = False,
    repetitions: int = 1,
    concurrency: int = 1,
) -> EvalReport:
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    work = [(case, attempt) for case in cases for attempt in range(1, repetitions + 1)]

    def execute(item: tuple[EvalCase, int]) -> CaseResult:
        case, attempt = item
        return run_case(
            case,
            target,
            judge=judge,
            require_judge=require_judge,
            attempt=attempt,
        )

    if concurrency == 1:
        results = tuple(execute(item) for item in work)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = tuple(executor.map(execute, work))
    return EvalReport(results=results)


__all__ = ["Target", "run_case", "run_eval"]
