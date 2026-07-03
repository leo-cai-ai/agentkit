from __future__ import annotations

from typing import Any

import agentkit.core.input_resolution as resolution_module
from agentkit.core.contracts import (
    IntentFrame,
    RouteDecision,
    SkillDefinition,
    TaskRequest,
)
from agentkit.core.execution.models import (
    AutonomyLimits,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    ToolPolicy,
)
from agentkit.core.input_resolution import SkillInputResolver
from agentkit.core.registry import SkillRegistry


def _skill() -> SkillDefinition:
    return SkillDefinition(
        name="xhs.growth.campaign",
        domain="marketing.social_growth",
        description="Research and prepare Xiaohongshu content.",
        input_schema={
            "type": "object",
            "required": ["topic"],
            "x-agentkit-infer-from-message": True,
            "properties": {
                "topic": {
                    "type": "string",
                    "minLength": 1,
                    "x-agentkit-label": "选题",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
            },
        },
        output_schema={},
        permissions=[],
        execution=SkillExecutionPolicy(
            reasoning=ReasoningStrategy.DIRECT,
            orchestration=OrchestrationMode.WORKFLOW,
            tool_policy=ToolPolicy.GOVERNED,
        ),
        autonomy=AutonomyLimits(),
        tools=[],
        handler=lambda ctx, args: {},
    )


def _resolver() -> SkillInputResolver:
    skills = SkillRegistry()
    skills.register(_skill())
    return SkillInputResolver(tenant_config={}, skills=skills)


def _intent(*, entities: dict[str, Any] | None = None) -> IntentFrame:
    return IntentFrame(
        raw_text="",
        language="zh-CN",
        intent_type="business_task",
        goal="研究小红书案例并生成草稿",
        boundaries={},
        entities=entities or {},
        target={"kind": "business_skill", "name": "xhs.growth.campaign"},
        confidence="high",
    )


def _route() -> RouteDecision:
    return RouteDecision(skill_name="xhs.growth.campaign", reason="test", confidence="high")


def test_structured_inputs_bypass_semantic_extraction(monkeypatch) -> None:
    def fail_if_called(system: str, user: str) -> dict[str, Any]:
        raise AssertionError("semantic extraction should not run")

    monkeypatch.setattr(resolution_module, "require_chat_json", fail_if_called)
    result = _resolver().resolve(
        request=TaskRequest(
            user_id="u",
            roles=[],
            text="执行",
            context={"skill_args": {"topic": "暑假带娃旅游", "top_n": 8}},
        ),
        intent=_intent(),
        route=_route(),
    )

    assert result.complete is True
    assert result.llm_used is False
    assert result.arguments == {"topic": "暑假带娃旅游", "top_n": 8}
    assert result.slots["topic"].source == "request_skill_args"


def test_natural_language_is_resolved_semantically(monkeypatch) -> None:
    monkeypatch.setattr(
        resolution_module,
        "require_chat_json",
        lambda system, user: {
            "arguments": {"topic": "暑假带娃旅游", "top_n": 5},
            "confidence": {"topic": 0.97, "top_n": 0.99},
            "missing_required": [],
            "clarification": "",
        },
    )
    result = _resolver().resolve(
        request=TaskRequest(
            user_id="u",
            roles=[],
            text="帮我看看暑假带娃去哪里玩比较火，找五篇案例再写一篇",
        ),
        intent=_intent(),
        route=_route(),
    )

    assert result.complete is True
    assert result.llm_used is True
    assert result.arguments == {"topic": "暑假带娃旅游", "top_n": 5}
    assert result.slots["topic"].source == "llm_slot_extraction"
    assert result.slots["topic"].confidence == 0.97


def test_low_confidence_required_input_requests_clarification(monkeypatch) -> None:
    monkeypatch.setattr(
        resolution_module,
        "require_chat_json",
        lambda system, user: {
            "arguments": {"topic": "可能是旅游"},
            "confidence": {"topic": 0.2},
            "missing_required": ["topic"],
            "clarification": "请告诉我这次要研究的具体选题。",
        },
    )
    result = _resolver().resolve(
        request=TaskRequest(user_id="u", roles=[], text="照之前的做五篇"),
        intent=_intent(),
        route=_route(),
    )

    assert result.complete is False
    assert result.arguments == {"top_n": 5}
    assert result.missing_required == ["topic"]
    assert result.clarification == "请告诉我这次要研究的具体选题。"
    assert any("low-confidence" in error for error in result.errors)


def test_intent_entities_are_traceable_and_schema_defaults_are_applied(monkeypatch) -> None:
    monkeypatch.setattr(
        resolution_module,
        "require_chat_json",
        lambda system, user: {
            "arguments": {},
            "confidence": {},
            "missing_required": [],
            "clarification": "",
        },
    )
    result = _resolver().resolve(
        request=TaskRequest(user_id="u", roles=[], text="研究这个主题"),
        intent=_intent(entities={"topic": "企业级 Agent"}),
        route=_route(),
    )

    assert result.complete is True
    assert result.arguments == {"topic": "企业级 Agent", "top_n": 5}
    assert result.slots["topic"].source == "intent_rule"
    assert result.slots["top_n"].source == "schema_default"
    assert result.slots["top_n"].default_reason


def test_invalid_explicit_value_does_not_silently_fall_back_to_default(monkeypatch) -> None:
    monkeypatch.setattr(
        resolution_module,
        "require_chat_json",
        lambda system, user: {
            "arguments": {},
            "confidence": {},
            "missing_required": [],
            "clarification": "",
        },
    )
    result = _resolver().resolve(
        request=TaskRequest(
            user_id="u",
            roles=[],
            text="研究暑假带娃旅游，抓 100 篇",
            context={"topic": "暑假带娃旅游", "top_n": 100},
        ),
        intent=_intent(),
        route=_route(),
    )

    assert result.complete is False
    assert result.arguments == {"topic": "暑假带娃旅游"}
    assert result.invalid_fields == ["top_n"]
    assert "请修正" in result.clarification
