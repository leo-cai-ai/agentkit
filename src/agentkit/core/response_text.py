"""任务结果的统一用户可读摘要。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_LEGACY_OUTPUT_MARKERS = frozenset(
    {"campaign_id", "workflow_status", "publish", "ranked_candidates"}
)


def format_task_output_text(*, status: str, output: Mapping[str, Any]) -> str:
    """把结构化 Task Output 转为适合聊天与持久化的简短摘要。"""

    data = dict(output)
    publish = _mapping(data.get("publish"))
    if status == "blocked" and publish:
        review = _mapping(publish.get("review"))
        reason = str(
            review.get("reason") or publish.get("reason") or "未通过质量门禁"
        )
        if _contains_chinese(reason):
            return f"内容审核未通过，未进入发布：{reason}"
        return f"Content review failed; publication was not started: {reason}"
    if _is_xhs_output(data):
        return _format_xhs_output(status=status, output=data)
    for key in ("answer", "message", "summary", "campaign_summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if status == "waiting_for_approval":
        approval = _mapping(data.get("approval"))
        skills = ", ".join(str(item) for item in approval.get("skills", []))
        return f"当前任务等待人工审批: {skills}" if skills else "当前任务等待人工审批。"
    if status == "needs_clarification":
        clarification = str(data.get("clarification") or "").strip()
        if clarification:
            return clarification
        missing = ", ".join(str(item) for item in data.get("missing_required", []))
        return f"请补充必填参数: {missing}" if missing else "请补充任务所需信息。"
    ranked = data.get("ranked_candidates")
    if isinstance(ranked, list):
        lines = [
            f"{index}. {item.get('name', item.get('candidate_id', 'candidate'))}"
            for index, item in enumerate(ranked, start=1)
            if isinstance(item, dict)
        ]
        if lines:
            return "\n".join(lines)
    if status in {"blocked", "failed", "rejected"}:
        return "任务未完成，可在运行追踪中查看失败详情。"
    return "任务已完成，可在运行追踪中查看详细结果。"


def normalize_persisted_assistant_text(content: str) -> str:
    """只读转换旧版结构化 assistant 消息；普通文本与未知 JSON 保持不变。"""

    text = str(content or "")
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError):
        return text
    if not isinstance(value, dict) or not (_LEGACY_OUTPUT_MARKERS & value.keys()):
        return text
    inferred_status = str(value.get("workflow_status") or "completed")
    publish = _mapping(value.get("publish"))
    if publish.get("status") == "blocked":
        inferred_status = "blocked"
    return format_task_output_text(status=inferred_status, output=value)


def _is_xhs_output(output: Mapping[str, Any]) -> bool:
    campaign_id = str(output.get("campaign_id") or "").upper()
    return output.get("platform") == "xiaohongshu" or campaign_id.startswith("XHS-")


def _format_xhs_output(*, status: str, output: Mapping[str, Any]) -> str:
    publish = _mapping(output.get("publish"))
    review = _mapping(publish.get("review"))
    publish_status = str(publish.get("status") or "")
    workflow_status = str(output.get("workflow_status") or status)
    topic = str(output.get("topic") or "").strip()
    language_source = topic + str(output.get("campaign_summary") or "")
    is_zh = _contains_chinese(language_source)
    topic_zh = f"“{topic}”" if topic else "当前"
    topic_en = f'"{topic}"' if topic else "the current"
    if publish_status == "blocked" or workflow_status == "blocked" or status == "blocked":
        reason = str(
            review.get("reason") or publish.get("reason") or "未通过质量门禁"
        )
        if is_zh or _contains_chinese(reason):
            return f"内容审核未通过，未进入发布：{reason}"
        return f"Content review failed; publication was not started: {reason}"
    if publish_status == "published":
        if is_zh:
            return f"已完成{topic_zh}主题研究、文案审核与发布。"
        return f"Completed research, copy review, and publication for {topic_en} topic."
    if publish_status == "awaiting_approval" or status == "waiting_for_approval":
        if is_zh:
            return f"已完成{topic_zh}主题研究和文案审核，等待人工确认发布。"
        return (
            f"Completed research and copy review for {topic_en} topic; "
            "awaiting publication approval."
        )
    if publish_status == "draft_created":
        if is_zh:
            return f"已完成{topic_zh}主题研究并生成草稿。"
        return f"Completed research for {topic_en} topic and created a draft."
    if is_zh:
        return f"已完成{topic_zh}主题研究与内容处理。"
    return f"Completed research and content processing for {topic_en} topic."


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))
