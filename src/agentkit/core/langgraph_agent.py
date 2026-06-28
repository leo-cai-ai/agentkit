"""LangGraph implementation of the enterprise agent runtime.

The graph is deliberately business-agnostic:

    start_run -> prepare_context -> understand_intent -> route -> plan
      -> review_plan -> human_approval -> execute -> review_output -> finalize

Business behavior is provided by registered skills and tools. Persistence is
provided by the audit implementation passed into the graph.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.errors import NodeInterrupt
from langgraph.graph import END, START, StateGraph

from .audit import InMemoryAuditLog, SQLiteAuditLog
from .contracts import IntentFrame, RouteDecision, TaskPlan, TaskRequest, TaskResponse
from .executor import PlanExecutor
from .governance import HumanApprovalGate, OutputReviewer, PlanReviewer
from .hooks import AgentLifecycleHooks
from .intent import IntentDecomposer
from .log_context import bind_run_id, set_run_id
from .logging_config import get_logger
from .metrics import timed_event
from .planner import Planner
from .router import IntentRouter

_log = get_logger("agentkit.graph")


class EnterpriseAgentState(TypedDict, total=False):
    request: TaskRequest
    run_id: str
    runtime_context: dict[str, Any]
    intent: IntentFrame
    route: RouteDecision
    plan: TaskPlan
    plan_review: dict[str, Any]
    approval: dict[str, Any]
    output: dict[str, Any]
    output_review: dict[str, Any]
    response: TaskResponse
    fastpath_active: bool
    combined_route_active: bool


AuditLog = InMemoryAuditLog | SQLiteAuditLog


class EnterpriseAgentGraph:
    """Compiled LangGraph agent for one enterprise tenant."""

    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_config: dict[str, Any],
        intent_decomposer: IntentDecomposer,
        router: IntentRouter,
        planner: Planner,
        executor: PlanExecutor,
        audit: AuditLog,
        plan_reviewer: PlanReviewer,
        approval_gate: HumanApprovalGate,
        output_reviewer: OutputReviewer,
        hooks: AgentLifecycleHooks | None = None,
        checkpointer: Any = None,
        fastpath: bool = False,
        combiner: Any = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_config = tenant_config
        self._fastpath = fastpath
        self._combiner = combiner
        self._intent_decomposer = intent_decomposer
        self._router = router
        self._planner = planner
        self._executor = executor
        self._audit = audit
        self._plan_reviewer = plan_reviewer
        self._approval_gate = approval_gate
        self._output_reviewer = output_reviewer
        self._hooks = hooks or AgentLifecycleHooks()
        self._checkpointer = checkpointer
        self._graph = self._build_graph()

    def invoke(self, request: TaskRequest) -> TaskResponse:
        # Stateless invocation: allocate a throwaway thread id.
        return self.run(request, thread_id=str(uuid.uuid4()))

    def run(self, request: TaskRequest, *, thread_id: str) -> TaskResponse:
        if self._checkpointer is None:
            final_state = self._graph.invoke({"request": request})
            return final_state["response"]
        config = {"configurable": {"thread_id": thread_id}}
        self._graph.invoke({"request": request}, config)
        return self._response_from_thread(thread_id, config)

    def resume(
        self,
        thread_id: str,
        *,
        approved_skills: list[str] | tuple[str, ...] = (),
        rejected_skills: list[str] | tuple[str, ...] = (),
        decision_context: dict[str, Any] | None = None,
    ) -> TaskResponse:
        if self._checkpointer is None:
            raise RuntimeError("resume requires an approval checkpointer (set it to 'memory').")
        config = {"configurable": {"thread_id": thread_id}}
        state = self._graph.get_state(config)
        if not state.values or "request" not in state.values:
            raise KeyError(f"unknown or expired thread_id: {thread_id}")
        run_id = str(state.values.get("run_id", "-"))
        self._validate_resume_decision(
            state=state,
            run_id=run_id,
            thread_id=thread_id,
            approved_skills=approved_skills,
            rejected_skills=rejected_skills,
        )
        request: TaskRequest = state.values["request"]
        context = dict(request.context)
        if approved_skills:
            context["approved_skills"] = list(approved_skills)
        if rejected_skills:
            context["rejected_skills"] = list(rejected_skills)
        if decision_context:
            context["approval_decision"] = dict(decision_context)
        resumed_request = TaskRequest(
            user_id=request.user_id,
            roles=request.roles,
            text=request.text,
            context=context,
        )
        # Re-bind the original run id so resume logs stay correlated, then inject
        # the human decision and continue from the paused human_approval node.
        # bind_run_id resets on exit so the run id never leaks past this call.
        with bind_run_id(run_id):
            self._audit.record(
                run_id,
                "run_resumed",
                {
                    "thread_id": thread_id,
                    "approved_skills": list(approved_skills),
                    "rejected_skills": list(rejected_skills),
                    "decision_context": decision_context or {},
                },
            )
            self._graph.update_state(config, {"request": resumed_request})
            self._graph.invoke(None, config)
        return self._response_from_thread(thread_id, config)

    def _validate_resume_decision(
        self,
        *,
        state: Any,
        run_id: str,
        thread_id: str,
        approved_skills: list[str] | tuple[str, ...],
        rejected_skills: list[str] | tuple[str, ...],
    ) -> None:
        if not state.next:
            raise RuntimeError(f"thread_id is not waiting for approval: {thread_id}")
        approved = {str(skill) for skill in approved_skills}
        rejected = {str(skill) for skill in rejected_skills}
        if not approved and not rejected:
            raise RuntimeError("approved_skills or rejected_skills is required.")
        overlap = approved & rejected
        if overlap:
            raise RuntimeError(
                "skills cannot be both approved and rejected: "
                + ", ".join(sorted(overlap))
            )

        approval = self._last_approval(run_id)
        if approval.get("status") != "waiting_for_approval":
            raise RuntimeError(f"thread_id is not waiting for approval: {thread_id}")
        pending = {str(skill) for skill in approval.get("skills", [])}
        if not pending:
            raise RuntimeError("pending approval has no skills to decide.")
        unknown = (approved | rejected) - pending
        if unknown:
            raise RuntimeError(
                "approval decision contains skills that are not pending: "
                + ", ".join(sorted(unknown))
            )

    def _response_from_thread(self, thread_id: str, config: dict[str, Any]) -> TaskResponse:
        state = self._graph.get_state(config)
        # A non-empty `.next` means the graph is paused (interrupted) before
        # finalize ran, i.e. it is waiting for human approval.
        if state.next:
            return self._build_waiting_response(state.values, thread_id)
        return state.values["response"]

    def _build_waiting_response(self, values: dict[str, Any], thread_id: str) -> TaskResponse:
        run_id = values["run_id"]
        approval = self._last_approval(run_id)
        output = {
            "status": "waiting_for_approval",
            "approval": approval,
            "thread_id": thread_id,
            "final": {
                "message": "This run is waiting for human approval before execution.",
                "approval": approval,
            },
            "governance": {
                "runtime_context": values.get("runtime_context", {}),
                "intent": asdict(values["intent"]) if "intent" in values else {},
                "plan_review": values.get("plan_review", {}),
                "approval": approval,
                "output_review": {},
            },
        }
        self._audit.record(
            run_id,
            "run_paused",
            {"status": "waiting_for_approval", "thread_id": thread_id},
        )
        return TaskResponse(
            output=output,
            plan=values["plan"],
            audit_events=self._audit.events_for(run_id),
        )

    def _last_approval(self, run_id: str) -> dict[str, Any]:
        for event in reversed(self._audit.events_for(run_id)):
            if event.get("type") == "human_approval_checked":
                payload = event.get("payload")
                return payload if isinstance(payload, dict) else {}
        return {}

    def _build_graph(self):
        graph = StateGraph(EnterpriseAgentState)
        graph.add_node("start_run", self._start_run_node)
        graph.add_node("prepare_context", self._prepare_context_node)
        graph.add_node("understand_intent", self._understand_intent_node)
        graph.add_node("route_request", self._route_node)
        graph.add_node("plan_step", self._plan_node)
        graph.add_node("review_plan", self._review_plan_node)
        graph.add_node("human_approval", self._human_approval_node)
        graph.add_node("execute", self._execute_node)
        graph.add_node("review_output", self._review_output_node)
        graph.add_node("finalize", self._finalize_node)

        graph.add_edge(START, "start_run")
        graph.add_edge("start_run", "prepare_context")
        graph.add_edge("prepare_context", "understand_intent")
        graph.add_edge("understand_intent", "route_request")
        graph.add_edge("route_request", "plan_step")
        graph.add_edge("plan_step", "review_plan")
        graph.add_edge("review_plan", "human_approval")
        graph.add_conditional_edges(
            "human_approval",
            self._next_after_approval,
            {
                "execute": "execute",
                "review_output": "review_output",
            },
        )
        graph.add_edge("execute", "review_output")
        graph.add_edge("review_output", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self._checkpointer)

    def _start_run_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        request = state["request"]
        run_id = self._audit.start_run(
            tenant_id=self._tenant_id,
            user_id=request.user_id,
            text=request.text,
        )
        # Bind the run id so every log record during this run is correlated.
        # LangGraph runs this graph's nodes synchronously on one thread, so the
        # context variable stays set for the remainder of the run.
        set_run_id(run_id)
        self._audit.record(run_id, "graph_node_finished", {"node": "start_run"})
        self._hooks.on_run_started(run_id=run_id, request=request)
        _log.info("run started", extra={"run_id": run_id})
        return {"run_id": run_id}

    def _prepare_context_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        runtime_context = {
            "tenant_id": self._tenant_id,
            "selected_agent": state["request"].context.get("agent", ""),
            "roles": state["request"].roles,
            "context_keys": sorted(state["request"].context.keys()),
            "runtime_manifest": self._tenant_config.get("runtime_manifest", {}),
        }
        self._audit.record(run_id, "context_prepared", runtime_context)
        self._audit.record(run_id, "graph_node_finished", {"node": "prepare_context"})
        return {"runtime_context": runtime_context}

    def _understand_intent_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        request = state["request"]

        # Deterministic fast-path: if the rule-based router can resolve a skill
        # with HIGH confidence, skip the advisory governance LLM calls
        # (intent/route/plan/plan_review) entirely. Otherwise fall through to the
        # full LLM pipeline so ambiguous requests keep their governance.
        if self._fastpath:
            with timed_event(self._audit, run_id, "node_timing", node="understand_intent"):
                det_intent = self._intent_decomposer.deterministic_intent(request)
                preview_route = self._router.deterministic_route(request, intent=det_intent)
            if preview_route.confidence == "high" and preview_route.skill_name:
                self._audit.record(run_id, "intent_understood", asdict(det_intent))
                self._audit.record(run_id, "fastpath_engaged", {"skill": preview_route.skill_name})
                self._audit.record(run_id, "graph_node_finished", {"node": "understand_intent"})
                return {
                    "intent": det_intent,
                    "route": preview_route,
                    "fastpath_active": True,
                }

        # Combined lane: resolve intent + route in one LLM call. The route node
        # then only validates the suggestion (no second round trip).
        if self._combiner is not None:
            with timed_event(self._audit, run_id, "node_timing", node="understand_intent"):
                intent, route = self._combiner.resolve(request)
            self._audit.record(run_id, "intent_understood", asdict(intent))
            self._audit.record(run_id, "llm_node_completed", {"node": "understand_intent"})
            self._audit.record(run_id, "combined_intent_route", {"skill": route.skill_name})
            self._audit.record(run_id, "graph_node_finished", {"node": "understand_intent"})
            return {
                "intent": intent,
                "route": route,
                "fastpath_active": False,
                "combined_route_active": True,
            }

        with timed_event(self._audit, run_id, "node_timing", node="understand_intent"):
            intent = self._intent_decomposer.decompose(request)
        self._audit.record(run_id, "intent_understood", asdict(intent))
        self._audit.record(run_id, "llm_node_completed", {"node": "understand_intent"})
        self._audit.record(run_id, "graph_node_finished", {"node": "understand_intent"})
        return {"intent": intent, "fastpath_active": False, "combined_route_active": False}

    def _route_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        self._hooks.before_route(run_id=run_id, request=state["request"])
        # Fast-path resolves route deterministically; the combined lane resolves
        # it in the same LLM call as intent. Either way the route is already in
        # state and this node only records it (no separate LLM round trip).
        if state.get("fastpath_active") or state.get("combined_route_active"):
            route = state["route"]
            self._audit.record(
                run_id,
                "route_selected",
                {
                    "skill": route.skill_name,
                    "reason": route.reason,
                    "confidence": route.confidence,
                    "intent_type": state["intent"].intent_type,
                    "intent_target": state["intent"].target,
                },
            )
            self._audit.record(run_id, "graph_node_finished", {"node": "route"})
            self._hooks.after_route(run_id=run_id, request=state["request"], route=route)
            return {"route": route}
        with timed_event(self._audit, run_id, "node_timing", node="route"):
            route = self._router.route(state["request"], intent=state["intent"])
        self._audit.record(
            run_id,
            "route_selected",
            {
                "skill": route.skill_name,
                "reason": route.reason,
                "confidence": route.confidence,
                "intent_type": state["intent"].intent_type,
                "intent_target": state["intent"].target,
            },
        )
        # The audit "node" label intentionally keeps the original name "route"
        # for audit-output stability, even though the graph node id is now
        # "route_request". Do not change these audit string values.
        self._audit.record(run_id, "llm_node_completed", {"node": "route"})
        self._audit.record(run_id, "graph_node_finished", {"node": "route"})
        self._hooks.after_route(run_id=run_id, request=state["request"], route=route)
        return {"route": route}

    def _plan_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        if state.get("fastpath_active"):
            with timed_event(self._audit, run_id, "node_timing", node="plan"):
                plan = self._planner.deterministic_plan(
                    request=state["request"],
                    route=state["route"],
                )
        else:
            with timed_event(self._audit, run_id, "node_timing", node="plan"):
                plan = self._planner.make_plan(
                    request=state["request"],
                    route=state["route"],
                    intent=state["intent"],
                )
        self._audit.record(
            run_id,
            "plan_created",
            {
                "steps": [
                    {"step_id": step.step_id, "skill": step.skill_name, "mode": step.mode}
                    for step in plan.steps
                ],
                "warnings": plan.warnings,
            },
        )
        # The audit "node" label intentionally keeps the original name "plan"
        # for audit-output stability, even though the graph node id is now
        # "plan_step". Do not change these audit string values.
        if not state.get("fastpath_active"):
            self._audit.record(run_id, "llm_node_completed", {"node": "plan"})
        self._audit.record(run_id, "graph_node_finished", {"node": "plan"})
        self._hooks.after_plan(run_id=run_id, request=state["request"], plan=plan)
        return {"plan": plan}

    def _review_plan_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        if state.get("fastpath_active"):
            plan_review = self._plan_reviewer.deterministic_review(plan=state["plan"])
            self._audit.record(run_id, "plan_reviewed", plan_review)
            self._audit.record(run_id, "graph_node_finished", {"node": "review_plan"})
            return {"plan_review": plan_review}
        plan_review = self._plan_reviewer.review(
            request=state["request"],
            plan=state["plan"],
        )
        self._audit.record(run_id, "plan_reviewed", plan_review)
        self._audit.record(run_id, "llm_node_completed", {"node": "review_plan"})
        self._audit.record(run_id, "graph_node_finished", {"node": "review_plan"})
        return {"plan_review": plan_review}

    def _human_approval_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        fastpath_active = bool(state.get("fastpath_active"))
        approval = self._approval_gate.evaluate(
            request=state["request"],
            plan=state["plan"],
            plan_review=state["plan_review"],
            skip_llm_assessment=fastpath_active,
        )
        self._audit.record(run_id, "human_approval_checked", approval)
        if not fastpath_active:
            self._audit.record(run_id, "llm_node_completed", {"node": "human_approval"})
        self._audit.record(run_id, "graph_node_finished", {"node": "human_approval"})

        # With a checkpointer, pause the graph here and resume in-place once the
        # human decides (gateway.resume). Without one, emit a waiting output so
        # the client can perform a protected full resubmit.
        if approval["status"] == "waiting_for_approval" and self._checkpointer is not None:
            raise NodeInterrupt(approval)

        if approval["status"] in {"waiting_for_approval", "rejected"}:
            is_rejected = approval["status"] == "rejected"
            return {
                "approval": approval,
                "output": {
                    "status": approval["status"],
                    "approval": approval,
                    "final": {
                        "message": (
                            "This run was rejected before execution."
                            if is_rejected
                            else "This run is waiting for human approval before execution."
                        ),
                        "approval": approval,
                    },
                },
            }

        return {"approval": approval}

    def _next_after_approval(self, state: EnterpriseAgentState) -> str:
        approval = state.get("approval", {})
        if approval.get("status") in {"waiting_for_approval", "rejected"}:
            return "review_output"
        return "execute"

    def _execute_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        with timed_event(self._audit, state["run_id"], "node_timing", node="execute"):
            output = self._executor.execute(
                run_id=state["run_id"],
                request=state["request"],
                plan=state["plan"],
                intent=state["intent"],
            )
        self._audit.record(state["run_id"], "llm_node_completed", {"node": "execute"})
        self._audit.record(state["run_id"], "graph_node_finished", {"node": "execute"})
        self._hooks.after_execute(
            run_id=state["run_id"],
            request=state["request"],
            output=output,
        )
        return {"output": output}

    def _review_output_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        with timed_event(self._audit, run_id, "node_timing", node="review_output"):
            output_review = self._output_reviewer.review(
                request=state["request"],
                plan=state["plan"],
                output=state["output"],
            )
        self._audit.record(run_id, "output_reviewed", output_review)
        self._audit.record(run_id, "llm_node_completed", {"node": "review_output"})
        self._audit.record(run_id, "graph_node_finished", {"node": "review_output"})
        return {"output_review": output_review}

    def _finalize_node(self, state: EnterpriseAgentState) -> EnterpriseAgentState:
        run_id = state["run_id"]
        output = dict(state["output"])
        output_review = state.get("output_review", {})
        if output_review.get("status") == "failed" and not output.get("error"):
            self._audit.record(
                run_id,
                "output_blocked",
                {
                    "reason": output_review.get("reason") or "output governance review failed",
                    "output_keys": sorted(output.keys()),
                },
            )
            output = {
                "error": "output_review_failed",
                "reason": output_review.get("reason") or "output governance review failed",
                "final": {
                    "message": "The response was blocked by output governance review.",
                },
            }
        output["governance"] = {
            "runtime_context": state.get("runtime_context", {}),
            "intent": asdict(state["intent"]) if "intent" in state else {},
            "plan_review": state.get("plan_review", {}),
            "approval": state.get("approval", {}),
            "output_review": output_review,
        }
        has_error = "error" in output
        output_status = output.get("status")
        if has_error:
            run_status = "failed"
        elif output_status in {"waiting_for_approval", "rejected"}:
            run_status = output_status
        else:
            run_status = "completed"
        _log.info("run finished: %s", run_status, extra={"run_id": run_id})
        self._audit.record(
            run_id,
            "run_finished",
            {"has_error": has_error, "status": run_status},
        )
        self._audit.record(run_id, "graph_node_finished", {"node": "finalize"})
        self._hooks.on_run_finished(run_id=run_id, request=state["request"], output=output)
        return {
            "response": TaskResponse(
                output=output,
                plan=state["plan"],
                audit_events=self._audit.events_for(run_id),
            )
        }
