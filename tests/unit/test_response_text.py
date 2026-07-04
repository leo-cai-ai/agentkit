from agentkit.core.response_text import format_task_output_text


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


def test_unknown_structured_output_does_not_dump_json() -> None:
    text = format_task_output_text(
        status="completed",
        output={"internal_payload": {"large": "value"}},
    )

    assert text == "任务已完成，可在运行追踪中查看详细结果。"
    assert "internal_payload" not in text
