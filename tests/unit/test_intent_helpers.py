from agentkit.core.contracts import TaskRequest
from agentkit.core.intent import (
    IntentDecomposer,
    detect_language,
    extract_entities,
    looks_like_business_task,
    normalize_text,
)
from tests.context_support import SpyContextInvoker
from tests.unit.test_capability_resolution import _agent


def test_detect_language_zh_vs_en():
    assert detect_language("你好") == "zh-CN"
    assert detect_language("hello") == "en"


def test_normalize_text_collapses_whitespace_and_lowercases():
    assert normalize_text("  Rank   THE  Top ") == "rank the top"


def test_extract_entities_from_text_when_context_empty():
    req = TaskRequest(
        user_id="u",
        roles=[],
        text="Rank candidates for JOB-001: C-100 and C-101",
    )
    entities = extract_entities(req)
    assert entities["job_id"] == "JOB-001"
    assert entities["candidate_ids"] == ["C-100", "C-101"]


def test_extract_entities_recovers_xhs_topic_and_top_n() -> None:
    request = TaskRequest(
        user_id="u",
        roles=[],
        text="围绕“AI改变生活”为主题，研究小红书热门前5内容",
    )

    entities = extract_entities(request)

    assert entities["topic"] == "AI改变生活"
    assert entities["top_n"] == 5


def test_extract_entities_accepts_reversed_curly_quotes_around_topic() -> None:
    request = TaskRequest(
        user_id="u",
        roles=[],
        text="以”AI 改变生活“为主题，研究小红书 top 5 的文案，比较写一篇文案并发布。",
    )

    entities = extract_entities(request)

    assert entities["topic"] == "AI 改变生活"
    assert entities["top_n"] == 5


def test_looks_like_business_task_detects_action_term():
    assert looks_like_business_task(text="please rank them", entities={}) is True
    assert looks_like_business_task(text="hello there", entities={}) is False


def test_intent_decomposer_only_passes_declared_context_sources() -> None:
    invoker = SpyContextInvoker(
        {
            "language": "zh-CN",
            "intent_type": "business_task",
            "goal": "查询订单",
            "target": {"kind": "business_skill", "name": "order.lookup"},
            "entities": {},
            "confidence": "high",
            "clarification": "",
            "signals": [],
        }
    )
    decomposer = IntentDecomposer(
        context_invoker=invoker,
        tenant_id="AI-ABC",
        tenant_selector="company_alpha",
    )
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="查询我的订单",
        context={
            "agent_context": {
                "summary": "用户正在跟进订单",
                "knowledge": [{"secret": "不应进入意图上下文"}],
            },
            "tool_credentials": "绝不能注入",
        },
    )

    result = decomposer.decompose(request, agent=_agent(), run_id="r1")

    assert result.target == {"kind": "business_skill", "name": "order.lookup"}
    rendered_request = invoker.requests[0]
    assert rendered_request.context_id == "runtime.intent"
    assert rendered_request.values.keys() == {
        "request.message",
        "conversation.summary",
        "request.intent_baseline",
    }
    assert rendered_request.values["conversation.summary"] == "用户正在跟进订单"
    assert "knowledge" not in str(rendered_request.values)
    assert "tool_credentials" not in str(rendered_request.values)


def test_intent_llm_empty_entity_does_not_erase_rule_extraction() -> None:
    invoker = SpyContextInvoker(
        {
            "language": "zh-CN",
            "intent_type": "business_task",
            "goal": "研究并发布小红书文案",
            "target": {"kind": "business_skill", "name": "xhs.growth.campaign"},
            "entities": {"topic": ""},
            "confidence": "high",
            "clarification": "",
            "signals": [],
        }
    )
    decomposer = IntentDecomposer(
        context_invoker=invoker,
        tenant_id="AI-ABC",
        tenant_selector="company_alpha",
    )
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="以”AI 改变生活“为主题，研究小红书 top 5 并发布。",
    )

    result = decomposer.decompose(request, agent=_agent(), run_id="r1")

    assert result.entities["topic"] == "AI 改变生活"
    assert result.entities["top_n"] == 5
