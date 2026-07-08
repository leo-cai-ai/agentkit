from pathlib import Path

import pytest

from agentkit.core.contracts import TaskRequest
from tests.integration.test_unified_agent_graph import _build_gateway


def test_runtime_uses_public_langgraph_interrupt_api() -> None:
    source = Path("src/agentkit/core/langgraph_agent.py").read_text(encoding="utf-8")

    assert "NodeInterrupt" not in source
    assert "from langgraph.types import Command, interrupt" in source
    assert "Command(resume=True)" in source


def test_resume_rejects_invalid_decisions(tmp_path) -> None:
    gateway = _build_gateway(tmp_path)
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="执行",
        context={
            "agent": "customer_service",
            "skill": "customer_service.echo",
            "skill_args": {"marker": "x"},
        },
    )
    completed = gateway.handle(request)

    with pytest.raises(RuntimeError, match="未等待审批"):
        gateway.resume(completed.thread_id, approved_skills=["customer_service.echo"])


def test_rejected_side_effect_never_executes(tmp_path) -> None:
    from tests.integration.test_durable_execution import _durable_gateway

    calls: list[str] = []
    gateway = _durable_gateway(tmp_path, calls)
    waiting = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="退款",
            context={
                "agent": "customer_service",
                "skill": "refund.apply",
                "skill_args": {"marker": "never"},
            },
        )
    )

    rejected = gateway.resume(waiting.thread_id, rejected_skills=["refund.apply"])

    assert rejected.status == "rejected"
    assert calls == []


def test_pending_approval_check_is_read_only(tmp_path) -> None:
    from tests.integration.test_durable_execution import _durable_gateway

    calls: list[str] = []
    gateway = _durable_gateway(tmp_path, calls)
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

    assert gateway.pending_approval(waiting.thread_id) is True
    assert calls == []

    gateway.resume(waiting.thread_id, approved_skills=["refund.apply"])

    assert gateway.pending_approval(waiting.thread_id) is False
    assert calls == ["once"]
