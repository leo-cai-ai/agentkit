from types import SimpleNamespace

from agentkit.core.contracts import TaskRequest
from agentkit.core.schema_input_resolver import SchemaInputResolver
from tests.context_support import SpyContextInvoker


def _agent():
    return SimpleNamespace(
        name="xhs_growth",
        max_tokens=20_000,
        autonomy_budget=SimpleNamespace(max_tokens=10_000),
        instructions="小红书 Agent",
    )


def _skill():
    return SimpleNamespace(
        name="xhs.growth.campaign",
        description="研究小红书内容并生成文案",
        skill_instructions="只使用已解析并验证的输入",
        input_schema={
            "type": "object",
            "required": ["topic"],
            "properties": {
                "topic": {"type": "string", "minLength": 1, "description": "研究主题"},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
    )


def test_complete_arguments_do_not_call_input_resolution_llm() -> None:
    invoker = SpyContextInvoker()
    resolver = SchemaInputResolver(
        context_invoker=invoker,
        tenant_id="AI-ABC",
        tenant_selector="company_alpha",
    )

    result = resolver.resolve(
        TaskRequest(user_id="u1", roles=[], text="研究 AI 改变生活"),
        agent=_agent(),
        skill=_skill(),
        arguments={"topic": "AI 改变生活", "top_n": 5},
        run_id="r1",
    )

    assert result.arguments == {"topic": "AI 改变生活", "top_n": 5}
    assert result.missing == ()
    assert result.llm_used is False
    assert invoker.requests == []


def test_missing_required_input_is_resolved_from_skill_schema() -> None:
    invoker = SpyContextInvoker(
        {
            "resolved": {"topic": "AI 改变生活", "ignored": "越界字段"},
            "unresolved": [],
            "clarification": "",
            "confidence": "high",
        }
    )
    resolver = SchemaInputResolver(
        context_invoker=invoker,
        tenant_id="AI-ABC",
        tenant_selector="company_alpha",
    )
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="研究这个主题的小红书 Top 5",
        context={"agent_context": {"summary": "用户前文讨论 AI 改变生活"}},
    )

    result = resolver.resolve(
        request,
        agent=_agent(),
        skill=_skill(),
        arguments={"top_n": 5},
        run_id="r1",
    )

    assert result.arguments == {"top_n": 5, "topic": "AI 改变生活"}
    assert result.missing == ()
    assert result.llm_used is True
    assert invoker.requests[0].context_id == "runtime.input-resolve"
    assert invoker.requests[0].values["skill.missing_fields"] == ["topic"]
    assert "ignored" not in result.arguments


def test_invalid_llm_value_remains_unresolved_with_natural_clarification() -> None:
    invoker = SpyContextInvoker(
        {
            "resolved": {"topic": ""},
            "unresolved": ["topic"],
            "clarification": "你希望研究哪个具体主题？例如：AI 改变生活。",
            "confidence": "low",
        }
    )
    resolver = SchemaInputResolver(
        context_invoker=invoker,
        tenant_id="AI-ABC",
        tenant_selector="company_alpha",
    )

    result = resolver.resolve(
        TaskRequest(user_id="u1", roles=[], text="研究一下并发布"),
        agent=_agent(),
        skill=_skill(),
        arguments={},
        run_id="r1",
    )

    assert result.arguments == {}
    assert result.missing == ("topic",)
    assert result.clarification == "你希望研究哪个具体主题？例如：AI 改变生活。"
    assert result.confidence == "low"
