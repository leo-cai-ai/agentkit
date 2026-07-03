from __future__ import annotations

import pytest

from agentkit.core.contracts import (
    AgentProfile,
    ArtifactContextPolicy,
    ContextPolicy,
    MemoryContextPolicy,
    RagContextPolicy,
)
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    ExecutionStrategyName,
)
from agentkit.core.multi_agent import (
    AgentDirectory,
    AgentMentionParser,
    MultipleAgentMentionsError,
    UnknownAgentMentionError,
)
from agentkit.core.registry import AgentRegistry


def _profile(name: str, description: str, skills: list[str]) -> AgentProfile:
    return AgentProfile(
        name=name,
        domain=f"demo.{name}",
        description=description,
        allowed_skills=skills,
        execution_policy=AgentExecutionPolicy(
            default_strategy=ExecutionStrategyName.DIRECT,
            allowed_strategies=(ExecutionStrategyName.DIRECT,),
        ),
        autonomy_budget=AutonomyBudget(
            max_model_calls=4,
            max_tool_calls=4,
            max_iterations=4,
            max_plan_steps=4,
            max_replans=1,
            max_tokens=4000,
            timeout_seconds=30,
        ),
        context_policy=ContextPolicy(
            memory=MemoryContextPolicy(True, "agent_user", 6, 2000),
            rag=RagContextPolicy(False, (), 3, 1000),
            artifacts=ArtifactContextPolicy((), ()),
        ),
        instructions=f"# {name}",
    )


@pytest.fixture()
def directory() -> AgentDirectory:
    registry = AgentRegistry()
    registry.register(_profile("general_agent", "通用协调", []))
    registry.register(_profile("hr_recruiter", "招聘筛选", ["candidate.rank"]))
    registry.register(_profile("customer_service", "订单与售后", ["order.lookup"]))
    return AgentDirectory(
        agents=registry,
        config={
            "general_agent": {"label": "General Agent", "aliases": ["通用"]},
            "hr_recruiter": {"label": "招聘 Agent", "aliases": ["招聘", "HR"]},
            "customer_service": {"label": "客服 Agent", "aliases": ["客服"]},
        },
    )


def test_explicit_mention_routes_only_the_current_message(directory) -> None:
    parser = AgentMentionParser(directory)

    first = parser.parse("@招聘 请分析候选人")
    second = parser.parse("再说说他的风险")

    assert first.agent_id == "hr_recruiter"
    assert first.task_text == "请分析候选人"
    assert second.agent_id is None
    assert second.task_text == "再说说他的风险"


def test_agent_id_and_label_are_valid_mentions(directory) -> None:
    parser = AgentMentionParser(directory)

    assert parser.parse("@hr_recruiter 排序").agent_id == "hr_recruiter"
    assert parser.parse("@招聘 Agent 排序").agent_id == "hr_recruiter"


def test_unknown_and_multiple_mentions_are_deterministic_errors(directory) -> None:
    parser = AgentMentionParser(directory)

    with pytest.raises(UnknownAgentMentionError, match="未知 Agent"):
        parser.parse("@财务 生成报表")
    with pytest.raises(MultipleAgentMentionsError, match="一个 Agent"):
        parser.parse("@招聘 @客服 一起处理")


def test_directory_exposes_only_business_agent_capability_cards(directory) -> None:
    cards = directory.business_cards()

    assert [card["id"] for card in cards] == ["customer_service", "hr_recruiter"]
    assert cards[1]["label"] == "招聘 Agent"
    assert cards[1]["skills"] == ["candidate.rank"]


def test_directory_rejects_alias_for_an_unregistered_agent() -> None:
    registry = AgentRegistry()
    registry.register(_profile("general_agent", "通用协调", []))

    with pytest.raises(ValueError, match="未启用 Agent"):
        AgentDirectory(
            agents=registry,
            config={"finance": {"label": "财务", "aliases": ["财务"]}},
        )
