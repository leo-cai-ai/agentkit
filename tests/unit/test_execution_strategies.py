from __future__ import annotations

import pytest

from agentkit.core.artifacts import InMemoryArtifactStore
from agentkit.core.contracts import (
    AgentProfile,
    ArtifactContextPolicy,
    ContextPolicy,
    MemoryContextPolicy,
    RagContextPolicy,
    SkillDefinition,
    TaskRequest,
)
from agentkit.core.execution.batch import BatchStrategy
from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    AutonomyLimits,
    CapabilityResolution,
    ComplexityAssessment,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    StrategyRequest,
    ToolPolicy,
)
from agentkit.core.execution.parallel import ParallelStrategy
from agentkit.core.execution.protocol import ExecutionContext
from agentkit.core.execution.selector import StrategyPolicyError
from agentkit.core.execution.workflow import WorkflowStrategy


def _agent() -> AgentProfile:
    return AgentProfile(
        name="test_agent",
        domain="test",
        description="测试",
        allowed_skills=["demo.one", "demo.two", "demo.batch", "demo.workflow"],
        execution_policy=AgentExecutionPolicy(
            default_strategy=ExecutionStrategyName.DIRECT,
            allowed_strategies=tuple(ExecutionStrategyName),
            allow_side_effects=True,
        ),
        autonomy_budget=AutonomyBudget(10, 10, 10, 10, 1, 10000, 60),
        context_policy=ContextPolicy(
            MemoryContextPolicy(False, "agent_user", 2, 1000),
            RagContextPolicy(False, (), 1, 100),
            ArtifactContextPolicy(("test",), ("test",)),
        ),
        instructions="测试执行 Agent 指令",
    )


def _skill(
    name: str,
    handler,
    *,
    orchestration: OrchestrationMode = OrchestrationMode.SINGLE,
    tool_policy: ToolPolicy = ToolPolicy.READ_ONLY,
    batch_key: str | None = None,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        domain="test",
        description=name,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=[],
        execution=SkillExecutionPolicy(
            ReasoningStrategy.DIRECT, orchestration, tool_policy
        ),
        autonomy=AutonomyLimits(),
        tools=[],
        handler=handler,
        batch_key=batch_key,
    )


def _resolution(*skills: str) -> CapabilityResolution:
    return CapabilityResolution(
        response_mode="multi_skill" if len(skills) > 1 else "skill",
        primary_skill=skills[0] if len(skills) == 1 else None,
        candidate_skills=skills,
        reason="test",
        confidence="high",
        complexity=ComplexityAssessment(
            candidate_skills=skills,
            independent_skills=len(skills) if len(skills) > 1 else 0,
        ),
    )


def _context(
    *skills: SkillDefinition,
    batch_size: int = 2,
    context_invoker: object | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        tenant_id="t1",
        tenant_selector="company_alpha",
        run_id="r1",
        agent=_agent(),
        request=TaskRequest(user_id="u1", roles=[], text="执行"),
        skills={skill.name: skill for skill in skills},
        tools={},
        tenant_config={},
        artifacts=InMemoryArtifactStore(),
        context_invoker=context_invoker or object(),
        batch_size=batch_size,
        max_concurrency=4,
    )


def test_execution_context_propagates_context_invoker_to_skill() -> None:
    skill = _skill("demo.one", lambda ctx, args: {})
    marker = object()
    context = ExecutionContext(
        tenant_id="t1",
        tenant_selector="company_alpha",
        run_id="r1",
        agent=_agent(),
        request=TaskRequest(user_id="u1", roles=[], text="执行"),
        skills={skill.name: skill},
        tools={},
        tenant_config={},
        artifacts=InMemoryArtifactStore(),
        context_invoker=marker,
    )

    scoped = context.skill_context(skill)

    assert scoped.tenant_selector == "company_alpha"
    assert scoped.run_id == "r1"
    assert scoped.agent is context.agent
    assert scoped.skill is skill
    assert scoped.context_invoker is marker


def test_direct_executes_one_skill() -> None:
    skill = _skill("demo.one", lambda ctx, args: {"value": args["value"]})
    request = StrategyRequest("执行", {"value": 3}, _resolution("demo.one"))

    result = DirectStrategy().execute(context=_context(skill), request=request)

    assert result.status == "completed"
    assert result.output == {"value": 3}


def test_workflow_writes_result_artifact() -> None:
    skill = _skill(
        "demo.workflow",
        lambda ctx, args: {"summary": "流程完成", "value": args["value"]},
        orchestration=OrchestrationMode.WORKFLOW,
    )
    context = _context(skill)
    request = StrategyRequest("执行流程", {"value": 7}, _resolution("demo.workflow"))

    result = WorkflowStrategy().execute(context=context, request=request)

    assert result.status == "completed"
    assert len(result.artifacts) == 1
    assert context.artifacts.get(result.artifacts[0]["artifact_id"]).payload["value"] == 7


def test_workflow_surfaces_deferred_action_without_executing_it() -> None:
    action = {"tool_name": "publish.note", "arguments": {"draft_id": "D-1"}}
    skill = _skill(
        "demo.workflow",
        lambda ctx, args: {"summary": "等待发布", "deferred_action": action},
        orchestration=OrchestrationMode.WORKFLOW,
    )

    result = WorkflowStrategy().execute(
        context=_context(skill),
        request=StrategyRequest("发布", {}, _resolution("demo.workflow")),
    )

    assert result.status == "deferred_action"
    assert result.output["deferred_action"] == action


def test_batch_shards_and_merges() -> None:
    skill = _skill(
        "demo.batch",
        lambda ctx, args: {"ids": args["ids"]},
        orchestration=OrchestrationMode.BATCH,
        batch_key="ids",
    )
    request = StrategyRequest("批处理", {"ids": [1, 2, 3]}, _resolution("demo.batch"))

    result = BatchStrategy().execute(
        context=_context(skill, batch_size=2), request=request
    )

    assert result.output == {"results": [{"ids": [1, 2]}, {"ids": [3]}]}
    assert result.metrics == {"shards": 2, "items": 3}


def test_batch_marks_shards_and_calls_merger_once() -> None:
    shard_flags: list[bool] = []
    merge_calls: list[list[dict]] = []

    def handler(ctx, args):
        shard_flags.append(args.get("_batch_shard") is True)
        return {"ids": args["ids"]}

    def merge(ctx, outputs, original_args):
        merge_calls.append(outputs)
        return {"ids": [item for output in outputs for item in output["ids"]]}

    handler.merge_batch = merge
    skill = _skill(
        "demo.batch",
        handler,
        orchestration=OrchestrationMode.BATCH,
        batch_key="ids",
    )

    result = BatchStrategy().execute(
        context=_context(skill, batch_size=1),
        request=StrategyRequest("批处理", {"ids": [1, 2, 3]}, _resolution("demo.batch")),
    )

    assert shard_flags == [True, True, True]
    assert len(merge_calls) == 1
    assert result.output == {"ids": [1, 2, 3]}


def test_parallel_executes_independent_read_only_skills() -> None:
    first = _skill("demo.one", lambda ctx, args: {"one": args["value"]})
    second = _skill("demo.two", lambda ctx, args: {"two": args["value"]})
    request = StrategyRequest(
        "并行",
        {"demo.one": {"value": 1}, "demo.two": {"value": 2}},
        _resolution("demo.one", "demo.two"),
    )

    result = ParallelStrategy().execute(
        context=_context(first, second), request=request
    )

    assert result.status == "completed"
    assert result.output == {"demo.one": {"one": 1}, "demo.two": {"two": 2}}
    assert result.metrics["tasks"] == 2


def test_parallel_rejects_side_effect_skill() -> None:
    side_effect = _skill(
        "demo.one",
        lambda ctx, args: {"ok": True},
        tool_policy=ToolPolicy.SIDE_EFFECT,
    )
    request = StrategyRequest("并行", {}, _resolution("demo.one", "demo.two"))

    with pytest.raises(StrategyPolicyError, match="副作用"):
        ParallelStrategy().execute(
            context=_context(
                side_effect,
                _skill("demo.two", lambda ctx, args: {"ok": True}),
            ),
            request=request,
        )
