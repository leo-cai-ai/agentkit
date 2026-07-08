from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.context.errors import ContextOutputInvalidError
from agentkit.core.contracts import TaskRequest, TaskResponse
from agentkit.core.memory.store import ConversationStore
from agentkit.core.multi_agent import AgentDirectory, MultiAgentCoordinator
from agentkit.core.registry import AgentRegistry
from agentkit.runtime.conversation_context import AgentConversationContext
from agentkit.runtime.conversation_persistence import ConversationPersistenceService
from agentkit.runtime.conversation_projection import ConversationProjectionService
from tests.unit.test_multi_agent import _profile


class FakeContextService:
    def __init__(self) -> None:
        self.builds: list[dict] = []
        self.delegations: list[dict] = []

    def build(self, **kwargs) -> AgentConversationContext:
        self.builds.append(kwargs)
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
        if self.status == "waiting_for_approval":
            self.audit.record(
                child_run,
                "run_paused",
                {"status": self.status, "thread_id": "child-thread"},
            )
        else:
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


def _projection_service(
    tmp_path,
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
    store = ConversationStore(tmp_path / "conversation.sqlite")
    projection = ConversationProjectionService(store=store, audit=audit)
    persistence = ConversationPersistenceService(store=store, projection=projection)
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
        conversation_projection=projection,
    )
    return service, gateway, audit, invoker, contexts, projection


def _prepared_request(
    projection: ConversationProjectionService,
    *,
    message: str,
    client_message_id: str,
    conversation_id: str | None = None,
    roles: list[str] | None = None,
) -> TaskRequest:
    accepted = projection.accept_user_message(
        tenant_id="tenant-a",
        user_id="u1",
        conversation_id=conversation_id,
        client_message_id=client_message_id,
        content=message,
        title=message[:60],
    )
    return TaskRequest(
        user_id="u1",
        roles=roles or ["employee"],
        text=message,
        context={
            "conversation_id": accepted.conversation_id,
            "conversation_turn_id": accepted.turn_id,
            "conversation_attempt_id": accepted.attempt_id,
        },
    )


def _timeline(projection, conversation_id):
    return projection.timeline(
        conversation_id=conversation_id,
        tenant_id="tenant-a",
        user_id="u1",
    )


def test_general_agent_consumes_prepared_turn_and_projects_canonical_answer(tmp_path) -> None:
    service, gateway, audit, invoker, contexts, projection = _projection_service(tmp_path)
    request = _prepared_request(
        projection,
        message="你好",
        client_message_id="client-general",
    )

    response = service.handle(request)

    turn = _timeline(projection, response.conversation_id).turns[0]
    attempt = turn["attempts"][0]
    assert response.status == "completed"
    assert response.agent == "general_agent"
    assert attempt["status"] == "succeeded"
    assert attempt["stage"] == "finalizing"
    assert turn["canonical_attempt_id"] == attempt["id"]
    assert attempt["messages"][0]["content"] == response.output["message"]
    assert contexts.builds[0]["exclude_turn_id"] == turn["id"]
    assert gateway.requests == []
    assert len(invoker.json_calls) == len(invoker.streaming_calls) == 1
    assert audit.get_run(response.run_id)["status"] == "completed"


def test_handle_requires_trusted_prepared_projection_ids(tmp_path) -> None:
    service, *_ = _projection_service(tmp_path)

    with pytest.raises(KeyError, match="conversation_turn_id"):
        service.handle(
            TaskRequest(
                user_id="u1",
                roles=[],
                text="你好",
                context={"conversation_id": "prepared-conversation"},
            )
        )


def test_explicit_mention_propagates_projection_ids_to_business_agent(tmp_path) -> None:
    service, gateway, _, invoker, contexts, projection = _projection_service(tmp_path)
    request = _prepared_request(
        projection,
        message="@招聘 分析候选人",
        client_message_id="client-delegate",
        roles=["recruiter"],
    )

    response = service.handle(request)

    delegated = gateway.requests[0]
    turn = _timeline(projection, response.conversation_id).turns[0]
    attempt = turn["attempts"][0]
    assert invoker.json_calls == []
    assert delegated.context["conversation_turn_id"] == turn["id"]
    assert delegated.context["conversation_attempt_id"] == attempt["id"]
    assert "conversation_id" not in delegated.context
    assert delegated.context["trace_conversation_id"] == response.conversation_id
    assert contexts.delegations[0]["exclude_turn_id"] == turn["id"]
    assert attempt["status"] == "succeeded"
    assert attempt["messages"][0]["agent_id"] == "hr_recruiter"


def test_route_failure_projects_visible_terminal_clarification(tmp_path) -> None:
    service, gateway, _, invoker, _, projection = _projection_service(tmp_path)

    def fail_route(_request) -> None:
        raise ContextOutputInvalidError(
            "runtime.agent-route: 输出不符合 Schema",
            context_id="runtime.agent-route",
        )

    invoker.invoke_json = fail_route
    request = _prepared_request(
        projection,
        message="研究小红书热门内容",
        client_message_id="client-route-fail",
    )

    response = service.handle(request)

    attempt = _timeline(projection, response.conversation_id).turns[0]["attempts"][0]
    assert response.status == "needs_clarification"
    assert attempt["status"] == "rejected"
    assert "未调用任何 Agent、Skill 或 Tool" in attempt["messages"][0]["content"]
    assert gateway.requests == []


def test_context_failure_preserves_user_input_and_failed_attempt(tmp_path) -> None:
    service, _, audit, _, contexts, projection = _projection_service(tmp_path)

    def fail_context(**kwargs):
        raise RuntimeError("context down")

    contexts.build = fail_context
    request = _prepared_request(
        projection,
        message="你好",
        client_message_id="client-context-fail",
    )

    with pytest.raises(RuntimeError, match="context down"):
        service.handle(request)

    timeline = projection.timeline_for_client_message(
        tenant_id="tenant-a",
        user_id="u1",
        client_message_id="client-context-fail",
    )
    assert timeline.turns[0]["user_message"]["content"] == "你好"
    assert timeline.turns[0]["attempts"][0]["status"] == "failed"
    run = audit.runs_for_conversation(
        conversation_id=timeline.conversation["id"],
        tenant_id="tenant-a",
        user_id="u1",
    )[0]
    assert run["status"] == "failed"


def test_delegation_failure_preserves_input_and_fails_attempt(tmp_path) -> None:
    service, gateway, _, _, _, projection = _projection_service(tmp_path)

    def fail_delegation(request):
        raise RuntimeError("child execution failed")

    gateway.handle_delegated = fail_delegation
    request = _prepared_request(
        projection,
        message="@招聘 分析候选人",
        client_message_id="client-delegation-fail",
    )

    with pytest.raises(RuntimeError, match="child execution failed"):
        service.handle(request)

    attempt = _timeline(projection, request.context["conversation_id"]).turns[0]["attempts"][0]
    assert attempt["status"] == "failed"


def test_general_llm_failure_preserves_input_and_fails_attempt(tmp_path) -> None:
    service, _, _, invoker, _, projection = _projection_service(tmp_path)

    def fail_answer(_request):
        raise RuntimeError("llm down")

    invoker.invoke_streaming = fail_answer
    request = _prepared_request(
        projection,
        message="解释一下",
        client_message_id="client-llm-fail",
    )

    with pytest.raises(RuntimeError, match="llm down"):
        service.handle(request)

    turn = _timeline(projection, request.context["conversation_id"]).turns[0]
    assert turn["user_message"]["content"] == "解释一下"
    assert turn["attempts"][0]["status"] == "failed"


def test_child_failed_status_projects_terminal_failure(tmp_path) -> None:
    service, _, _, _, _, projection = _projection_service(
        tmp_path,
        child_status="failed",
        child_output={"reason": "tool failed"},
    )
    request = _prepared_request(
        projection,
        message="@招聘 分析候选人",
        client_message_id="client-tool-fail",
    )

    response = service.handle(request)

    attempt = _timeline(projection, response.conversation_id).turns[0]["attempts"][0]
    assert response.status == "failed"
    assert attempt["status"] == "failed"
    assert attempt["messages"][0]["content"]


def test_waiting_approval_is_durable_before_response(tmp_path) -> None:
    service, _, _, _, _, projection = _projection_service(
        tmp_path,
        child_status="waiting_for_approval",
        child_output={
            "approval": {
                "skills": ["candidate.rank"],
                "preview": {"title": "候选人审批预览"},
            }
        },
    )
    request = _prepared_request(
        projection,
        message="@招聘 发布录用通知",
        client_message_id="client-approval",
    )

    response = service.handle(request)

    attempt = _timeline(projection, response.conversation_id).turns[0]["attempts"][0]
    action = attempt["actions"][0]
    assert response.status == "waiting_for_approval"
    assert attempt["status"] == "waiting_for_approval"
    assert action["status"] == "pending"
    assert action["preview"]["title"] == "候选人审批预览"
    assert action["preview"]["content"]


def test_approval_projection_failure_preserves_input_and_fails_attempt(tmp_path) -> None:
    service, _, _, _, _, projection = _projection_service(
        tmp_path,
        child_status="waiting_for_approval",
        child_output={"approval": {"skills": ["candidate.rank"], "preview": {}}},
    )
    request = _prepared_request(
        projection,
        message="@招聘 发布录用通知",
        client_message_id="client-approval-fail",
    )

    def fail_approval(**kwargs):
        raise RuntimeError("approval store down")

    projection.request_approval = fail_approval

    with pytest.raises(RuntimeError, match="approval store down"):
        service.handle(request)

    turn = _timeline(projection, request.context["conversation_id"]).turns[0]
    assert turn["user_message"]["content"] == "@招聘 发布录用通知"
    assert turn["attempts"][0]["status"] == "failed"


def test_resume_projects_final_output_without_second_user_message(tmp_path) -> None:
    service, _, _, _, _, projection = _projection_service(
        tmp_path,
        child_status="waiting_for_approval",
        child_output={
            "approval": {
                "skills": ["candidate.rank"],
                "preview": {"title": "候选人审批预览"},
            }
        },
    )
    request = _prepared_request(
        projection,
        message="@招聘 发布录用通知",
        client_message_id="client-resume",
    )
    waiting = service.handle(request)

    resumed = service.resume(
        waiting.thread_id,
        user_id="u1",
        roles=["recruiter"],
        approved_skills=["candidate.rank"],
    )

    timeline = _timeline(projection, resumed.conversation_id)
    attempt = timeline.turns[0]["attempts"][0]
    assert resumed.status == "completed"
    assert len(timeline.turns) == 1
    assert attempt["status"] == "succeeded"
    assert attempt["messages"][-1]["content"] == "审批后已执行"
    assert attempt["actions"][0]["status"] == "completed"


def test_resume_failure_fails_existing_attempt(tmp_path) -> None:
    service, gateway, audit, _, _, projection = _projection_service(
        tmp_path,
        child_status="waiting_for_approval",
        child_output={"approval": {"skills": ["candidate.rank"], "preview": {}}},
    )
    request = _prepared_request(
        projection,
        message="@招聘 发布录用通知",
        client_message_id="client-resume-fail",
    )
    waiting = service.handle(request)

    def fail_resume(thread_id, **kwargs):
        raise RuntimeError("child resume failed")

    gateway.resume = fail_resume

    with pytest.raises(RuntimeError, match="child resume failed"):
        service.resume(
            waiting.thread_id,
            user_id="u1",
            roles=["recruiter"],
            approved_skills=["candidate.rank"],
        )

    attempt = _timeline(projection, waiting.conversation_id).turns[0]["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["actions"][0]["status"] == "invalidated"
    assert audit.get_run(waiting.run_id)["status"] == "failed"


def test_bind_conflict_preserves_original_attempt_and_exception(tmp_path) -> None:
    service, _, audit, _, _, projection = _projection_service(tmp_path)
    request = _prepared_request(
        projection,
        message="你好",
        client_message_id="client-bind-conflict",
    )
    projection.bind_run(
        request.context["conversation_attempt_id"],
        run_id="first-run",
        agent_id="general_agent",
    )

    with pytest.raises(ValueError, match="already bound"):
        service.handle(request)

    attempt = _timeline(projection, request.context["conversation_id"]).turns[0]["attempts"][0]
    assert attempt["status"] == "running"
    assert attempt["run_id"] == "first-run"
    duplicate_run = next(
        run
        for run in audit.runs_for_conversation(
            conversation_id=request.context["conversation_id"],
            tenant_id="tenant-a",
            user_id="u1",
        )
        if run["run_id"] != "first-run"
    )
    assert duplicate_run["status"] == "failed"


def test_blocked_general_result_is_projected(tmp_path) -> None:
    service, _, _, _, _, projection = _projection_service(
        tmp_path,
        child_status="blocked",
    )
    request = _prepared_request(
        projection,
        message="@招聘 审核这份内容",
        client_message_id="client-blocked",
    )

    response = service.handle(request)

    attempt = _timeline(projection, response.conversation_id).turns[0]["attempts"][0]
    assert response.status == "blocked"
    assert attempt["status"] == "rejected"
    assert attempt["messages"][0]["content"]
