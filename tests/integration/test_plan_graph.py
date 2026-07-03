from langgraph.checkpoint.memory import MemorySaver

from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.plan import PlanExecuteStrategy
from agentkit.core.execution.registry import StrategyRegistry
from tests.unit.test_execution_strategies import _skill
from tests.unit.test_plan_strategy import (
    FakePlanModel,
    _plan,
    _plan_context,
    _plan_request,
    _step,
)


def test_plan_subgraph_checkpoints_each_scheduling_transition() -> None:
    checkpointer = MemorySaver()
    first = _skill("first", lambda ctx, args: {"value": 1})
    second = _skill("second", lambda ctx, args: {"value": 2})
    strategy = PlanExecuteStrategy(
        model=FakePlanModel(
            _plan(
                _step("first", "first"),
                _step("second", "second", depends_on=["first"]),
            )
        ),
        strategies=StrategyRegistry([DirectStrategy()]),
        checkpointer=checkpointer,
    )

    result = strategy.execute(
        context=_plan_context(first, second), request=_plan_request("first", "second")
    )

    config = {"configurable": {"thread_id": "r1:plan:test_agent"}}
    assert result.status == "completed"
    assert result.metrics["steps"] == 2
    assert len(list(checkpointer.list(config))) >= 6
