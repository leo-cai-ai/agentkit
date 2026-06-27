"""LLM-as-judge scoring for the evaluation harness.

The judge grades a target's output against a free-form rubric on a 1-5 scale and
returns a structured verdict. The LLM call is injectable (``judge_fn``) so tests
can run fully offline with a deterministic stub.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

JudgeFn = Callable[[str, str], dict[str, Any]]

JUDGE_SYSTEM = (
    "You are a strict evaluation judge. Grade the ASSISTANT OUTPUT against the "
    "RUBRIC on an integer scale from 1 (fails the rubric) to 5 (fully satisfies "
    "it). Be conservative. Respond with ONLY a JSON object: "
    '{"score": <1-5>, "reason": "<one sentence>"}.'
)


@dataclass(frozen=True)
class JudgeResult:
    score: float
    reason: str = ""


def _build_user(rubric: str, output: str, user: str) -> str:
    parts = [f"RUBRIC:\n{rubric}"]
    if user:
        parts.append(f"\nORIGINAL REQUEST:\n{user}")
    parts.append(f"\nASSISTANT OUTPUT:\n{output}")
    return "\n".join(parts)


class LLMJudge:
    def __init__(self, judge_fn: JudgeFn | None = None) -> None:
        self._judge_fn = judge_fn

    def _fn(self) -> JudgeFn:
        if self._judge_fn is not None:
            return self._judge_fn
        from agentkit.core import llm_client

        return llm_client.require_chat_json

    def score(self, *, output: str, rubric: str, user: str = "") -> JudgeResult:
        try:
            data = self._fn()(JUDGE_SYSTEM, _build_user(rubric, output, user))
        except Exception as exc:  # noqa: BLE001 - a judge failure should not crash the run
            return JudgeResult(score=0.0, reason=f"judge error: {exc}")
        try:
            raw = float(data.get("score", 0))
        except (TypeError, ValueError):
            raw = 0.0
        score = max(1.0, min(5.0, raw)) if raw else 0.0
        return JudgeResult(score=score, reason=str(data.get("reason", "")))


__all__ = ["JudgeResult", "LLMJudge", "JUDGE_SYSTEM"]
