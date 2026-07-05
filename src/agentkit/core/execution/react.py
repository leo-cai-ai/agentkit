"""带预算、白名单和可靠终止语义的 ReAct LangGraph 子图。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.core.artifacts import canonical_json
from agentkit.core.contracts import SkillDefinition
from agentkit.core.langgraph_runtime import invoke_graph_v2

from .models import AutonomyBudget, StrategyRequest, StrategyResult, ToolRisk
from .protocol import ExecutionContext


class ReactAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call", "final"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    decision_summary: str = ""
    answer: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> ReactAction:
        if self.type == "tool_call" and not self.tool_name:
            raise ValueError("tool_call 必须提供 tool_name")
        if self.type == "final" and not self.answer:
            raise ValueError("final 必须提供 answer")
        return self


@dataclass(frozen=True)
class ReactModelDecision:
    action: ReactAction
    token_count: int = 0

    def __post_init__(self) -> None:
        if self.token_count < 0:
            raise ValueError("token_count 不能小于 0")


class ReactModel(Protocol):
    def decide(
        self,
        *,
        context: ExecutionContext,
        skill: SkillDefinition,
        request: StrategyRequest,
        observations: tuple[dict[str, Any], ...],
        allowed_tools: tuple[dict[str, Any], ...],
        remaining_budget: dict[str, int | float],
    ) -> ReactModelDecision: ...


class ReactState(TypedDict):
    observations: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    result: StrategyResult | None
    action: ReactAction | None
    model_calls: int
    tool_calls: int
    iterations: int
    token_count: int
    deadline_at: float
    seen_actions: list[str]


class ReactStrategy:
    name = "react"

    def __init__(
        self,
        *,
        model: ReactModel,
        clock: Callable[[], float] = time.time,
        checkpointer: Any = None,
    ) -> None:
        self._model = model
        self._clock = clock
        self._checkpointer = checkpointer

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult:
        skill_name = request.capability.primary_skill
        if not skill_name or len(request.capability.candidate_skills) != 1:
            return StrategyResult(
                status="strategy_rejected",
                output={"reason": "ReAct 只允许一个 Capability"},
            )
        skill = context.skill(skill_name)
        budget = context.effective_budget
        deadline = self._clock() + budget.timeout_seconds
        allowed_names = tuple(skill.tools)
        allowed_tools = tuple(
            {
                "name": name,
                "description": context.tools[name].description,
                "input_schema": context.tools[name].input_schema,
                "risk": context.tools[name].risk.value,
            }
            for name in allowed_names
            if name in context.tools
        )

        def decide(state: ReactState) -> dict[str, Any]:
            stop = self._budget_status(state, budget)
            if stop:
                return {"result": self._stopped_result(state, stop), "action": None}
            remaining = {
                "model_calls": budget.max_model_calls - state["model_calls"],
                "tool_calls": budget.max_tool_calls - state["tool_calls"],
                "iterations": budget.max_iterations - state["iterations"],
                "tokens": budget.max_tokens - state["token_count"],
                "timeout_seconds": max(0.0, state["deadline_at"] - self._clock()),
            }
            try:
                decision = self._model.decide(
                    context=context,
                    skill=skill,
                    request=request,
                    observations=tuple(state["observations"]),
                    allowed_tools=allowed_tools,
                    remaining_budget=remaining,
                )
            except Exception as exc:  # noqa: BLE001 - 统一为可审计终态
                return {
                    "result": self._result(
                        state,
                        status="model_failed",
                        output={"reason": str(exc)},
                    ),
                    "action": None,
                }
            return {
                "action": decision.action,
                "model_calls": state["model_calls"] + 1,
                "iterations": state["iterations"] + 1,
                "token_count": state["token_count"] + decision.token_count,
            }

        def route_decision(state: ReactState) -> Literal["execute_tool", "finish", "stop"]:
            if state["result"] is not None:
                return "stop"
            action = state["action"]
            if action is None:
                return "stop"
            return "finish" if action.type == "final" else "execute_tool"

        def finish(state: ReactState) -> dict[str, StrategyResult]:
            action = state["action"]
            assert action is not None
            return {
                "result": self._result(
                    state,
                    status="completed",
                    output={
                        "answer": action.answer,
                        "evidence_refs": action.evidence_refs,
                        "observations": state["observations"],
                    },
                )
            }

        def execute_tool(state: ReactState) -> dict[str, Any]:
            stop = self._budget_status(state, budget, before_tool=True)
            if stop:
                return {"result": self._stopped_result(state, stop), "action": None}
            action = state["action"]
            assert action is not None and action.tool_name is not None
            if action.tool_name not in allowed_names or action.tool_name not in context.tools:
                return {
                    "result": self._result(
                        state,
                        status="strategy_rejected",
                        output={"reason": f"Tool 不在 Skill 白名单: {action.tool_name}"},
                    )
                }
            tool = context.tools[action.tool_name]
            if tool.risk is ToolRisk.SIDE_EFFECT:
                return {
                    "result": self._result(
                        state,
                        status="deferred_action",
                        output={
                            "deferred_action": {
                                "tool_name": tool.name,
                                "arguments": action.arguments,
                            }
                        },
                    )
                }
            fingerprint = _action_fingerprint(action)
            if fingerprint in state["seen_actions"]:
                return {
                    "result": self._result(
                        state,
                        status="no_progress",
                        output={"reason": "检测到重复 Tool Action"},
                    )
                }
            try:
                output = context.skill_context(skill).call_tool(tool.name, action.arguments)
            except Exception as exc:  # noqa: BLE001 - ToolExecutor 已归一化错误
                return {
                    "result": self._result(
                        state,
                        status="tool_failed",
                        output={"tool": tool.name, "reason": str(exc)},
                    )
                }
            artifact = context.artifacts.put(
                kind=f"react.{tool.name}.observation",
                payload=output,
                summary=_observation_summary(output),
                metadata={"tool": tool.name, "run_id": context.run_id},
            ).ref()
            observation = {
                "tool": tool.name,
                "summary": artifact["summary"],
                "artifact_id": artifact["artifact_id"],
            }
            return {
                "observations": [*state["observations"], observation],
                "artifacts": [*state["artifacts"], artifact],
                "tool_calls": state["tool_calls"] + 1,
                "seen_actions": [*state["seen_actions"], fingerprint],
                "action": None,
            }

        def route_after_tool(state: ReactState) -> Literal["decide", "stop"]:
            return "stop" if state["result"] is not None else "decide"

        graph = StateGraph(ReactState)
        graph.add_node("decide", decide)
        graph.add_node("execute_tool", execute_tool)
        graph.add_node("finish", finish)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges(
            "decide",
            route_decision,
            {"execute_tool": "execute_tool", "finish": "finish", "stop": END},
        )
        graph.add_conditional_edges(
            "execute_tool", route_after_tool, {"decide": "decide", "stop": END}
        )
        graph.add_edge("finish", END)
        final = cast(
            ReactState,
            invoke_graph_v2(
                graph.compile(checkpointer=self._checkpointer),
                ReactState(
                    observations=[],
                    artifacts=[],
                    result=None,
                    action=None,
                    model_calls=0,
                    tool_calls=0,
                    iterations=0,
                    token_count=0,
                    deadline_at=deadline,
                    seen_actions=[],
                ),
                config={
                    "configurable": {"thread_id": f"{context.run_id}:react:{skill_name}"}
                },
            ),
        )
        result = final.get("result")
        if result is None:
            return self._result(final, status="no_progress", output={"reason": "无终态"})
        return result

    def _budget_status(
        self,
        state: ReactState,
        budget: AutonomyBudget,
        *,
        before_tool: bool = False,
    ) -> str | None:
        if self._clock() >= state["deadline_at"]:
            return "budget_exhausted"
        if state["iterations"] >= budget.max_iterations:
            return "budget_exhausted"
        if state["model_calls"] >= budget.max_model_calls:
            return "budget_exhausted"
        if state["token_count"] >= budget.max_tokens:
            return "budget_exhausted"
        if before_tool and state["tool_calls"] >= budget.max_tool_calls:
            return "budget_exhausted"
        return None

    def _stopped_result(self, state: ReactState, status: str) -> StrategyResult:
        return self._result(
            state,
            status=status,
            output={"observations": state["observations"]},
        )

    def _result(
        self,
        state: ReactState,
        *,
        status: str,
        output: dict[str, Any],
    ) -> StrategyResult:
        return StrategyResult(
            status=status,
            output=output,
            artifacts=tuple(state["artifacts"]),
            metrics={
                "model_calls": state["model_calls"],
                "tool_calls": state["tool_calls"],
                "iterations": state["iterations"],
                "token_count": state["token_count"],
            },
        )


def _action_fingerprint(action: ReactAction) -> str:
    payload = {
        "tool_name": action.tool_name,
        "arguments": action.arguments,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _observation_summary(output: dict[str, Any]) -> str:
    return json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)[:500]


__all__ = ["ReactAction", "ReactModel", "ReactModelDecision", "ReactStrategy"]
