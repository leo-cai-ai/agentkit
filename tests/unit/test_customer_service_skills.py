from pathlib import Path

from agentkit.core.execution.models import (
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    ToolProvider,
    ToolRisk,
)
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.declarative_catalog import load_catalog, register_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repository_catalog_has_exactly_three_business_agents() -> None:
    catalog = load_catalog(REPO_ROOT)

    assert set(catalog.agents) == {
        "general_agent",
        "customer_service",
        "hr_recruiter",
        "xhs_growth",
    }
    assert catalog.agents["general_agent"].skills == ()


def test_customer_service_declares_four_governed_capabilities() -> None:
    catalog = load_catalog(REPO_ROOT)
    agent = catalog.agents["customer_service"]

    assert agent.context.rag.enabled is True
    assert agent.skills == (
        "customer.answer",
        "order.lookup",
        "logistics.diagnose",
        "refund.apply",
    )
    assert catalog.capabilities["customer.answer"].execution.reasoning is ReasoningStrategy.DIRECT
    assert catalog.capabilities["logistics.diagnose"].execution.reasoning is ReasoningStrategy.REACT
    assert (
        catalog.capabilities["refund.apply"].execution.orchestration is OrchestrationMode.WORKFLOW
    )
    assert catalog.tools["commerce.order.get"].provider is ToolProvider.PYTHON
    assert catalog.tools["refund.submit"].risk is ToolRisk.SIDE_EFFECT


def test_customer_order_tool_and_refund_handler_compile() -> None:
    catalog = load_catalog(REPO_ROOT)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    register_catalog(
        catalog,
        enabled_agent_ids={"customer_service"},
        agents=agents,
        skills=skills,
        tools=tools,
    )

    assert tools.get("commerce.order.get").handler({"order_id": "O-100"})["order_id"] == "O-100"
    assert skills.get("refund.apply").execution.orchestration is OrchestrationMode.WORKFLOW
    assert agents.get("customer_service").execution_policy.default_strategy is (
        ExecutionStrategyName.DIRECT
    )


def test_hr_and_xhs_use_new_execution_and_context_policies() -> None:
    catalog = load_catalog(REPO_ROOT)

    assert catalog.capabilities["candidate.rank"].execution.orchestration is OrchestrationMode.BATCH
    assert catalog.agents["xhs_growth"].context.rag.enabled is False
    assert (
        catalog.capabilities["xhs.growth.campaign"].execution.orchestration
        is OrchestrationMode.WORKFLOW
    )
