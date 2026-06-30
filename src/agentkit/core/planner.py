"""Planner that converts a route decision into executable steps."""

from __future__ import annotations

import json
from typing import Any

from .contracts import IntentFrame, PlanStep, RouteDecision, TaskPlan, TaskRequest
from .llm_client import LLMRequiredError, require_chat_json
from .registry import SkillRegistry


class Planner:
    def __init__(self, *, tenant_config: dict, skills: SkillRegistry) -> None:
        self._tenant_config = tenant_config
        self._skills = skills

    def make_plan(
        self,
        *,
        request: TaskRequest,
        route: RouteDecision,
        intent: IntentFrame,
        resolved_args: dict[str, Any] | None = None,
    ) -> TaskPlan:
        deterministic = self._deterministic_plan(
            request=request,
            route=route,
            resolved_args=resolved_args,
        )
        data = require_chat_json(
            self._llm_system_prompt(),
            self._llm_user_prompt(
                request=request,
                route=route,
                intent=intent,
                deterministic=deterministic,
                resolved_args=resolved_args,
            ),
        )
        return self._plan_from_llm_data(
            data=data,
            request=request,
            route=route,
            deterministic=deterministic,
            resolved_args=resolved_args,
        )

    def deterministic_plan(
        self,
        *,
        request: TaskRequest,
        route: RouteDecision,
        resolved_args: dict[str, Any] | None = None,
    ) -> TaskPlan:
        """Rule-based single-step plan only (no LLM). Used by the fast-path."""
        return self._deterministic_plan(
            request=request,
            route=route,
            resolved_args=resolved_args,
        )

    def _deterministic_plan(
        self,
        *,
        request: TaskRequest,
        route: RouteDecision,
        resolved_args: dict[str, Any] | None = None,
    ) -> TaskPlan:
        if route.skill_name is None:
            return TaskPlan(route=route, steps=[], warnings=["No skill selected."])

        skill = self._skills.get(route.skill_name)
        args = dict(request.context) if resolved_args is None else dict(resolved_args)
        mode = skill.execution_mode
        warnings: list[str] = []

        if skill.batch_key:
            values = args.get(skill.batch_key, [])
            count = len(values) if isinstance(values, list) else 0
            batch_threshold = int(self._tenant_config.get("batch_threshold", 2))
            if count >= batch_threshold:
                mode = "batch"
            if count == 0:
                warnings.append(f"batch key '{skill.batch_key}' is empty or missing")

        return TaskPlan(
            route=route,
            steps=[
                PlanStep(
                    step_id=1,
                    skill_name=skill.name,
                    mode=mode,
                    args=args,
                )
            ],
            warnings=warnings,
        )

    def _llm_system_prompt(self) -> str:
        return (
            "You are the LLM planning node in a governed LangGraph agent runtime. "
            "Return only valid JSON with keys: steps, warnings. "
            "Each step must contain step_id, skill_name, mode, args, depends_on. "
            "Use only registered skills and the provided request context/entities. "
            "Do not invent tools, hidden data, permissions, or connector results. "
            "If no route skill is selected, return an empty steps list and explain in warnings. "
            "Prefer the deterministic_suggestion for batch mode and required args "
            "unless the user context clearly conflicts."
        )

    def _llm_user_prompt(
        self,
        *,
        request: TaskRequest,
        route: RouteDecision,
        intent: IntentFrame,
        deterministic: TaskPlan,
        resolved_args: dict[str, Any] | None,
    ) -> str:
        skill_payload: dict[str, Any] | None = None
        if route.skill_name is not None:
            skill = self._skills.get(route.skill_name)
            skill_payload = {
                "name": skill.name,
                "domain": skill.domain,
                "description": skill.description,
                "input_schema": skill.input_schema,
                "output_schema": skill.output_schema,
                "execution_mode": skill.execution_mode,
                "batch_key": skill.batch_key,
                "tools": skill.tools,
                "permissions": skill.permissions,
            }
        payload = {
            "message": request.text,
            "request_context": request.context,
            "intent": {
                "intent_type": intent.intent_type,
                "goal": intent.goal,
                "entities": intent.entities,
                "target": intent.target,
                "boundaries": intent.boundaries,
            },
            "route": {
                "skill_name": route.skill_name,
                "reason": route.reason,
                "confidence": route.confidence,
            },
            "selected_skill": skill_payload,
            "tenant_policy": {
                "batch_threshold": self._tenant_config.get("batch_threshold"),
                "batch_size": self._tenant_config.get("batch_size"),
                "approval_required_skills": self._tenant_config.get("approval_required_skills", []),
            },
            "deterministic_suggestion": {
                "steps": [
                    {
                        "step_id": step.step_id,
                        "skill_name": step.skill_name,
                        "mode": step.mode,
                        "args": step.args,
                        "depends_on": step.depends_on,
                    }
                    for step in deterministic.steps
                ],
                "warnings": deterministic.warnings,
            },
            "resolved_skill_arguments": resolved_args,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _plan_from_llm_data(
        self,
        *,
        data: dict[str, Any],
        request: TaskRequest,
        route: RouteDecision,
        deterministic: TaskPlan,
        resolved_args: dict[str, Any] | None,
    ) -> TaskPlan:
        warnings = _string_list(data.get("warnings"))
        if route.skill_name is None:
            return TaskPlan(
                route=route,
                steps=[],
                warnings=warnings or deterministic.warnings or ["No skill selected."],
            )

        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise LLMRequiredError("Planning LLM did not return any executable steps.")

        skill = self._skills.get(route.skill_name)
        steps: list[PlanStep] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                raise LLMRequiredError("Planning LLM returned a non-object step.")
            step_skill = str(raw_step.get("skill_name") or route.skill_name)
            if step_skill != route.skill_name:
                raise LLMRequiredError(
                    f"Planning LLM attempted to use skill '{step_skill}' "
                    f"outside route '{route.skill_name}'."
                )
            args = raw_step.get("args")
            if not isinstance(args, dict):
                args = dict(request.context)
            if resolved_args is not None:
                args = dict(resolved_args)
            mode = str(raw_step.get("mode") or skill.execution_mode)
            mode = self._validated_mode(mode=mode, skill_name=route.skill_name, args=args)
            depends_on = raw_step.get("depends_on")
            if not isinstance(depends_on, list):
                depends_on = []
            steps.append(
                PlanStep(
                    step_id=int(raw_step.get("step_id") or index),
                    skill_name=route.skill_name,
                    mode=mode,  # type: ignore[arg-type]
                    args=args,
                    depends_on=[int(item) for item in depends_on if isinstance(item, int)],
                )
            )

        return TaskPlan(route=route, steps=steps, warnings=warnings)

    def _validated_mode(self, *, mode: str, skill_name: str, args: dict[str, Any]) -> str:
        allowed_modes = {"react", "plan_execute", "batch", "workflow", "no_tool"}
        if mode not in allowed_modes:
            raise LLMRequiredError(f"Planning LLM returned invalid execution mode: {mode}")

        skill = self._skills.get(skill_name)
        if skill.batch_key:
            values = args.get(skill.batch_key, [])
            count = len(values) if isinstance(values, list) else 0
            batch_threshold = int(self._tenant_config.get("batch_threshold", 2))
            if count >= batch_threshold:
                return "batch"
        return mode


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
