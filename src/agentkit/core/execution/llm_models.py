"""把通用 LLM JSON 调用适配为 ReAct 与 Plan 的结构化模型。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agentkit.core.llm_client import require_chat_json

from .models import StrategyRequest
from .plan import ExecutionPlan, PlanModelDecision
from .react import ReactAction, ReactModelDecision

JsonCall = Callable[[str, str], dict[str, Any]]


class StructuredReactModel:
    def __init__(self, *, call_json: JsonCall = require_chat_json) -> None:
        self._call_json = call_json

    def decide(
        self,
        *,
        request: StrategyRequest,
        observations: tuple[dict[str, Any], ...],
        allowed_tools: tuple[dict[str, Any], ...],
        remaining_budget: dict[str, int | float],
    ) -> ReactModelDecision:
        system = (
            "你是受治理的企业 ReAct 决策节点。只返回一个 JSON Action。"
            "type 只能是 tool_call 或 final；Tool 只能从 allowed_tools 中选择。"
            "只输出简短 decision_summary，不输出隐藏思维链。"
        )
        payload = {
            "goal": request.goal,
            "arguments": request.arguments,
            "observations": observations,
            "allowed_tools": allowed_tools,
            "remaining_budget": remaining_budget,
        }
        action = ReactAction.model_validate(
            self._call_json(system, json.dumps(payload, ensure_ascii=False, default=str))
        )
        return ReactModelDecision(action=action, token_count=0)


class StructuredPlanModel:
    def __init__(self, *, call_json: JsonCall = require_chat_json) -> None:
        self._call_json = call_json

    def generate(
        self,
        *,
        request: StrategyRequest,
        allowed_skills: tuple[str, ...],
        completed_artifacts: tuple[dict[str, Any], ...],
        previous_failure: dict[str, Any] | None,
        remaining_budget: dict[str, int | float],
    ) -> PlanModelDecision:
        system = (
            "你是受治理的企业计划节点。只返回 ExecutionPlan JSON，包含 goal 和 steps。"
            "每个 Step 只能引用 allowed_skills，依赖必须构成 DAG；不得修改已完成副作用。"
            "不要输出思维链。"
        )
        payload = {
            "goal": request.goal,
            "arguments": request.arguments,
            "allowed_skills": allowed_skills,
            "completed_artifacts": completed_artifacts,
            "previous_failure": previous_failure,
            "remaining_budget": remaining_budget,
        }
        plan = ExecutionPlan.model_validate(
            self._call_json(system, json.dumps(payload, ensure_ascii=False, default=str))
        )
        return PlanModelDecision(plan=plan, token_count=0)


__all__ = ["StructuredPlanModel", "StructuredReactModel"]
