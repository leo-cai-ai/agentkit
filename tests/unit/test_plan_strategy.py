from __future__ import annotations

from dataclasses import replace
from typing import Any

from agentkit.core.contracts import SkillDefinition, TaskRequest
from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.models import (
    AutonomyBudget,
    AutonomyLimits,
    CapabilityResolution,
    ComplexityAssessment,
    OrchestrationMode,
    StrategyRequest,
    StrategyResult,
    ToolPolicy,
)
from agentkit.core.execution.plan import (
    ExecutionPlan,
    PlanExecuteStrategy,
    PlanModelDecision,
    PlanStepSpec,
)
from agentkit.core.execution.registry import StrategyRegistry
from agentkit.core.execution.workflow import WorkflowStrategy
from tests.unit.test_execution_strategies import _context, _skill


class FakePlanModel:
    def __init__(self, *plans: ExecutionPlan) -> None:
        self._plans = list(plans)
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs) -> PlanModelDecision:
        self.calls.append(kwargs)
        if not self._plans:
            raise AssertionError("FakePlanModel 没有剩余计划")
        return PlanModelDecision(plan=self._plans.pop(0), token_count=1)


class CapturingStrategy:
    name = "direct"

    def __init__(self) -> None:
        self.budgets: list[AutonomyBudget] = []

    def execute(self, *, context, request) -> StrategyResult:
        self.budgets.append(context.effective_budget)
        return StrategyResult(status="completed", output={"ok": True})


class MetricsStrategy(CapturingStrategy):
    def execute(self, *, context, request) -> StrategyResult:
        self.budgets.append(context.effective_budget)
        return StrategyResult(
            status="completed",
            output={"ok": True},
            metrics={"model_calls": 2, "tool_calls": 1, "token_count": 100},
        )


def _step(
    step_id: str,
    skill: str,
    *,
    args: dict[str, Any] | None = None,
    args_from: dict[str, str] | None = None,
    depends_on: list[str] | None = None,
) -> PlanStepSpec:
    return PlanStepSpec(
        id=step_id,
        skill=skill,
        args=args or {},
        args_from=args_from or {},
        depends_on=depends_on or [],
    )


def _plan(*steps: PlanStepSpec) -> ExecutionPlan:
    return ExecutionPlan(goal="处理订单问题", steps=list(steps))


def _plan_request(*skills: str) -> StrategyRequest:
    return StrategyRequest(
        goal="处理订单问题",
        arguments={},
        capability=CapabilityResolution(
            response_mode="multi_skill",
            primary_skill=None,
            candidate_skills=skills,
            reason="test",
            confidence="high",
            complexity=ComplexityAssessment(
                candidate_skills=skills,
                estimated_steps=len(skills),
                has_dependencies=True,
            ),
        ),
    )


def _plan_context(
    *skills: SkillDefinition,
    approved: tuple[str, ...] = (),
    budget: AutonomyBudget | None = None,
):
    context = _context(*skills)
    return replace(
        context,
        agent=replace(context.agent, allowed_skills=[skill.name for skill in skills]),
        request=TaskRequest(
            user_id="u1",
            roles=["support"],
            text="执行计划",
            context={"approved_skills": list(approved)},
        ),
        budget=budget,
    )


def _strategy(model: FakePlanModel) -> PlanExecuteStrategy:
    return PlanExecuteStrategy(
        model=model,
        strategies=StrategyRegistry([DirectStrategy(), WorkflowStrategy()]),
    )


def test_plan_executes_dependency_dag_and_artifact_arguments() -> None:
    executed: list[str] = []
    order = _skill(
        "order.lookup",
        lambda ctx, args: executed.append("order") or {"tracking_id": "T-1"},
    )
    shipping = _skill(
        "logistics.lookup",
        lambda ctx, args: executed.append("shipping")
        or {"tracking_id": args["tracking_id"], "status": "delayed"},
    )
    resolve = _skill(
        "customer.issue.resolve",
        lambda ctx, args: executed.append("resolve") or {"resolution": "补偿"},
    )
    model = FakePlanModel(
        _plan(
            _step("order", "order.lookup"),
            _step(
                "shipping",
                "logistics.lookup",
                args_from={"tracking_id": "order.tracking_id"},
                depends_on=["order"],
            ),
            _step(
                "resolve",
                "customer.issue.resolve",
                depends_on=["shipping"],
            ),
        )
    )

    result = _strategy(model).execute(
        context=_plan_context(order, shipping, resolve),
        request=_plan_request("order.lookup", "logistics.lookup", "customer.issue.resolve"),
    )

    assert result.status == "completed"
    assert executed == ["order", "shipping", "resolve"]
    assert result.output["steps"]["shipping"]["tracking_id"] == "T-1"
    assert len(result.artifacts) == 3


def test_plan_step_applies_skill_budget_inside_outer_envelope() -> None:
    capture = CapturingStrategy()
    skill = replace(
        _skill("order.lookup", lambda ctx, args: {"ok": True}),
        autonomy=AutonomyLimits(
            max_model_calls=2,
            max_plan_steps=1,
            max_tokens=500,
        ),
    )
    strategy = PlanExecuteStrategy(
        model=FakePlanModel(_plan(_step("order", "order.lookup"))),
        strategies=StrategyRegistry([capture]),
    )
    outer = AutonomyBudget(10, 10, 10, 8, 1, 5000, 60)

    result = strategy.execute(
        context=_plan_context(skill, budget=outer),
        request=_plan_request("order.lookup"),
    )

    assert result.status == "completed"
    assert capture.budgets[0].max_model_calls == 2
    assert capture.budgets[0].max_plan_steps == 1
    assert capture.budgets[0].max_tokens == 500


def test_plan_carries_child_consumption_into_next_step_budget() -> None:
    capture = MetricsStrategy()
    one = _skill("one", lambda ctx, args: {})
    two = _skill("two", lambda ctx, args: {})
    strategy = PlanExecuteStrategy(
        model=FakePlanModel(
            _plan(
                _step("one", "one"),
                _step("two", "two", depends_on=["one"]),
            )
        ),
        strategies=StrategyRegistry([capture]),
    )

    result = strategy.execute(
        context=_plan_context(
            one,
            two,
            budget=AutonomyBudget(6, 5, 10, 4, 1, 1000, 60),
        ),
        request=_plan_request("one", "two"),
    )

    assert result.status == "completed"
    assert [item.max_model_calls for item in capture.budgets] == [5, 3]
    assert [item.max_tool_calls for item in capture.budgets] == [5, 4]
    assert [item.max_tokens for item in capture.budgets] == [999, 899]


def test_plan_step_budget_error_reports_actual_and_limit() -> None:
    skills = [_skill(f"s{index}", lambda ctx, args: {}) for index in range(3)]
    model = FakePlanModel(
        _plan(
            *[
                _step(f"step-{index}", skill.name)
                for index, skill in enumerate(skills)
            ]
        )
    )

    result = _strategy(model).execute(
        context=_plan_context(
            *skills,
            budget=AutonomyBudget(10, 10, 10, 2, 1, 5000, 60),
        ),
        request=_plan_request(*(skill.name for skill in skills)),
    )

    assert result.status == "plan_invalid"
    assert result.output == {
        "reason": "Plan 步骤数超过预算：生成 3，最多允许 2",
        "actual_steps": 3,
        "max_plan_steps": 2,
    }


def test_plan_rejects_cycle() -> None:
    skill_a = _skill("a", lambda ctx, args: {})
    skill_b = _skill("b", lambda ctx, args: {})
    model = FakePlanModel(
        _plan(
            _step("a", "a", depends_on=["b"]),
            _step("b", "b", depends_on=["a"]),
        )
    )

    result = _strategy(model).execute(
        context=_plan_context(skill_a, skill_b), request=_plan_request("a", "b")
    )

    assert result.status == "plan_invalid"


def test_replan_cannot_add_unbound_skill() -> None:
    failing = _skill("order.lookup", lambda ctx, args: (_ for _ in ()).throw(RuntimeError("x")))
    model = FakePlanModel(
        _plan(_step("order", "order.lookup")),
        _plan(_step("admin", "admin.delete")),
    )

    result = _strategy(model).execute(
        context=_plan_context(failing), request=_plan_request("order.lookup")
    )

    assert result.status == "strategy_rejected"
    assert result.metrics["replans"] == 1


def test_plan_stops_at_replan_budget() -> None:
    failing = _skill("order.lookup", lambda ctx, args: (_ for _ in ()).throw(RuntimeError("x")))
    model = FakePlanModel(
        _plan(_step("order", "order.lookup")),
        _plan(_step("order", "order.lookup")),
    )
    budget = AutonomyBudget(10, 10, 10, 4, 1, 1000, 60)

    result = _strategy(model).execute(
        context=_plan_context(failing, budget=budget),
        request=_plan_request("order.lookup"),
    )

    assert result.status == "tool_failed"
    assert result.metrics["replans"] == 1


def test_replan_preserves_completed_side_effect() -> None:
    refund_calls: list[str] = []
    refund = _skill(
        "refund.apply",
        lambda ctx, args: refund_calls.append("called") or {"refund_id": "R-1"},
        orchestration=OrchestrationMode.WORKFLOW,
        tool_policy=ToolPolicy.SIDE_EFFECT,
    )
    failing = _skill("notify.send", lambda ctx, args: (_ for _ in ()).throw(RuntimeError("x")))
    recovered = _skill("notify.backup", lambda ctx, args: {"sent": True})
    model = FakePlanModel(
        _plan(
            _step("refund", "refund.apply"),
            _step("notify", "notify.send", depends_on=["refund"]),
        ),
        _plan(
            _step("refund", "refund.apply"),
            _step("notify_backup", "notify.backup", depends_on=["refund"]),
        ),
    )

    result = _strategy(model).execute(
        context=_plan_context(
            refund,
            failing,
            recovered,
            approved=("refund.apply",),
        ),
        request=_plan_request("refund.apply", "notify.send", "notify.backup"),
    )

    assert result.status == "completed"
    assert refund_calls == ["called"]
    assert result.output["frozen_steps"] == ["refund"]


def test_side_effect_plan_pauses_before_execution() -> None:
    calls: list[str] = []
    refund = _skill(
        "refund.apply",
        lambda ctx, args: calls.append("called") or {"refund_id": "R-1"},
        orchestration=OrchestrationMode.WORKFLOW,
        tool_policy=ToolPolicy.SIDE_EFFECT,
    )

    result = _strategy(FakePlanModel(_plan(_step("refund", "refund.apply")))).execute(
        context=_plan_context(refund), request=_plan_request("refund.apply")
    )

    assert result.status == "waiting_for_approval"
    assert calls == []
