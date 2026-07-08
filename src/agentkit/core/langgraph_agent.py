"""三个业务 Agent 共用的唯一 LangGraph Runtime。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agentkit.runtime.conversation_context import (
    AgentConversationContext,
    ConversationContextService,
)
from agentkit.runtime.conversation_persistence import ConversationPersistenceService

from .artifacts import ArtifactStore, InMemoryArtifactStore
from .audit import InMemoryAuditLog, PostgresAuditLog, SQLiteAuditLog
from .context.errors import ContextHashMismatchError
from .contracts import AgentProfile, IntentFrame, SkillDefinition, TaskRequest, TaskResponse
from .execution.models import (
    CapabilityResolution,
    StrategyRequest,
    StrategyResult,
    ToolPolicy,
    ToolProvider,
    ToolRisk,
)
from .execution.protocol import ExecutionContext
from .execution.registry import StrategyRegistry
from .execution.selector import StrategySelection, StrategySelector
from .idempotency import IdempotencyStore
from .langgraph_runtime import invoke_graph_v2
from .registry import AgentRegistry, SkillRegistry, ToolRegistry
from .router import CapabilityResolutionError, IntentRouter
from .schema_input_resolver import InputResolution
from .tool_backends import PythonToolBackend, ToolBackendRegistry
from .tool_executor import ToolExecutor

AuditLog = InMemoryAuditLog | SQLiteAuditLog | PostgresAuditLog


class IntentResolver(Protocol):
    def __call__(
        self,
        request: TaskRequest,
        *,
        agent: AgentProfile,
        run_id: str,
    ) -> IntentFrame: ...


class InputResolver(Protocol):
    def resolve(
        self,
        request: TaskRequest,
        *,
        agent: AgentProfile,
        skill: SkillDefinition,
        arguments: dict[str, Any],
        run_id: str,
    ) -> InputResolution: ...


class UnifiedAgentState(TypedDict, total=False):
    request: TaskRequest
    thread_id: str
    run_id: str
    context_manifest_hash: str
    agent: AgentProfile
    conversation: AgentConversationContext | None
    intent: IntentFrame
    resolution: CapabilityResolution
    arguments: dict[str, Any]
    selection: StrategySelection
    approval_required: list[str]
    result: StrategyResult


class _Audit(Protocol):
    def start_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        text: str,
        agent_id: str | None = None,
        parent_run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str: ...

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...

    def events_for(self, run_id: str) -> list[dict[str, Any]]: ...


class UnifiedAgentGraph:
    """统一入口、统一上下文、统一策略与统一持久化的治理图。"""

    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_selector: str,
        tenant_config: dict[str, Any],
        agents: AgentRegistry,
        skills: SkillRegistry,
        tools: ToolRegistry,
        audit: _Audit,
        context_invoker: Any,
        router: IntentRouter,
        selector: StrategySelector,
        strategies: StrategyRegistry,
        intent_resolver: IntentResolver,
        input_resolver: InputResolver,
        checkpointer: Any,
        conversation_context: ConversationContextService | None = None,
        conversation_persistence: ConversationPersistenceService | None = None,
        artifact_store_factory: Callable[[str], ArtifactStore] | None = None,
        tool_backends: ToolBackendRegistry | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_selector = tenant_selector
        self._tenant_config = tenant_config
        self._agents = agents
        self._skills = skills
        self._tools = tools
        self._audit = audit
        self._context_invoker = context_invoker
        self._router = router
        self._selector = selector
        self._strategies = strategies
        self._intent_resolver = intent_resolver
        self._input_resolver = input_resolver
        self._checkpointer = checkpointer
        self._conversation_context = conversation_context
        self._conversation_persistence = conversation_persistence
        self._artifact_store_factory = artifact_store_factory or (
            lambda run_id: InMemoryArtifactStore()
        )
        self._tool_backends = tool_backends or ToolBackendRegistry(
            {tool.provider: PythonToolBackend() for tool in tools.all() if tool.handler}
        )
        if not tools.all():
            self._tool_backends = tool_backends or ToolBackendRegistry(
                {ToolProvider.PYTHON: PythonToolBackend()}
            )
        self._idempotency_store = idempotency_store
        self._graph = self._build_graph()

    def run(self, request: TaskRequest, *, thread_id: str | None = None) -> TaskResponse:
        thread = thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread}}
        try:
            invoke_graph_v2(
                self._graph,
                {"request": request, "thread_id": thread},
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - 未处理异常必须收口为可审计终态
            return self._failure_response(thread, config, exc)
        return self._response_from_state(thread, config)

    def resume(
        self,
        thread_id: str,
        *,
        approved_skills: list[str] | tuple[str, ...] = (),
        rejected_skills: list[str] | tuple[str, ...] = (),
        decision_context: dict[str, Any] | None = None,
    ) -> TaskResponse:
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = self._graph.get_state(config)
        if not snapshot.values or "request" not in snapshot.values:
            raise KeyError(f"未知或已过期 thread_id: {thread_id}")
        if not snapshot.next:
            raise RuntimeError(f"thread_id 当前未等待审批: {thread_id}")
        original_context_hash = str(snapshot.values.get("context_manifest_hash") or "")
        if original_context_hash != self._context_invoker.manifest_hash:
            run_id = str(snapshot.values.get("run_id") or "")
            self._audit.record(
                run_id,
                "context_hash_mismatch",
                {
                    "expected": original_context_hash,
                    "actual": self._context_invoker.manifest_hash,
                },
            )
            raise ContextHashMismatchError("审批恢复时 Context Manifest 已改变，请重新发起任务")
        approved = {str(item) for item in approved_skills}
        rejected = {str(item) for item in rejected_skills}
        if not approved and not rejected:
            raise RuntimeError("必须提供 approved_skills 或 rejected_skills")
        if approved & rejected:
            raise RuntimeError("同一 Skill 不能同时批准和拒绝")
        pending = set(snapshot.values.get("approval_required", []))
        unknown = (approved | rejected) - pending
        if unknown:
            raise RuntimeError(f"审批包含非待处理 Skill: {', '.join(sorted(unknown))}")
        request = cast(TaskRequest, snapshot.values["request"])
        request_context = dict(request.context)
        request_context["approved_skills"] = sorted(approved)
        request_context["rejected_skills"] = sorted(rejected)
        if decision_context:
            request_context["approval_decision"] = dict(decision_context)
        resumed_request = replace(request, context=request_context)
        run_id = str(snapshot.values["run_id"])
        self._audit.record(
            run_id,
            "run_resumed",
            {
                "thread_id": thread_id,
                "approved_skills": sorted(approved),
                "rejected_skills": sorted(rejected),
            },
        )
        self._graph.update_state(config, {"request": resumed_request})
        try:
            invoke_graph_v2(self._graph, Command(resume=True), config=config)
        except Exception as exc:  # noqa: BLE001 - 恢复执行也必须形成终态
            return self._failure_response(thread_id, config, exc)
        return self._response_from_state(thread_id, config)

    def _failure_response(
        self,
        thread_id: str,
        config: dict[str, Any],
        error: Exception,
    ) -> TaskResponse:
        snapshot = self._graph.get_state(config)
        values = cast(dict[str, Any], snapshot.values)
        run_id = str(values.get("run_id") or "")
        if not run_id:
            raise error
        error_code = str(getattr(error, "code", "runtime_error"))
        self._audit.record(
            run_id,
            "run_failed",
            {
                "error_code": error_code,
                "error_type": type(error).__name__,
                "reason": str(error),
            },
        )
        self._audit.record(run_id, "run_finished", {"status": "failed"})
        request = cast(TaskRequest, values["request"])
        agent = cast(AgentProfile | None, values.get("agent"))
        selection = cast(StrategySelection | None, values.get("selection"))
        return TaskResponse(
            status="failed",
            output={
                "message": "任务执行失败，请在运行追踪中查看详细原因。",
                "error_code": error_code,
            },
            run_id=run_id,
            thread_id=thread_id,
            agent=agent.name if agent else str(request.context.get("agent") or ""),
            strategy=selection.strategy.value if selection else "",
            conversation_id=str(request.context.get("conversation_id") or ""),
            governance={"error_code": error_code},
            audit_events=self._audit.events_for(run_id),
        )

    def _build_graph(self):
        graph = StateGraph(UnifiedAgentState)
        graph.add_node("start_run", self._start_run)
        graph.add_node("load_agent", self._load_agent)
        graph.add_node("build_context", self._build_context)
        graph.add_node("understand_request", self._understand_request)
        graph.add_node("resolve_capability", self._resolve_capability)
        graph.add_node("resolve_inputs", self._resolve_inputs)
        graph.add_node("select_strategy", self._select_strategy)
        graph.add_node("review_strategy", self._review_strategy)
        graph.add_node("human_approval", self._human_approval)
        graph.add_node("execute_strategy", self._execute_strategy)
        graph.add_node("post_execution_approval", self._post_execution_approval)
        graph.add_node("deferred_approval", self._deferred_approval)
        graph.add_node("review_output", self._review_output)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "start_run")
        graph.add_edge("start_run", "load_agent")
        graph.add_edge("load_agent", "build_context")
        graph.add_edge("build_context", "understand_request")
        graph.add_edge("understand_request", "resolve_capability")
        graph.add_edge("resolve_capability", "resolve_inputs")
        graph.add_edge("resolve_inputs", "select_strategy")
        graph.add_edge("select_strategy", "review_strategy")
        graph.add_edge("review_strategy", "human_approval")
        graph.add_edge("human_approval", "execute_strategy")
        graph.add_edge("execute_strategy", "post_execution_approval")
        graph.add_edge("post_execution_approval", "deferred_approval")
        graph.add_edge("deferred_approval", "review_output")
        graph.add_edge("review_output", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self._checkpointer)

    def _start_run(self, state: UnifiedAgentState) -> dict[str, Any]:
        request = state["request"]
        run_id = self._audit.start_run(
            tenant_id=self._tenant_id,
            user_id=request.user_id,
            text=request.text,
            agent_id=str(request.context.get("agent") or "") or None,
            parent_run_id=str(request.context.get("parent_run_id") or "") or None,
            conversation_id=(
                str(
                    request.context.get("trace_conversation_id")
                    or request.context.get("conversation_id")
                    or ""
                )
                or None
            ),
        )
        return {
            "run_id": run_id,
            "context_manifest_hash": self._context_invoker.manifest_hash,
        }

    def _load_agent(self, state: UnifiedAgentState) -> dict[str, Any]:
        request = state["request"]
        agent_id = str(request.context.get("agent") or "")
        try:
            agent = self._agents.get(agent_id)
        except KeyError:
            return {
                "result": StrategyResult(
                    status="capability_denied",
                    output={"reason": f"未知 Agent: {agent_id}"},
                )
            }
        self._audit.record(state["run_id"], "agent_loaded", {"agent": agent.name})
        return {"agent": agent}

    def _build_context(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state or self._conversation_context is None:
            return {"conversation": None}
        request = state["request"]
        conversation_id = str(request.context.get("conversation_id") or "")
        if not conversation_id:
            return {"conversation": None}
        context = self._conversation_context.build(
            agent=state["agent"],
            tenant_id=self._tenant_id,
            agent_id=state["agent"].name,
            user_id=request.user_id,
            conversation_id=conversation_id,
            run_id=state["run_id"],
            message=request.text,
            roles=request.roles,
        )
        self._audit.record(state["run_id"], "context_built", {"conversation_id": conversation_id})
        enriched_request = replace(
            request,
            context={
                **request.context,
                "agent_context": {
                    "summary": context.summary,
                    "recent_messages": list(context.recent_messages),
                    "memories": list(context.memories),
                    "knowledge": list(context.knowledge),
                },
            },
        )
        return {"conversation": context, "request": enriched_request}

    def _understand_request(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state:
            return {}
        intent = self._intent_resolver(
            state["request"],
            agent=state["agent"],
            run_id=state["run_id"],
        )
        self._audit.record(
            state["run_id"], "intent_understood", {"intent_type": intent.intent_type}
        )
        return {"intent": intent}

    def _resolve_capability(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state:
            return {}
        try:
            resolution = self._router.resolve(
                state["request"],
                intent=state["intent"],
                run_id=state["run_id"],
            )
        except CapabilityResolutionError as exc:
            return {
                "result": StrategyResult(status="capability_denied", output={"reason": str(exc)})
            }
        self._audit.record(
            state["run_id"],
            "capability_resolved",
            {"skills": list(resolution.candidate_skills)},
        )
        return {"resolution": resolution}

    def _resolve_inputs(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state:
            return {}
        resolution = state["resolution"]
        if resolution.response_mode == "answer" or resolution.primary_skill is None:
            return {"arguments": dict(state["request"].context.get("plan_args", {}))}
        skill = self._skills.get(resolution.primary_skill)
        request_context = state["request"].context
        intent_entities = state["intent"].entities
        explicit = request_context.get("skill_args")
        arguments = dict(explicit) if isinstance(explicit, dict) else {}
        properties = skill.input_schema.get("properties", {})
        if isinstance(properties, dict):
            for name in properties:
                if name in arguments:
                    continue
                if name in request_context:
                    arguments[name] = request_context[name]
                elif name in intent_entities:
                    arguments[name] = intent_entities[name]
        initial_fields = set(arguments)
        input_resolution = self._input_resolver.resolve(
            state["request"],
            agent=state["agent"],
            skill=skill,
            arguments=arguments,
            run_id=state["run_id"],
        )
        self._audit.record(
            state["run_id"],
            "inputs_resolved",
            {
                "skill": skill.name,
                "missing_fields": list(input_resolution.missing),
                "resolved_fields": sorted(set(input_resolution.arguments) - initial_fields),
                "confidence": input_resolution.confidence,
                "llm_used": input_resolution.llm_used,
            },
        )
        if input_resolution.missing:
            return {
                "result": StrategyResult(
                    status="needs_clarification",
                    output={
                        "missing_required": list(input_resolution.missing),
                        "clarification": input_resolution.clarification,
                    },
                )
            }
        return {"arguments": input_resolution.arguments}

    def _select_strategy(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state:
            return {}
        selection = self._selector.select(agent=state["agent"], resolution=state["resolution"])
        self._audit.record(
            state["run_id"],
            "strategy_selected",
            {"strategy": selection.strategy.value, "reason": selection.reason},
        )
        return {"selection": selection}

    def _review_strategy(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state:
            return {"approval_required": []}
        approved = set(state["request"].context.get("approved_skills", []))
        required = [
            skill_name
            for skill_name in state["resolution"].candidate_skills
            if self._skills.get(skill_name).execution.tool_policy is ToolPolicy.SIDE_EFFECT
            and skill_name not in approved
        ]
        return {"approval_required": required}

    def _human_approval(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state or not state.get("approval_required"):
            return {}
        request = state["request"]
        rejected = set(request.context.get("rejected_skills", []))
        if rejected:
            return {
                "result": StrategyResult(
                    status="rejected", output={"rejected_skills": sorted(rejected)}
                )
            }
        approved = set(request.context.get("approved_skills", []))
        if set(state["approval_required"]) <= approved:
            return {}
        interrupt(
            {
                "type": "approval_required",
                "skills": list(state["approval_required"]),
            }
        )
        return {}

    def _execute_strategy(self, state: UnifiedAgentState) -> dict[str, Any]:
        if "result" in state:
            return {}
        selection = state["selection"]
        request = state["request"]
        run_id = state["run_id"]
        artifacts = self._artifact_store_factory(run_id)
        granted = _granted_permissions(self._tenant_config, request.roles)
        candidate_skills = [self._skills.get(name) for name in state["resolution"].candidate_skills]
        allowed_tools = {tool_name for skill in candidate_skills for tool_name in skill.tools}
        approved_skills = set(request.context.get("approved_skills", []))
        approved_tools = {
            tool_name
            for skill in candidate_skills
            if skill.name in approved_skills
            for tool_name in skill.tools
            if self._tools.get(tool_name).risk is ToolRisk.SIDE_EFFECT
        }
        invoker = ToolExecutor(
            tenant_id=self._tenant_id,
            audit=self._audit,
            run_id=run_id,
            backends=self._tool_backends,
            permissions=granted,
            allowed_tools=allowed_tools,
            tool_policy=selection.tool_policy,
            approved_side_effects=approved_tools,
            idempotency_store=self._idempotency_store,
        )
        execution_context = ExecutionContext(
            tenant_id=self._tenant_id,
            tenant_selector=self._tenant_selector,
            run_id=run_id,
            agent=state["agent"],
            request=request,
            skills={skill.name: skill for skill in self._skills.all()},
            tools={tool.name: tool for tool in self._tools.all()},
            tenant_config=self._tenant_config,
            artifacts=artifacts,
            context_invoker=self._context_invoker,
            budget=selection.budget,
            invoker=invoker,
        )
        strategy = self._strategies.get(selection.strategy.value)
        result = strategy.execute(
            context=execution_context,
            request=StrategyRequest(
                goal=state["intent"].goal,
                arguments=state["arguments"],
                capability=state["resolution"],
            ),
        )
        self._audit.record(run_id, "strategy_finished", {"status": result.status})
        return {"result": result}

    def _post_execution_approval(self, state: UnifiedAgentState) -> dict[str, Any]:
        result = state.get("result")
        if result is None or result.status != "deferred_action":
            return {}
        skill_name = state["resolution"].primary_skill or ""
        return {"approval_required": [skill_name]}

    def _deferred_approval(self, state: UnifiedAgentState) -> dict[str, Any]:
        result = state.get("result")
        if result is None or result.status != "deferred_action":
            return {}
        action = result.output.get("deferred_action", {})
        raw_calls = action.get("tool_calls") if isinstance(action, dict) else None
        if isinstance(raw_calls, list) and raw_calls:
            calls = [item for item in raw_calls if isinstance(item, dict)]
        else:
            calls = [
                {
                    "tool_name": action.get("tool_name"),
                    "args": action.get("arguments", {}),
                    "result_key": "action_result",
                }
            ]
        tool_names = {str(item.get("tool_name") or "") for item in calls}
        tool_names.discard("")
        skill_name = state["resolution"].primary_skill or ""
        request = state["request"]
        approved = set(request.context.get("approved_skills", []))
        rejected = set(request.context.get("rejected_skills", []))
        if skill_name in rejected:
            return {"result": StrategyResult(status="rejected", output={})}
        if skill_name not in approved:
            interrupt(
                {
                    "type": "approval_required",
                    "skills": list(state["approval_required"]),
                }
            )
            return {}
        invoker = ToolExecutor(
            tenant_id=self._tenant_id,
            audit=self._audit,
            run_id=state["run_id"],
            backends=self._tool_backends,
            permissions=_granted_permissions(self._tenant_config, request.roles),
            allowed_tools=tool_names,
            tool_policy=ToolPolicy.SIDE_EFFECT,
            approved_side_effects=tool_names,
            idempotency_store=self._idempotency_store,
        )
        completed_output = dict(result.output)
        completed_output.pop("deferred_action", None)
        for item in calls:
            tool_name = str(item.get("tool_name") or "")
            output = invoker.call(
                self._tools.get(tool_name),
                dict(item.get("args") or item.get("arguments") or {}),
            )
            completed_output[str(item.get("result_key") or tool_name)] = output
        return {"result": StrategyResult(status="completed", output=completed_output)}

    def _review_output(self, state: UnifiedAgentState) -> dict[str, Any]:
        result = state.get("result")
        if result is not None:
            self._audit.record(state["run_id"], "output_reviewed", {"status": result.status})
        return {}

    def _finalize(self, state: UnifiedAgentState) -> dict[str, Any]:
        result = state.get("result") or StrategyResult(
            status="failed", output={"reason": "Runtime 未产生结果"}
        )
        self._audit.record(state["run_id"], "run_finished", {"status": result.status})
        return {"result": result}

    def _response_from_state(self, thread_id: str, config: dict[str, Any]) -> TaskResponse:
        snapshot = self._graph.get_state(config)
        values = cast(dict[str, Any], snapshot.values)
        run_id = str(values.get("run_id", ""))
        agent = cast(AgentProfile | None, values.get("agent"))
        selection = cast(StrategySelection | None, values.get("selection"))
        conversation_id = str(values["request"].context.get("conversation_id") or "")
        if snapshot.next:
            self._audit.record(
                run_id,
                "run_paused",
                {"status": "waiting_for_approval", "thread_id": thread_id},
            )
            pending_result = cast(StrategyResult | None, values.get("result"))
            approval = {"skills": values.get("approval_required", [])}
            if pending_result is not None and pending_result.status == "deferred_action":
                action = pending_result.output.get("deferred_action", {})
                if isinstance(action, dict) and isinstance(action.get("preview"), dict):
                    preview = action["preview"]
                else:
                    arguments = action.get("arguments", {}) if isinstance(action, dict) else {}
                    preview = arguments.get("package", {}) if isinstance(arguments, dict) else {}
                approval.update({"phase": "post_execution", "preview": preview})
            result = StrategyResult(
                status="waiting_for_approval",
                output={"approval": approval},
            )
        else:
            result = cast(StrategyResult, values.get("result"))
        approval_governance = (
            result.output.get("approval", {})
            if snapshot.next
            else {"skills": values.get("approval_required", [])}
        )
        governance = {
            "strategy": selection.strategy.value if selection else "",
            "allowed_skills": list(agent.allowed_skills) if agent else [],
            "approval": approval_governance,
        }
        return TaskResponse(
            status=result.status,
            output=result.output,
            run_id=run_id,
            thread_id=thread_id,
            agent=agent.name if agent else "",
            strategy=selection.strategy.value if selection else "",
            conversation_id=conversation_id,
            governance=governance,
            audit_events=self._audit.events_for(run_id),
        )


def _granted_permissions(tenant_config: dict[str, Any], roles: list[str]) -> set[str]:
    role_permissions = tenant_config.get("role_permissions", {})
    granted: set[str] = set()
    for role in roles:
        granted.update(role_permissions.get(role, []))
    return granted


__all__ = ["UnifiedAgentGraph", "UnifiedAgentState"]
