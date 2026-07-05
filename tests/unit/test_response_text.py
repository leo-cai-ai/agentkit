import json

from agentkit.core.response_text import (
    format_task_output_text,
    normalize_persisted_assistant_text,
)


def test_xhs_published_summary_uses_actual_outcome() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI时代的副业",
        "campaign_summary": (
            "Prepared a reviewed 30-day Xiaohongshu workflow targeting "
            "10000 new followers with daily publishing."
        ),
        "workflow_status": "completed",
        "publish": {"status": "published"},
    }

    assert format_task_output_text(status="completed", output=output) == (
        "已完成“AI时代的副业”主题研究、文案审核与发布。"
    )


def test_xhs_waiting_summary_describes_human_approval() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI工具",
        "publish": {"status": "awaiting_approval"},
    }

    assert format_task_output_text(status="waiting_for_approval", output=output) == (
        "已完成“AI工具”主题研究和文案审核，等待人工确认发布。"
    )


def test_xhs_draft_summary_describes_saved_draft() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI工具",
        "publish": {"status": "draft_created"},
    }

    assert format_task_output_text(status="completed", output=output) == (
        "已完成“AI工具”主题研究并生成草稿。"
    )


def test_xhs_blocked_summary_keeps_review_reason() -> None:
    output = {
        "platform": "xiaohongshu",
        "workflow_status": "blocked",
        "publish": {
            "status": "blocked",
            "reason": "copy review failed",
            "review": {"reason": "证据不足"},
        },
    }

    assert format_task_output_text(status="blocked", output=output) == (
        "内容审核未通过，未进入发布：证据不足"
    )


def test_generic_output_prefers_explicit_message() -> None:
    assert (
        format_task_output_text(
            status="completed",
            output={"message": "订单已查询"},
        )
        == "订单已查询"
    )


def test_clarification_prefers_natural_question_over_internal_field_name() -> None:
    text = format_task_output_text(
        status="needs_clarification",
        output={
            "missing_required": ["topic"],
            "clarification": "我还没识别到你想研究的主题，可以再具体说一下吗？",
        },
    )

    assert text == "我还没识别到你想研究的主题，可以再具体说一下吗？"


def test_unknown_structured_output_does_not_dump_json() -> None:
    text = format_task_output_text(
        status="completed",
        output={"internal_payload": {"large": "value"}},
    )

    assert text == "任务已完成，可在运行追踪中查看详细结果。"
    assert "internal_payload" not in text


def test_legacy_xhs_json_message_is_normalized() -> None:
    legacy = json.dumps(
        {
            "campaign_id": "XHS-30D-10000",
            "platform": "xiaohongshu",
            "topic": "AI时代的副业",
            "workflow_status": "blocked",
            "publish": {
                "status": "blocked",
                "review": {"reason": "证据不足"},
            },
        },
        ensure_ascii=False,
    )

    assert normalize_persisted_assistant_text(legacy) == (
        "内容审核未通过，未进入发布：证据不足"
    )


def test_normal_markdown_and_unrecognized_json_are_not_rewritten() -> None:
    assert normalize_persisted_assistant_text("**正常回答**") == "**正常回答**"
    assert normalize_persisted_assistant_text('{"example": true}') == '{"example": true}'
    assert normalize_persisted_assistant_text('[{"campaign_id": "x"}]') == (
        '[{"campaign_id": "x"}]'
    )
