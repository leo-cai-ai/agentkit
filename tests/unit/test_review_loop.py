from __future__ import annotations

import pytest

from agentkit.core.review import (
    ReviewDecision,
    ReviewExecutionError,
    ReviewLoop,
    ReviewPolicy,
    ReviewTransition,
)


def test_review_loop_returns_candidate_when_first_review_passes() -> None:
    revised: list[str] = []

    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
        "draft",
        review=lambda candidate, attempt: ReviewDecision.passed(reason="grounded"),
        revise=lambda candidate, decision, attempt: revised.append(candidate) or "revised",
    )

    assert result.candidate == "draft"
    assert result.decision.status == "passed"
    assert result.revision_count == 0
    assert revised == []


def test_review_loop_revises_once_then_passes() -> None:
    reviewed: list[str] = []

    def review(candidate: str, attempt: int) -> ReviewDecision:
        reviewed.append(candidate)
        if candidate == "draft":
            return ReviewDecision.revisable(reason="unsupported claim")
        return ReviewDecision.passed(reason="grounded")

    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
        "draft",
        review=review,
        revise=lambda candidate, decision, attempt: "revised",
    )

    assert result.candidate == "revised"
    assert result.decision.status == "passed"
    assert result.revision_count == 1
    assert reviewed == ["draft", "revised"]


def test_review_loop_blocks_when_revision_budget_is_exhausted() -> None:
    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
        "draft",
        review=lambda candidate, attempt: ReviewDecision.revisable(reason="still unsafe"),
        revise=lambda candidate, decision, attempt: "revised",
    )

    assert result.candidate == "revised"
    assert result.decision.status == "blocked"
    assert result.decision.reason == "still unsafe"
    assert result.revision_count == 1
    assert [item.status for item in result.history] == ["revisable", "blocked"]


def test_review_loop_stops_immediately_when_review_blocks() -> None:
    revised: list[str] = []

    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=3)).run(
        "draft",
        review=lambda candidate, attempt: ReviewDecision.blocked(reason="policy violation"),
        revise=lambda candidate, decision, attempt: revised.append(candidate) or "revised",
    )

    assert result.decision.status == "blocked"
    assert result.revision_count == 0
    assert revised == []


@pytest.mark.parametrize(("stage", "review_raises"), [("review", True), ("revise", False)])
def test_review_loop_wraps_callback_errors(stage: str, review_raises: bool) -> None:
    def review(candidate: str, attempt: int) -> ReviewDecision:
        if review_raises:
            raise ValueError("review failed")
        return ReviewDecision.revisable(reason="rewrite")

    def revise(candidate: str, decision: ReviewDecision, attempt: int) -> str:
        raise ValueError("revision failed")

    with pytest.raises(ReviewExecutionError) as error:
        ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
            "draft",
            review=review,
            revise=revise,
        )

    assert error.value.stage == stage


def test_review_loop_reports_review_and_revision_transitions() -> None:
    transitions: list[ReviewTransition] = []

    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
        "draft",
        review=lambda candidate, attempt: (
            ReviewDecision.revisable(reason="rewrite")
            if attempt == 0
            else ReviewDecision.passed(reason="grounded")
        ),
        revise=lambda candidate, decision, attempt: "revised",
        on_transition=transitions.append,
    )

    assert result.decision.status == "passed"
    assert [(item.stage, item.attempt) for item in transitions] == [
        ("review", 0),
        ("revise", 1),
        ("review", 1),
    ]
