from __future__ import annotations

from dataclasses import replace

from agentkit.core.review import (
    OutputReviewChain,
    OutputReviewContext,
    OutputReviewFinding,
    OutputReviewResult,
    OutputSafetyReviewer,
)
from agentkit.core.execution.models import StrategyResult
from agentkit.core.langgraph_agent import UnifiedAgentGraph
from agentkit.core.safety import ContentSafetyGuard


class _FlagReviewer:
    def review(self, context: OutputReviewContext) -> OutputReviewResult:
        return OutputReviewResult(
            action="flag",
            output={**context.output, "checked": True},
            findings=(
                OutputReviewFinding(
                    code="quality.signal",
                    severity="medium",
                    message="需要人工关注。",
                ),
            ),
        )


class _BlockReviewer:
    def review(self, context: OutputReviewContext) -> OutputReviewResult:
        return OutputReviewResult(
            action="block",
            output=context.output,
            findings=(
                OutputReviewFinding(
                    code="policy.blocked",
                    severity="high",
                    message="输出不符合发布策略。",
                ),
            ),
        )


class _BrokenReviewer:
    def review(self, context: OutputReviewContext) -> OutputReviewResult:
        raise RuntimeError("不应泄露到审计的内部错误")


def _context(**changes: object) -> OutputReviewContext:
    base = OutputReviewContext(
        tenant_id="company_alpha",
        run_id="run-1",
        agent_id="general_agent",
        strategy="direct",
        status="completed",
        output={"message": "ok"},
    )
    return replace(base, **changes)


def test_output_review_chain_aggregates_flags_and_updated_output() -> None:
    result = OutputReviewChain([_FlagReviewer()]).review(_context())

    assert result.action == "flag"
    assert result.output == {"message": "ok", "checked": True}
    assert [finding.code for finding in result.findings] == ["quality.signal"]


def test_output_review_chain_stops_on_block() -> None:
    result = OutputReviewChain([_FlagReviewer(), _BlockReviewer()]).review(_context())

    assert result.action == "block"
    assert [finding.code for finding in result.findings] == [
        "quality.signal",
        "policy.blocked",
    ]


def test_output_review_chain_fails_closed_without_leaking_exception() -> None:
    result = OutputReviewChain([_BrokenReviewer()], fail_closed=True).review(_context())

    assert result.action == "block"
    assert result.findings[0].code == "reviewer.error"
    assert "内部错误" not in result.findings[0].message


def test_output_safety_reviewer_redacts_nested_pii() -> None:
    reviewer = OutputSafetyReviewer(ContentSafetyGuard())

    result = reviewer.review(
        _context(
            output={
                "message": "联系 user@example.com",
                "items": [{"owner": "other@example.com"}],
            }
        )
    )

    assert result.action == "flag"
    assert result.output == {
        "message": "联系 [REDACTED:email]",
        "items": [{"owner": "[REDACTED:email]"}],
    }
    assert {finding.path for finding in result.findings} == {
        "$.message",
        "$.items[0].owner",
    }


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def record(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((run_id, event_type, payload))


def test_graph_output_review_replaces_output_and_records_safe_summary() -> None:
    graph = object.__new__(UnifiedAgentGraph)
    graph._tenant_id = "company_alpha"
    graph._audit = _Audit()
    graph._output_review_chain = OutputReviewChain([OutputSafetyReviewer(ContentSafetyGuard())])

    update = graph._review_output(
        {
            "run_id": "run-1",
            "result": StrategyResult(
                status="completed",
                output={"message": "联系 user@example.com"},
            ),
        }
    )

    assert update["result"].output == {"message": "联系 [REDACTED:email]"}
    assert graph._audit.events == [
        (
            "run-1",
            "output_reviewed",
            {
                "status": "completed",
                "action": "flag",
                "finding_codes": ["safety.pii.email"],
                "finding_count": 1,
            },
        )
    ]


def test_graph_output_review_blocks_without_reexecuting_strategy() -> None:
    graph = object.__new__(UnifiedAgentGraph)
    graph._tenant_id = "company_alpha"
    graph._audit = _Audit()
    graph._output_review_chain = OutputReviewChain([_BlockReviewer()])

    update = graph._review_output(
        {
            "run_id": "run-1",
            "result": StrategyResult(status="completed", output={"message": "draft"}),
        }
    )

    assert update["result"].status == "blocked"
    assert update["result"].output == {
        "reason": "输出未通过治理审查。",
        "review": {
            "action": "block",
            "finding_codes": ["policy.blocked"],
        },
    }
