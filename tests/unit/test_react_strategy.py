from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentkit.core.contracts import ToolDefinition
from agentkit.core.execution.models import (
    AutonomyBudget,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    StrategyRequest,
    ToolPolicy,
    ToolProvider,
    ToolRisk,
)
from agentkit.core.execution.react import ReactAction, ReactModelDecision, ReactStrategy
from tests.unit.test_execution_strategies import _context, _resolution, _skill


class FakeActionModel:
    def __init__(self, actions: list[ReactAction], *, tokens_per_call: int = 1) -> None:
        self._actions = list(actions)
        self._tokens = tokens_per_call
        self.calls: list[dict[str, Any]] = []

    def decide(self, **kwargs) -> ReactModelDecision:
        self.calls.append(kwargs)
        if not self._actions:
            raise AssertionError("FakeActionModel 没有剩余 Action")
        return ReactModelDecision(action=self._actions.pop(0), token_count=self._tokens)


class FakeClock:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def _tool_action(name: str, arguments: dict[str, Any]) -> ReactAction:
    return ReactAction(
        type="tool_call",
        tool_name=name,
        arguments=arguments,
        decision_summary="需要更多证据",
    )


def _final(answer: str) -> ReactAction:
    return ReactAction(type="final", answer=answer, evidence_refs=["e1"])


def _react_context(
    *,
    budget: AutonomyBudget | None = None,
    side_effect_calls: list[dict[str, Any]] | None = None,
):
    side_effect_calls = side_effect_calls if side_effect_calls is not None else []
    tools = {
        "web.search": ToolDefinition(
            name="web.search",
            domain="research",
            description="检索",
            handler=lambda args: {"items": [args["query"]]},
            provider=ToolProvider.PYTHON,
            risk=ToolRisk.READ_ONLY,
        ),
        "docs.open": ToolDefinition(
            name="docs.open",
            domain="research",
            description="打开文档",
            handler=lambda args: {"text": args["url"]},
            provider=ToolProvider.PYTHON,
            risk=ToolRisk.READ_ONLY,
        ),
        "refund.submit": ToolDefinition(
            name="refund.submit",
            domain="support",
            description="退款",
            handler=lambda args: side_effect_calls.append(args) or {"ok": True},
            provider=ToolProvider.PYTHON,
            risk=ToolRisk.SIDE_EFFECT,
        ),
    }
    skill = _skill("demo.one", lambda ctx, args: {})
    skill = replace(
        skill,
        execution=SkillExecutionPolicy(
            ReasoningStrategy.REACT,
            OrchestrationMode.SINGLE,
            ToolPolicy.READ_ONLY,
            True,
        ),
        tools=list(tools),
    )
    return replace(_context(skill), tools=tools, budget=budget)


def _request() -> StrategyRequest:
    return StrategyRequest("研究主题", {}, _resolution("demo.one"))


def test_react_selects_tools_until_final() -> None:
    model = FakeActionModel(
        [
            _tool_action("web.search", {"query": "agent frameworks"}),
            _tool_action("docs.open", {"url": "https://example.test/report"}),
            _final("结论"),
        ]
    )

    result = ReactStrategy(model=model).execute(context=_react_context(), request=_request())

    assert result.status == "completed"
    assert result.output["answer"] == "结论"
    assert result.metrics["iterations"] == 3
    assert result.metrics["tool_calls"] == 2
    assert len(result.artifacts) == 2


def test_react_stops_repeated_action() -> None:
    repeated = _tool_action("web.search", {"query": "same"})

    result = ReactStrategy(model=FakeActionModel([repeated, repeated])).execute(
        context=_react_context(), request=_request()
    )

    assert result.status == "no_progress"
    assert result.metrics["tool_calls"] == 1


def test_react_never_executes_side_effect() -> None:
    calls: list[dict[str, Any]] = []
    result = ReactStrategy(
        model=FakeActionModel([_tool_action("refund.submit", {"order_id": "O-1"})])
    ).execute(context=_react_context(side_effect_calls=calls), request=_request())

    assert result.status == "deferred_action"
    assert result.output["deferred_action"]["tool_name"] == "refund.submit"
    assert calls == []


@pytest.mark.parametrize(
    ("field", "metric"),
    [
        ("max_model_calls", "model_calls"),
        ("max_tool_calls", "tool_calls"),
        ("max_iterations", "iterations"),
        ("max_tokens", "token_count"),
    ],
)
def test_react_never_exceeds_discrete_budget(field: str, metric: str) -> None:
    values = {
        "max_model_calls": 10,
        "max_tool_calls": 10,
        "max_iterations": 10,
        "max_plan_steps": 1,
        "max_replans": 0,
        "max_tokens": 10,
        "timeout_seconds": 60,
    }
    values[field] = 1
    budget = AutonomyBudget(**values)
    model = FakeActionModel(
        [
            _tool_action("web.search", {"query": "one"}),
            _tool_action("docs.open", {"url": "two"}),
            _final("done"),
        ]
    )

    result = ReactStrategy(model=model).execute(
        context=_react_context(budget=budget), request=_request()
    )

    assert result.status == "budget_exhausted"
    assert result.metrics[metric] <= 1


def test_react_stops_when_deadline_expires() -> None:
    budget = AutonomyBudget(10, 10, 10, 1, 0, 100, 1)
    model = FakeActionModel([_tool_action("web.search", {"query": "one"}), _final("x")])

    result = ReactStrategy(model=model, clock=FakeClock([100.0, 100.0, 100.0, 101.1])).execute(
        context=_react_context(budget=budget), request=_request()
    )

    assert result.status == "budget_exhausted"


def test_react_rejects_tool_outside_skill_whitelist() -> None:
    model = FakeActionModel([_tool_action("unknown.tool", {})])

    result = ReactStrategy(model=model).execute(context=_react_context(), request=_request())

    assert result.status == "strategy_rejected"
