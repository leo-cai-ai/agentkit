"""受验证的多 Skill Plan-and-Execute LangGraph 子图。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, TypedDict, cast

from jsonschema import validate as validate_json
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from agentkit.core.langgraph_runtime import invoke_graph_v2

from .models import (
    AutonomyBudget,
    CapabilityResolution,
    ComplexityAssessment,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    StrategyRequest,
    StrategyResult,
    ToolPolicy,
)
from .protocol import ExecutionContext
from .registry import StrategyRegistry


class PlanStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    skill: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    args_from: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    strategy: ExecutionStrategyName | None = None


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    steps: list[PlanStepSpec] = Field(min_length=1)


@dataclass(frozen=True)
class PlanModelDecision:
    plan: ExecutionPlan
    token_count: int = 0

    def __post_init__(self) -> None:
        if self.token_count < 0:
            raise ValueError("token_count 不能小于 0")


class PlanModel(Protocol):
    def generate(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
        allowed_skills: tuple[str, ...],
        completed_artifacts: tuple[dict[str, Any], ...],
        previous_failure: dict[str, Any] | None,
        remaining_budget: dict[str, int | float],
    ) -> PlanModelDecision: ...


class PlanState(TypedDict):
    plan: ExecutionPlan | None
    completed: dict[str, dict[str, Any]]
    artifacts: list[dict[str, Any]]
    frozen_steps: list[str]
    current_step: str | None
    failure: dict[str, Any] | None
    result: StrategyResult | None
    model_calls: int
    tool_calls: int
    token_count: int
    replans: int
    deadline_at: float


class PlanValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status: str = "plan_invalid",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = dict(details or {})


class PlanExecuteStrategy:
    name = "plan_execute"

    def __init__(
        self,
        *,
        model: PlanModel,
        strategies: StrategyRegistry,
        checkpointer: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._model = model
        self._strategies = strategies
        self._checkpointer = checkpointer
        self._clock = clock

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult:
        budget = context.effective_budget
        deadline = self._clock() + budget.timeout_seconds

        def generate(state: PlanState) -> dict[str, Any]:
            stopped = self._model_budget_exhausted(state, budget)
            if stopped:
                return {"result": self._result(state, "budget_exhausted", {})}
            try:
                decision = self._model.generate(
                    context=context,
                    request=request,
                    allowed_skills=request.capability.candidate_skills,
                    completed_artifacts=tuple(state["artifacts"]),
                    previous_failure=state["failure"],
                    remaining_budget=self._remaining_budget(state, budget),
                )
            except Exception as exc:  # noqa: BLE001 - 模型异常必须形成终态
                return {
                    "result": self._result(
                        state, "model_failed", {"reason": str(exc)}
                    )
                }
            return {
                "plan": decision.plan,
                "model_calls": state["model_calls"] + 1,
                "token_count": state["token_count"] + decision.token_count,
                "failure": None,
                "current_step": None,
            }

        def validate_plan(state: PlanState) -> dict[str, Any]:
            if state["result"] is not None:
                return {}
            plan = state["plan"]
            assert plan is not None
            try:
                self._validate_plan(
                    plan=plan,
                    request=request,
                    context=context,
                    budget=budget,
                    completed=state["completed"],
                    frozen_steps=state["frozen_steps"],
                )
            except PlanValidationError as exc:
                return {
                    "result": self._result(
                        state,
                        exc.status,
                        {"reason": str(exc), **exc.details},
                    )
                }
            return {}

        def approval(state: PlanState) -> dict[str, Any]:
            if state["result"] is not None:
                return {}
            plan = state["plan"]
            assert plan is not None
            approved = set(context.request.context.get("approved_skills", []))
            required = [
                step.skill
                for step in plan.steps
                if context.skill(step.skill).execution.tool_policy is ToolPolicy.SIDE_EFFECT
                and step.skill not in approved
                and step.id not in state["completed"]
            ]
            if required:
                return {
                    "result": self._result(
                        state,
                        "waiting_for_approval",
                        {
                            "approval": {"skills": list(dict.fromkeys(required))},
                            "plan": plan.model_dump(mode="json"),
                        },
                    )
                }
            return {}

        def schedule(state: PlanState) -> dict[str, Any]:
            if state["result"] is not None:
                return {}
            plan = state["plan"]
            assert plan is not None
            if all(step.id in state["completed"] for step in plan.steps):
                outputs = {
                    step.id: state["completed"][step.id]["output"] for step in plan.steps
                }
                return {
                    "result": self._result(
                        state,
                        "completed",
                        {"steps": outputs, "frozen_steps": state["frozen_steps"]},
                    ),
                    "current_step": None,
                }
            completed_ids = set(state["completed"])
            ready = [
                step
                for step in plan.steps
                if step.id not in completed_ids and set(step.depends_on) <= completed_ids
            ]
            if not ready:
                return {
                    "result": self._result(
                        state, "plan_invalid", {"reason": "Plan 没有可调度步骤"}
                    )
                }
            return {"current_step": ready[0].id}

        def execute_step(state: PlanState) -> dict[str, Any]:
            if self._clock() >= state["deadline_at"]:
                return {"result": self._result(state, "budget_exhausted", {})}
            plan = state["plan"]
            current_id = state["current_step"]
            assert plan is not None and current_id is not None
            step = next(item for item in plan.steps if item.id == current_id)
            skill = context.skill(step.skill)
            try:
                args = _resolve_step_args(step, state["completed"])
                validate_json(instance=args, schema=skill.input_schema)
                strategy_name = step.strategy or _default_step_strategy(skill.execution)
                strategy = self._strategies.get(strategy_name.value)
                child_request = StrategyRequest(
                    goal=plan.goal,
                    arguments=args,
                    capability=CapabilityResolution(
                        response_mode="skill",
                        primary_skill=skill.name,
                        candidate_skills=(skill.name,),
                        reason=f"Plan Step {step.id}",
                        confidence="high",
                        complexity=ComplexityAssessment(candidate_skills=(skill.name,)),
                    ),
                )
                remaining_model_calls = budget.max_model_calls - state["model_calls"]
                remaining_tool_calls = budget.max_tool_calls - state["tool_calls"]
                remaining_tokens = budget.max_tokens - state["token_count"]
                remaining_timeout = state["deadline_at"] - self._clock()
                if (
                    min(
                        remaining_model_calls,
                        remaining_tool_calls,
                        remaining_tokens,
                    )
                    <= 0
                    or remaining_timeout <= 0
                ):
                    return {"result": self._result(state, "budget_exhausted", {})}
                remaining = AutonomyBudget(
                    max_model_calls=remaining_model_calls,
                    max_tool_calls=remaining_tool_calls,
                    max_iterations=budget.max_iterations,
                    max_plan_steps=budget.max_plan_steps,
                    max_replans=max(0, budget.max_replans - state["replans"]),
                    max_tokens=remaining_tokens,
                    timeout_seconds=remaining_timeout,
                )
                child_context = replace(
                    context,
                    budget=skill.autonomy.apply_to(remaining),
                )
                child = strategy.execute(context=child_context, request=child_request)
            except Exception as exc:  # noqa: BLE001 - Step 失败由 Replan Policy 处理
                return {
                    "failure": {
                        "status": "tool_failed",
                        "step_id": step.id,
                        "skill": step.skill,
                        "reason": str(exc),
                    }
                }
            if child.status != "completed":
                return {
                    "failure": {
                        "status": child.status,
                        "step_id": step.id,
                        "skill": step.skill,
                        "reason": child.output.get("reason", child.status),
                    }
                }
            artifact = context.artifacts.put(
                kind=f"plan.{step.skill}.result",
                payload=child.output,
                summary=f"Plan Step {step.id} 完成",
                metadata={"step_id": step.id, "skill": step.skill},
            ).ref()
            completed = dict(state["completed"])
            completed[step.id] = {
                "output": child.output,
                "artifact": artifact,
                "spec": step.model_dump(mode="json"),
            }
            frozen = list(state["frozen_steps"])
            if (
                skill.execution.tool_policy is ToolPolicy.SIDE_EFFECT
                and step.id not in frozen
            ):
                frozen.append(step.id)
            return {
                "completed": completed,
                "artifacts": [*state["artifacts"], *child.artifacts, artifact],
                "frozen_steps": frozen,
                "model_calls": state["model_calls"]
                + int(child.metrics.get("model_calls", 0)),
                "tool_calls": state["tool_calls"] + int(child.metrics.get("tool_calls", 0)),
                "token_count": state["token_count"]
                + int(child.metrics.get("token_count", 0)),
                "failure": None,
                "current_step": None,
            }

        def route_after_execute(state: PlanState) -> Literal["schedule", "replan", "fail", "stop"]:
            if state["result"] is not None:
                return "stop"
            failure = state["failure"]
            if failure is None:
                return "schedule"
            if (
                failure["status"] in {"tool_failed", "no_progress"}
                and state["replans"] < budget.max_replans
            ):
                return "replan"
            return "fail"

        def prepare_replan(state: PlanState) -> dict[str, Any]:
            return {"replans": state["replans"] + 1}

        def fail(state: PlanState) -> dict[str, StrategyResult]:
            failure = state["failure"] or {"status": "tool_failed", "reason": "未知失败"}
            return {
                "result": self._result(
                    state,
                    str(failure["status"]),
                    {"failure": failure},
                )
            }

        def route_result(state: PlanState) -> Literal["continue", "stop"]:
            return "stop" if state["result"] is not None else "continue"

        graph = StateGraph(PlanState)
        graph.add_node("generate", generate)
        graph.add_node("validate", validate_plan)
        graph.add_node("approval", approval)
        graph.add_node("schedule", schedule)
        graph.add_node("execute_step", execute_step)
        graph.add_node("prepare_replan", prepare_replan)
        graph.add_node("fail", fail)
        graph.add_edge(START, "generate")
        graph.add_conditional_edges(
            "generate", route_result, {"continue": "validate", "stop": END}
        )
        graph.add_conditional_edges(
            "validate", route_result, {"continue": "approval", "stop": END}
        )
        graph.add_conditional_edges(
            "approval", route_result, {"continue": "schedule", "stop": END}
        )
        graph.add_conditional_edges(
            "schedule", route_result, {"continue": "execute_step", "stop": END}
        )
        graph.add_conditional_edges(
            "execute_step",
            route_after_execute,
            {
                "schedule": "schedule",
                "replan": "prepare_replan",
                "fail": "fail",
                "stop": END,
            },
        )
        graph.add_edge("prepare_replan", "generate")
        graph.add_edge("fail", END)
        final = cast(
            PlanState,
            invoke_graph_v2(
                graph.compile(checkpointer=self._checkpointer),
                PlanState(
                    plan=None,
                    completed={},
                    artifacts=[],
                    frozen_steps=[],
                    current_step=None,
                    failure=None,
                    result=None,
                    model_calls=0,
                    tool_calls=0,
                    token_count=0,
                    replans=0,
                    deadline_at=deadline,
                ),
                config={
                    "configurable": {
                        "thread_id": f"{context.run_id}:plan:{context.agent.name}"
                    }
                },
            ),
        )
        result = final.get("result")
        return result or self._result(final, "plan_invalid", {"reason": "Plan 无终态"})

    def _validate_plan(
        self,
        *,
        plan: ExecutionPlan,
        request: StrategyRequest,
        context: ExecutionContext,
        budget: AutonomyBudget,
        completed: dict[str, dict[str, Any]],
        frozen_steps: list[str],
    ) -> None:
        actual_steps = len(plan.steps)
        if actual_steps > budget.max_plan_steps:
            raise PlanValidationError(
                "Plan 步骤数超过预算："
                f"生成 {actual_steps}，最多允许 {budget.max_plan_steps}",
                details={
                    "actual_steps": actual_steps,
                    "max_plan_steps": budget.max_plan_steps,
                },
            )
        ids = [step.id for step in plan.steps]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("Plan Step ID 必须唯一")
        id_set = set(ids)
        for step in plan.steps:
            if step.skill not in request.capability.candidate_skills:
                raise PlanValidationError(
                    f"Plan 引用了未绑定 Capability: {step.skill}",
                    status="strategy_rejected",
                )
            context.skill(step.skill)
            missing = set(step.depends_on) - id_set
            if missing:
                raise PlanValidationError(
                    f"Plan 依赖不存在: {', '.join(sorted(missing))}"
                )
            for reference in step.args_from.values():
                source = reference.split(".", 1)[0]
                if source not in step.depends_on:
                    raise PlanValidationError("args_from 必须引用显式依赖 Step")
        _assert_acyclic(plan.steps)
        for frozen_id in frozen_steps:
            matching = next((step for step in plan.steps if step.id == frozen_id), None)
            if matching is None:
                raise PlanValidationError("Replan 不能删除已冻结副作用 Step")
            if matching.model_dump(mode="json") != completed[frozen_id]["spec"]:
                raise PlanValidationError("Replan 不能修改已冻结副作用 Step")

    def _model_budget_exhausted(
        self,
        state: PlanState,
        budget: AutonomyBudget,
    ) -> bool:
        return (
            self._clock() >= state["deadline_at"]
            or state["model_calls"] >= budget.max_model_calls
            or state["token_count"] >= budget.max_tokens
        )

    def _remaining_budget(
        self,
        state: PlanState,
        budget: AutonomyBudget,
    ) -> dict[str, int | float]:
        return {
            "model_calls": budget.max_model_calls - state["model_calls"],
            "tool_calls": budget.max_tool_calls - state["tool_calls"],
            "plan_steps": budget.max_plan_steps,
            "replans": budget.max_replans - state["replans"],
            "tokens": budget.max_tokens - state["token_count"],
            "timeout_seconds": max(0.0, state["deadline_at"] - self._clock()),
        }

    def _result(
        self,
        state: PlanState,
        status: str,
        output: dict[str, Any],
    ) -> StrategyResult:
        return StrategyResult(
            status=status,
            output=output,
            artifacts=tuple(state["artifacts"]),
            metrics={
                "steps": len(state["completed"]),
                "replans": state["replans"],
                "model_calls": state["model_calls"],
                "tool_calls": state["tool_calls"],
                "token_count": state["token_count"],
            },
        )


def _default_step_strategy(execution: Any) -> ExecutionStrategyName:
    if execution.orchestration is OrchestrationMode.WORKFLOW:
        return ExecutionStrategyName.WORKFLOW
    if execution.reasoning is ReasoningStrategy.REACT:
        return ExecutionStrategyName.REACT
    return ExecutionStrategyName.DIRECT


def _resolve_step_args(
    step: PlanStepSpec,
    completed: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = dict(step.args)
    for target, reference in step.args_from.items():
        source_id, separator, path = reference.partition(".")
        if not separator or source_id not in completed:
            raise PlanValidationError(f"无效 Artifact 引用: {reference}")
        value: Any = completed[source_id]["output"]
        for component in path.split("."):
            if not isinstance(value, dict) or component not in value:
                raise PlanValidationError(f"Artifact 字段不存在: {reference}")
            value = value[component]
        result[target] = value
    return result


def _assert_acyclic(steps: list[PlanStepSpec]) -> None:
    dependencies = {step.id: set(step.depends_on) for step in steps}
    ready = [step_id for step_id, deps in dependencies.items() if not deps]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for step_id, deps in dependencies.items():
            if current in deps:
                deps.remove(current)
                if not deps:
                    ready.append(step_id)
    if visited != len(steps):
        raise PlanValidationError("Plan 依赖存在环")


__all__ = [
    "ExecutionPlan",
    "PlanExecuteStrategy",
    "PlanModel",
    "PlanModelDecision",
    "PlanStepSpec",
]
