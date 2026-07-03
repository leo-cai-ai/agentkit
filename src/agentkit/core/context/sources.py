"""Context 输入 Source、序列化器与裁剪器白名单。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_SOURCES = frozenset(
    {
        "request.message",
        "request.goal",
        "request.arguments",
        "request.language",
        "request.intent_baseline",
        "conversation.summary",
        "conversation.recent_messages",
        "memory.facts",
        "memory.exchange",
        "memory.summary_window",
        "rag.query",
        "rag.candidates",
        "routing.candidate_skills",
        "execution.allowed_tools",
        "execution.allowed_skills",
        "execution.observations",
        "execution.completed_artifacts",
        "execution.previous_failure",
        "execution.remaining_budget",
        "skill.ranking_result",
        "skill.article",
        "skill.research_quality",
        "skill.article_evidence",
        "skill.article_patterns",
        "skill.campaign",
    }
)

_SERIALIZERS = frozenset({"text", "canonical_json"})
_TRUNCATORS = frozenset({"head", "tail", "newest", "highest_score"})


def canonical_json(value: Any) -> str:
    """生成可复现、紧凑且保留中文的 JSON。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass(frozen=True)
class ContextSourceRegistry:
    """只允许 Runtime 明确注册的数据入口和确定性转换。"""

    sources: frozenset[str]
    serializers: frozenset[str] = _SERIALIZERS
    truncators: frozenset[str] = _TRUNCATORS

    @classmethod
    def default(cls) -> ContextSourceRegistry:
        return cls(sources=DEFAULT_SOURCES)

    def require_source(self, name: str) -> None:
        if name not in self.sources:
            raise ValueError(f"未注册 Context Source: {name}")

    def require_serializer(self, name: str) -> None:
        if name not in self.serializers:
            raise ValueError(f"未注册 Context Serializer: {name}")

    def require_truncator(self, name: str) -> None:
        if name not in self.truncators:
            raise ValueError(f"未注册 Context Truncator: {name}")

    def serialize(self, name: str, value: Any) -> str:
        self.require_serializer(name)
        if name == "canonical_json":
            return canonical_json(value)
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    def truncate_items(self, name: str, values: Iterable[Any], limit: int) -> list[Any]:
        self.require_truncator(name)
        if limit <= 0:
            return []
        items = list(values)
        if name == "head":
            return items[:limit]
        if name in {"tail", "newest"}:
            return items[-limit:]
        return sorted(items, key=_score_key)[:limit]


def _score_key(value: Any) -> tuple[float, str]:
    if isinstance(value, dict):
        raw_score = value.get("score", 0)
        identity = str(value.get("id") or value.get("artifact_id") or canonical_json(value))
    else:
        raw_score = 0
        identity = canonical_json(value)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0
    return (-score, identity)


__all__ = ["ContextSourceRegistry", "DEFAULT_SOURCES", "canonical_json"]
