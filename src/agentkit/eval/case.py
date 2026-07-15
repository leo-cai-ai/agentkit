"""Data model for the LLM evaluation harness.

A *golden dataset* is a list of :class:`EvalCase`. Each case feeds an input to a
*target* (an LLM prompt or the full agent gateway) and asserts properties of the
output via :class:`CheckSpec` checks — deterministic matchers and/or an
LLM-as-judge rubric. Results aggregate into an :class:`EvalReport` that powers a
CI regression gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckSpec:
    """One assertion about a target's output.

    ``type`` selects a deterministic matcher (``contains``, ``regex``, ...) or
    ``judge`` for an LLM-as-judge rubric. ``weight`` scales the check's score
    contribution; ``min_score`` is the judge pass threshold (1-5 scale).
    """

    type: str
    value: Any = None
    rubric: str = ""
    min_score: float = 4.0
    weight: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckSpec:
        return cls(
            type=str(data["type"]),
            value=data.get("value"),
            rubric=str(data.get("rubric", "")),
            min_score=float(data.get("min_score", 4.0)),
            weight=float(data.get("weight", 1.0)),
        )


@dataclass(frozen=True)
class EvalCase:
    id: str
    system: str = ""
    user: str = ""
    agent: str = ""
    checks: tuple[CheckSpec, ...] = ()
    tags: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        checks = [CheckSpec.from_dict(check) for check in data.get("checks", [])]
        expected_strategy = data.get("expected_strategy")
        if expected_strategy:
            checks.append(
                CheckSpec(
                    "json_path_equals",
                    {"path": "response.strategy", "equals": expected_strategy},
                )
            )
        expected_status = data.get("expected_status")
        if expected_status:
            checks.append(
                CheckSpec(
                    "json_path_equals",
                    {"path": "status", "equals": expected_status},
                )
            )
        expected_events = data.get("expected_events")
        if expected_events is None and expected_status:
            expected_events = ["run_started"]
            if expected_strategy:
                expected_events.append("strategy_selected")
            expected_events.append(
                "run_paused" if expected_status == "waiting_for_approval" else "run_finished"
            )
        if expected_events:
            checks.append(CheckSpec("event_sequence", expected_events))
        return cls(
            id=str(data["id"]),
            system=str(data.get("system", "")),
            user=str(data.get("user", data.get("text", ""))),
            agent=str(data.get("agent", "")),
            checks=tuple(checks),
            tags=tuple(str(t) for t in data.get("tags", [])),
            context=dict(data.get("context", {})),
        )


@dataclass(frozen=True)
class CheckOutcome:
    type: str
    passed: bool
    score: float  # 0..1 contribution
    detail: str = ""
    weight: float = 1.0
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "passed": self.passed,
            "score": round(self.score, 3),
            "detail": self.detail,
            "weight": self.weight,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    output: str
    outcomes: tuple[CheckOutcome, ...]
    tags: tuple[str, ...] = ()
    attempt: int = 1

    @property
    def evaluated(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.skipped]

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.evaluated)

    @property
    def score(self) -> float:
        evaluated = self.evaluated
        if not evaluated:
            return 1.0
        total_weight = sum(o.weight for o in evaluated) or 1.0
        return sum(o.score * o.weight for o in evaluated) / total_weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "attempt": self.attempt,
            "passed": self.passed,
            "score": round(self.score, 3),
            "tags": list(self.tags),
            "outcomes": [o.to_dict() for o in self.outcomes],
            "output": self.output[:2000],
        }


@dataclass(frozen=True)
class EvalReport:
    results: tuple[CaseResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed_count / self.total if self.total else 1.0

    @property
    def mean_score(self) -> float:
        return sum(r.score for r in self.results) / self.total if self.total else 1.0

    def gate(self, *, min_pass_rate: float = 1.0, min_mean_score: float = 0.0) -> bool:
        """True when the run meets both regression thresholds."""
        return self.pass_rate >= min_pass_rate and self.mean_score >= min_mean_score

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.total - self.passed_count,
            "pass_rate": round(self.pass_rate, 3),
            "mean_score": round(self.mean_score, 3),
        }

    def format_text(self) -> str:
        lines = [
            f"Eval: {self.passed_count}/{self.total} passed "
            f"(pass_rate={self.pass_rate:.2%}, mean_score={self.mean_score:.2f})",
        ]
        for result in self.results:
            mark = "PASS" if result.passed else "FAIL"
            attempt = f" attempt={result.attempt}" if result.attempt > 1 else ""
            lines.append(f"  [{mark}] {result.case_id}{attempt} (score={result.score:.2f})")
            for outcome in result.outcomes:
                if outcome.passed and not outcome.skipped:
                    continue
                status = "skip" if outcome.skipped else "fail"
                lines.append(f"      - {status} {outcome.type}: {outcome.detail}")
        return "\n".join(lines)


__all__ = [
    "CheckSpec",
    "EvalCase",
    "CheckOutcome",
    "CaseResult",
    "EvalReport",
]
