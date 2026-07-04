from agentkit.core.contracts import TaskResponse
from agentkit.web.app import format_response_text


def test_unified_response_formatter_prefers_business_summary() -> None:
    response = TaskResponse(
        status="completed",
        output={"campaign_summary": "已完成小红书内容草稿"},
        run_id="r1",
        thread_id="t1",
        agent="xhs_growth",
        strategy="workflow",
        conversation_id="c1",
        governance={},
        audit_events=[],
    )

    assert format_response_text(response) == "已完成小红书内容草稿"


def test_unified_response_formatter_explains_blocked_review() -> None:
    response = TaskResponse(
        status="blocked",
        output={
            "campaign_summary": "Prepared workflow",
            "publish": {
                "status": "blocked",
                "reason": "copy review failed",
                "review": {
                    "reason": "证据不足",
                    "findings": [{"severity": "error", "message": "无证据事实"}],
                },
            },
        },
        run_id="r-blocked",
        thread_id="t-blocked",
        agent="xhs_growth",
        strategy="workflow",
        conversation_id="c-blocked",
        governance={},
        audit_events=[],
    )

    assert format_response_text(response) == "内容审核未通过，未进入发布：证据不足"
