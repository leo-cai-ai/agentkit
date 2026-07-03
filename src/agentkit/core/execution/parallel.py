"""多个无依赖只读 Capability 的受限并发执行。"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

from .models import StrategyRequest, StrategyResult, ToolPolicy
from .protocol import ExecutionContext
from .selector import StrategyPolicyError


class ParallelStrategy:
    name = "parallel"

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult:
        names = request.capability.candidate_skills
        if len(names) < 2 or request.capability.complexity.has_dependencies:
            raise StrategyPolicyError("Parallel 需要多个无依赖 Capability")
        skills = [context.skill(name) for name in names]
        if any(skill.execution.tool_policy is ToolPolicy.SIDE_EFFECT for skill in skills):
            raise StrategyPolicyError("Parallel 禁止执行副作用 Capability")

        def execute_one(name: str) -> tuple[str, dict]:
            skill = context.skill(name)
            raw_args = request.arguments.get(name, request.arguments)
            if not isinstance(raw_args, dict):
                raise StrategyPolicyError(f"Parallel 参数必须是对象: {name}")
            output = skill.handler(context.skill_context(skill), dict(raw_args))
            return name, output

        workers = min(max(1, context.max_concurrency), len(names))
        futures = []
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="agentkit-parallel"
        ) as pool:
            for name in names:
                copied = contextvars.copy_context()
                futures.append(pool.submit(copied.run, execute_one, name))
            results = dict(future.result() for future in futures)
        return StrategyResult(
            status="completed",
            output=results,
            metrics={"tasks": len(names), "max_concurrency": workers},
        )


__all__ = ["ParallelStrategy"]
