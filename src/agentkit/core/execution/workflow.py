"""执行开发时已知且固定的 Workflow Handler。"""

from __future__ import annotations

from .models import OrchestrationMode, StrategyRequest, StrategyResult
from .protocol import ExecutionContext
from .selector import StrategyPolicyError


class WorkflowStrategy:
    name = "workflow"

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult:
        skill_name = request.capability.primary_skill
        if not skill_name or len(request.capability.candidate_skills) != 1:
            raise StrategyPolicyError("Workflow 只允许一个入口 Capability")
        skill = context.skill(skill_name)
        if skill.execution.orchestration is not OrchestrationMode.WORKFLOW:
            raise StrategyPolicyError(f"Capability 未声明 workflow: {skill_name}")
        output = skill.handler(context.skill_context(skill), dict(request.arguments))
        summary = str(output.get("summary") or output.get("campaign_summary") or "")
        artifact = context.artifacts.put(
            kind=f"{skill.name}.result",
            payload=output,
            summary=summary,
            metadata={"skill": skill.name, "run_id": context.run_id},
        ).ref()
        return StrategyResult(
            status="completed",
            output=output,
            artifacts=(artifact,),
        )


__all__ = ["WorkflowStrategy"]
