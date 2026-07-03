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
