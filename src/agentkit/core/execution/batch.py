"""把同一 Capability 的列表输入分片后确定性合并。"""

from __future__ import annotations

import math

from .models import StrategyRequest, StrategyResult
from .protocol import ExecutionContext
from .selector import StrategyPolicyError


class BatchStrategy:
    name = "batch"

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult:
        skill_name = request.capability.primary_skill
        if not skill_name or len(request.capability.candidate_skills) != 1:
            raise StrategyPolicyError("Batch 只允许一个 Capability")
        skill = context.skill(skill_name)
        if not skill.batch_key:
            raise StrategyPolicyError(f"Capability 未声明 batch_key: {skill_name}")
        values = request.arguments.get(skill.batch_key)
        if not isinstance(values, list):
            raise StrategyPolicyError(f"Batch 输入必须包含列表字段: {skill.batch_key}")
        batch_size = max(1, context.batch_size)
        outputs: list[dict] = []
        for offset in range(0, len(values), batch_size):
            shard_args = dict(request.arguments)
            shard_args[skill.batch_key] = values[offset : offset + batch_size]
            shard_args["_batch_shard"] = True
            outputs.append(skill.handler(context.skill_context(skill), shard_args))
        merger = getattr(skill.handler, "merge_batch", None)
        output = (
            merger(context.skill_context(skill), outputs, request.arguments)
            if callable(merger)
            else {"results": outputs}
        )
        return StrategyResult(
            status="completed",
            output=output,
            metrics={
                "shards": math.ceil(len(values) / batch_size) if values else 0,
                "items": len(values),
            },
        )


__all__ = ["BatchStrategy"]
