from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.context.errors import ContextOutputInvalidError
from agentkit.core.contracts import TaskRequest, TaskResponse
from agentkit.core.multi_agent import AgentDirectory, MultiAgentCoordinator
from agentkit.core.registry import AgentRegistry
from agentkit.runtime.conversation_context import AgentConversationContext
from tests.unit.test_multi_agent import _profile


class FakePersistence:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.turns: list[dict] = []

    def create_conversation(self, **kwargs) -> str:
        self.created.append(kwargs)
        return "conversation-1"

    def record_turn(self, **kwargs) -> None:
        self.turns.append(kwargs)


class FakeContextService:
    def __init__(self) -> None:
        self.delegations: list[dict] = []

    def build(self, **kwargs) -> AgentConversationContext:
        return AgentConversationContext(
            conversation_id=kwargs["conversation_id"],
            summary="用户正在讨论招聘",
            recent_messages=({"role": "user", "content": "前一轮消息"},),
            memories=("用户偏好中文",),
            knowledge=(),
        )

    def build_for_delegation(self, **kwargs) -> AgentConversationContext:
        self.delegations.append(kwargs)
        return AgentConversationContext(
            conversation_id=kwargs["conversation_id"],
            summary="用户正在讨论招聘",
            recent_messages=({"role": "user", "content": "前一轮消息"},),
            memories=("招聘 Agent 的长期记忆",),
            knowledge=("招聘制度",),
        )


class FakeInvoker:
    manifest_hash = "sha256:test"

    def __init__(self, decision: dict | None = None) -> None:
        self.decision = decision or {
            "action": "answer",
            "target_agent": None,
            "task": "",
            "reason": "普通交流",
            "confidence": "high",
        }
        self.json_calls = []
        self.streaming_calls = []

    def invoke_json(self, request):
        self.json_calls.append(request)
        return SimpleNamespace(value=dict(self.decision))

    def invoke_streaming(self, request):
        self.streaming_calls.append(request)
        return SimpleNamespace(value="我是 General Agent，可以协调业务助手。")


class FakeGateway:
    def __init__(
        self,
        audit: InMemoryAuditLog,
        *,
        status: str = "completed",
        output: dict | None = None,
    ) -> None:
        self.audit = audit
        self.status = status
        self.output = output or {"message": "招聘分析已完成"}
        self.requests: list[TaskRequest] = []

    def handle_delegated(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        child_run = self.audit.start_run(
            tenant_id="tenant-a",
            user_id=request.user_id,
            text=request.text,
            agent_id=str(request.context["agent"]),
            parent_run_id=str(request.context["parent_run_id"]),
            conversation_id=str(request.context["trace_conversation_id"]),
        )
        self.audit.record(child_run, "run_finished", {"status": self.status})
        return TaskResponse(
            status=self.status,
            output=dict(self.output),
            run_id=child_run,
            thread_id="child-thread",
            agent=str(request.context["agent"]),
            strategy="direct",
            conversation_id="",
            governance={"strategy": "direct"},
            audit_events=self.audit.events_for(child_run),
        )

    def resume(self, thread_id: str, **kwargs) -> TaskResponse:
        child = self.audit.run_for_thread(thread_id, tenant_id="tenant-a", user_id="u1")
        self.audit.record(child["run_id"], "run_resumed", {"thread_id": thread_id})
        self.audit.record(child["run_id"], "run_finished", {"status": "completed"})
        return TaskResponse(
            status="completed",
            output={"message": "审批后已执行"},
            run_id=child["run_id"],
            thread_id=thread_id,
            agent=child["agent_id"],
            strategy="workflow",
            conversation_id="",
            governance={"strategy": "workflow"},
            audit_events=self.audit.events_for(child["run_id"]),
        )


def _service(
    decision: dict | None = None,
    *,
    child_status: str = "completed",
    child_output: dict | None = None,
):
    agents = AgentRegistry()
    agents.register(_profile("general_agent", "通用协调", []))
    agents.register(_profile("hr_recruiter", "招聘筛选", ["candidate.rank"]))
    agents.register(_profile("customer_service", "订单与售后", ["order.lookup"]))
    directory = AgentDirectory(
        agents=agents,
        config={
            "general_agent": {"label": "General Agent", "aliases": ["通用"]},
            "hr_recruiter": {"label": "招聘 Agent", "aliases": ["招聘"]},
            "customer_service": {"label": "客服 Agent", "aliases": ["客服"]},
        },
    )
    audit = InMemoryAuditLog()
    persistence = FakePersistence()
    contexts = FakeContextService()
    invoker = FakeInvoker(decision)
    gateway = FakeGateway(audit, status=child_status, output=child_output)
    service = MultiAgentCoordinator(
        tenant_id="tenant-a",
        tenant_selector="company_alpha",
        directory=directory,
        gateway=gateway,
        audit=audit,
        context_invoker=invoker,
        conversation_context=contexts,
        conversation_persistence=persistence,
    )
    return service, gateway, audit, invoker, contexts, persistence


def test_general_agent_owns_conversation_and_answers_normal_message() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service()

    response = service.handle(
        TaskRequest(user_id="u1", roles=["employee"], text="你好", context={})
    )

    assert response.status == "completed"
    assert response.agent == "general_agent"
    assert response.conversation_id == "conversation-1"
    assert response.output["message"].startswith("我是 General Agent")
    assert gateway.requests == []
    assert len(invoker.json_calls) == 1
    assert len(invoker.streaming_calls) == 1
    assert persistence.created[0]["agent_id"] == "general_agent"
    assert persistence.turns[0]["assistant_agent_id"] == "general_agent"
    assert audit.get_run(response.run_id)["agent_id"] == "general_agent"


def test_retry_relationship_and_user_outcome_reach_conversation_persistence() -> None:
    service, _gateway, _audit, _invoker, _contexts, persistence = _service()

    response = service.handle(
        TaskRequest(
            user_id="u1",
            roles=["employee"],
            text="你好",
            context={
                "conversation_id": "conversation-existing",
                "retry_of_run_id": "run-old",
            },
        )
    )

    assert response.status == "completed"
    assert persistence.turns[0]["retry_of_run_id"] == "run-old"
    assert persistence.turns[0]["outcome"] == "succeeded"


def test_explicit_mention_skips_router_and_creates_child_run() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service()

    response = service.handle(
        TaskRequest(
            user_id="u1",
            roles=["recruiter"],
            text="@招聘 分析候选人",
            context={"conversation_id": "conversation-existing"},
        )
    )

    assert invoker.json_calls == []
    assert response.agent == "hr_recruiter"
    assert response.governance["route"]["type"] == "explicit_mention"
    assert response.governance["delegation"]["child_run_id"]
    delegated = gateway.requests[0]
    assert delegated.text == "分析候选人"
    assert delegated.context["agent"] == "hr_recruiter"
    assert "conversation_id" not in delegated.context
    child = audit.get_run(response.governance["delegation"]["child_run_id"])
    assert child["parent_run_id"] == response.run_id
    assert persistence.turns[0]["assistant_agent_id"] == "hr_recruiter"


def test_general_agent_propagates_blocked_child_status() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service(child_status="blocked")

    response = service.handle(
        TaskRequest(
            user_id="u1",
            roles=["growth_manager"],
            text="@招聘 审核这份内容",
            context={"conversation_id": "conversation-existing"},
        )
    )

    assert response.status == "blocked"
    assert response.governance["delegation"]["status"] == "blocked"
    assert audit.get_run(response.run_id)["status"] == "blocked"


def test_delegated_business_output_persists_same_user_facing_summary() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI时代的副业",
        "workflow_status": "completed",
        "publish": {"status": "published"},
    }
    service, _gateway, _audit, _invoker, _contexts, persistence = _service(
        {
            "action": "delegate",
            "target_agent": "customer_service",
            "task": "研究并发布小红书内容",
            "reason": "测试业务委派",
            "confidence": "high",
        },
        child_output=output,
    )

    service.handle(
        TaskRequest(
            user_id="u1",
            roles=["growth_manager"],
            text="研究并发布小红书内容",
            context={"conversation_id": "conversation-existing"},
        )
    )

    assert persistence.turns[0]["assistant_message"] == (
        "已完成“AI时代的副业”主题研究、文案审核与发布。"
    )
    assert not persistence.turns[0]["assistant_message"].startswith("{")


def test_general_router_can_delegate_without_explicit_mention() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service(
        {
            "action": "delegate",
            "target_agent": "customer_service",
            "task": "查询订单 O-1 的物流",
            "reason": "属于订单物流能力",
            "confidence": "high",
        }
    )

    response = service.handle(
        TaskRequest(
            user_id="u1",
            roles=["support"],
            text="我的 O-1 怎么还没到",
            context={},
        )
    )

    assert response.agent == "customer_service"
    assert response.governance["route"]["type"] == "general_delegate"
    assert gateway.requests[0].text == "查询订单 O-1 的物流"
    assert contexts.delegations[0]["agent"].name == "customer_service"
    assert gateway.requests[0].context["agent_context"]["knowledge"] == ["招聘制度"]


def test_invalid_router_output_stops_without_fake_execution() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service()

    def fail_route(_request) -> None:
        raise ContextOutputInvalidError(
            "runtime.agent-route: 输出不符合 Schema: 'task' is required",
            context_id="runtime.agent-route",
        )

    invoker.invoke_json = fail_route
    response = service.handle(
        TaskRequest(
            user_id="u1",
            roles=["employee"],
            text="研究小红书热门内容",
            context={},
        )
    )

    assert response.status == "needs_clarification"
    assert response.governance["route"]["type"] == "route_failed"
    assert "未调用任何 Agent、Skill 或 Tool" in response.output["message"]
    assert gateway.requests == []
    assert invoker.streaming_calls == []
    assert any(event["type"] == "agent_route_failed" for event in audit.events_for(response.run_id))


def test_approval_resume_returns_to_the_original_general_conversation() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service()
    parent = audit.start_run(
        tenant_id="tenant-a",
        user_id="u1",
        text="@招聘 发出录用通知",
        agent_id="general_agent",
        conversation_id="conversation-approval",
    )
    child = audit.start_run(
        tenant_id="tenant-a",
        user_id="u1",
        text="发出录用通知",
        agent_id="hr_recruiter",
        parent_run_id=parent,
        conversation_id="conversation-approval",
    )
    audit.record(
        parent,
        "agent_route_decided",
        {
            "type": "explicit_mention",
            "action": "delegate",
            "target_agent": "hr_recruiter",
            "reason": "用户显式指定",
            "confidence": "high",
        },
    )
    audit.record(
        child,
        "run_paused",
        {"status": "waiting_for_approval", "thread_id": "approval-thread"},
    )

    response = service.resume(
        "approval-thread",
        user_id="u1",
        roles=["recruiter"],
        approved_skills=["offer.send"],
    )

    assert response.run_id == parent
    assert response.conversation_id == "conversation-approval"
    assert response.agent == "hr_recruiter"
    assert persistence.turns[0]["user_message"] == "@招聘 发出录用通知"
    assert persistence.turns[0]["assistant_agent_id"] == "hr_recruiter"


def test_retry_relationship_survives_approval_pause_and_resume() -> None:
    service, _gateway, audit, _invoker, _contexts, persistence = _service()
    parent = audit.start_run(
        tenant_id="tenant-a",
        user_id="u1",
        text="@招聘 发出录用通知",
        agent_id="general_agent",
        conversation_id="conversation-approval",
    )
    child = audit.start_run(
        tenant_id="tenant-a",
        user_id="u1",
        text="发出录用通知",
        agent_id="hr_recruiter",
        parent_run_id=parent,
        conversation_id="conversation-approval",
    )
    audit.record(
        parent,
        "conversation_retry_started",
        {"retry_of_run_id": "run-old"},
    )
    audit.record(
        child,
        "run_paused",
        {"status": "waiting_for_approval", "thread_id": "approval-retry"},
    )

    service.resume(
        "approval-retry",
        user_id="u1",
        roles=["recruiter"],
        approved_skills=["offer.send"],
    )

    assert persistence.turns[0]["retry_of_run_id"] == "run-old"


def test_context_failure_finishes_parent_run_without_persisting_turn() -> None:
    service, _, audit, _, contexts, persistence = _service()

    def fail_context(**kwargs):
        raise RuntimeError("context storage unavailable")

    contexts.build = fail_context

    with pytest.raises(RuntimeError, match="context storage unavailable"):
        service.handle(
            TaskRequest(
                user_id="u1",
                roles=["employee"],
                text="你好",
                context={"conversation_id": "conversation-failed"},
            )
        )

    runs = audit.runs_for_conversation(
        conversation_id="conversation-failed",
        tenant_id="tenant-a",
        user_id="u1",
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert [event["type"] for event in audit.events_for(runs[0]["run_id"])][-2:] == [
        "run_failed",
        "run_finished",
    ]
    assert persistence.turns == []


def test_delegation_failure_finishes_parent_run_without_persisting_turn() -> None:
    service, gateway, audit, _, _, persistence = _service()

    def fail_delegation(request):
        raise RuntimeError("child execution failed")

    gateway.handle_delegated = fail_delegation

    with pytest.raises(RuntimeError, match="child execution failed"):
        service.handle(
            TaskRequest(
                user_id="u1",
                roles=["employee"],
                text="@招聘 分析候选人",
                context={"conversation_id": "conversation-failed"},
            )
        )

    runs = audit.runs_for_conversation(
        conversation_id="conversation-failed",
        tenant_id="tenant-a",
        user_id="u1",
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert persistence.turns == []


def test_resume_failure_finishes_waiting_parent_run() -> None:
    service, gateway, audit, _, _, persistence = _service()
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="u1",
        text="@招聘 发出录用通知",
        agent_id="general_agent",
        conversation_id="conversation-approval-failed",
    )
    child_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="u1",
        text="发出录用通知",
        agent_id="hr_recruiter",
        parent_run_id=parent_id,
        conversation_id="conversation-approval-failed",
    )
    audit.record(
        child_id,
        "run_paused",
        {"status": "waiting_for_approval", "thread_id": "approval-failed"},
    )
    audit.record(
        parent_id,
        "run_paused",
        {"status": "waiting_for_approval", "child_run_id": child_id},
    )

    def fail_resume(thread_id, **kwargs):
        raise RuntimeError("child resume failed")

    gateway.resume = fail_resume

    with pytest.raises(RuntimeError, match="child resume failed"):
        service.resume(
            "approval-failed",
            user_id="u1",
            roles=["employee"],
            approved_skills=["offer.send"],
        )

    assert audit.get_run(parent_id)["status"] == "failed"
    assert [event["type"] for event in audit.events_for(parent_id)][-2:] == [
        "run_failed",
        "run_finished",
    ]
    assert persistence.turns == []
