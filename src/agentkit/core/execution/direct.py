"""直接回答或执行单个 Capability。"""

from __future__ import annotations

from .models import StrategyRequest, StrategyResult
from .protocol import ExecutionContext
from .selector import StrategyPolicyError


class DirectStrategy:
    name = "direct"

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult:
        if request.capability.response_mode == "answer":
            if context.answer_handler is None:
                return StrategyResult(status="completed", output={"answer": request.goal})
            return StrategyResult(
                status="completed",
                output=context.answer_handler(context, request),
                metrics={"model_calls": 1},
            )
        skill_name = request.capability.primary_skill
        if not skill_name or len(request.capability.candidate_skills) != 1:
            raise StrategyPolicyError("Direct 只允许一个明确的 Capability")
        skill = context.skill(skill_name)
        output = skill.handler(context.skill_context(skill), dict(request.arguments))
        return StrategyResult(status="completed", output=output)


__all__ = ["DirectStrategy"]
