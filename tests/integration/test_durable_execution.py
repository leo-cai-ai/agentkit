from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentkit.core.artifacts import InMemoryArtifactStore
from agentkit.core.audit import SQLiteAuditLog
from agentkit.core.context.errors import ContextHashMismatchError
from agentkit.core.contracts import TaskRequest
from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.models import AutonomyBudget
from agentkit.core.execution.registry import StrategyRegistry
from agentkit.core.execution.selector import StrategySelector
from agentkit.core.execution.workflow import WorkflowStrategy
from agentkit.core.gateway import AgentGateway, build_checkpointer
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from tests.integration.test_unified_agent_graph import _agent, _intent, _skill


def _durable_gateway(tmp_path, calls: list[str], *, context_invoker=None) -> AgentGateway:
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    agents.register(_agent("customer_service", ["refund.apply"]))
    skills.register(
        _skill(
            "refund.apply",
            lambda ctx, args: calls.append(args["marker"]) or {"refund": "R-1"},
            workflow=True,
            side_effect=True,
        )
    )
    return AgentGateway(
        tenant_id="t1",
        tenant_selector="company_alpha",
        tenant_config={},
        agents=agents,
        skills=skills,
        tools=tools,
        audit=SQLiteAuditLog(tmp_path / "audit.sqlite"),
        context_invoker=context_invoker or SimpleNamespace(manifest_hash="sha256:test"),
        checkpointer=build_checkpointer(mode="sqlite", sqlite_path=tmp_path / "checkpoints.sqlite"),
        selector=StrategySelector(
            skills=skills,
            global_budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        ),
        strategies=StrategyRegistry([DirectStrategy(), WorkflowStrategy()]),
        intent_resolver=_intent,
        artifact_store_factory=lambda run_id: InMemoryArtifactStore(),
    )


def test_sqlite_checkpoint_resumes_across_runtime_restart(tmp_path) -> None:
    calls: list[str] = []
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="退款",
        context={
            "agent": "customer_service",
            "skill": "refund.apply",
            "skill_args": {"marker": "once"},
        },
    )
    waiting = _durable_gateway(tmp_path, calls).handle(request)

    resumed_gateway = _durable_gateway(tmp_path, calls)
    resumed = resumed_gateway.resume(waiting.thread_id, approved_skills=["refund.apply"])

    assert waiting.status == "waiting_for_approval"
    assert resumed.status == "completed"
    assert resumed.run_id == waiting.run_id
    assert calls == ["once"]
    assert [item["type"] for item in resumed.audit_events].count("capability_resolved") == 1

    with pytest.raises(RuntimeError, match="未等待审批"):
        resumed_gateway.resume(waiting.thread_id, approved_skills=["refund.apply"])


def test_resume_rejects_changed_context_manifest(tmp_path) -> None:
    calls: list[str] = []
    context_invoker = SimpleNamespace(manifest_hash="sha256:original")
    gateway = _durable_gateway(tmp_path, calls, context_invoker=context_invoker)
    waiting = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="退款",
            context={
                "agent": "customer_service",
                "skill": "refund.apply",
                "skill_args": {"marker": "once"},
            },
        )
    )
    context_invoker.manifest_hash = "sha256:changed"

    with pytest.raises(ContextHashMismatchError):
        gateway.resume(waiting.thread_id, approved_skills=["refund.apply"])

    assert calls == []


def test_sqlite_checkpoint_inspection_distinguishes_pending_completed_and_missing(
    tmp_path,
) -> None:
    calls: list[str] = []
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="退款",
        context={
            "agent": "customer_service",
            "skill": "refund.apply",
            "skill_args": {"marker": "once"},
        },
    )
    first = _durable_gateway(tmp_path, calls)
    waiting = first.handle(request)

    pending = first.approval_checkpoint(waiting.thread_id)
    assert pending.status == "pending"
    assert pending.response is None

    resumed = _durable_gateway(tmp_path, calls)
    completed_response = resumed.resume(
        waiting.thread_id,
        approved_skills=["refund.apply"],
    )
    completed = resumed.approval_checkpoint(waiting.thread_id)
    assert completed.status == "completed"
    assert completed.response == completed_response

    missing = resumed.approval_checkpoint("missing-thread")
    assert missing.status == "missing"
    assert missing.response is None
    assert calls == ["once"]
