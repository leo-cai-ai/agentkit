"""统一 Runtime 使用的不可变执行模型。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Literal


class ReasoningStrategy(StrEnum):
    DIRECT = "direct"
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


class OrchestrationMode(StrEnum):
    SINGLE = "single"
    WORKFLOW = "workflow"
    BATCH = "batch"
    PARALLEL = "parallel"


class ToolPolicy(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    GOVERNED = "governed"
    SIDE_EFFECT = "side_effect"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    GOVERNED = "governed"
    SIDE_EFFECT = "side_effect"


class ToolProvider(StrEnum):
    PYTHON = "python"
    MCP = "mcp"


class ExecutionStrategyName(StrEnum):
    DIRECT = "direct"
    WORKFLOW = "workflow"
    BATCH = "batch"
    PARALLEL = "parallel"
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


_POSITIVE_BUDGET_FIELDS = {
    "max_model_calls",
    "max_tool_calls",
    "max_iterations",
    "max_plan_steps",
    "max_tokens",
    "timeout_seconds",
}


@dataclass(frozen=True)
class AutonomyBudget:
    max_model_calls: int
    max_tool_calls: int
    max_iterations: int
    max_plan_steps: int
    max_replans: int
    max_tokens: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in _POSITIVE_BUDGET_FIELDS and value <= 0:
                raise ValueError(f"{item.name} 必须大于 0")
            if item.name == "max_replans" and value < 0:
                raise ValueError("max_replans 不能小于 0")

    def restrict(self, other: AutonomyBudget) -> AutonomyBudget:
        """逐项采用更严格的完整预算。"""

        return AutonomyBudget(
            **{
                item.name: min(getattr(self, item.name), getattr(other, item.name))
                for item in fields(self)
            }
        )


@dataclass(frozen=True)
class AutonomyLimits:
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_iterations: int | None = None
    max_plan_steps: int | None = None
    max_replans: int | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None:
                continue
            if item.name in _POSITIVE_BUDGET_FIELDS and value <= 0:
                raise ValueError(f"{item.name} 必须大于 0")
            if item.name == "max_replans" and value < 0:
                raise ValueError("max_replans 不能小于 0")

    def apply_to(self, budget: AutonomyBudget) -> AutonomyBudget:
        """将 Skill 的可选上限应用到 Agent 有效预算。"""

        values: dict[str, int | float] = {}
        for item in fields(budget):
            current = getattr(budget, item.name)
            limit = getattr(self, item.name)
            values[item.name] = current if limit is None else min(current, limit)
        return AutonomyBudget(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class AgentExecutionPolicy:
    default_strategy: ExecutionStrategyName
    allowed_strategies: tuple[ExecutionStrategyName, ...]
    allow_dynamic_selection: bool = False
    allow_side_effects: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_strategies:
            raise ValueError("allowed_strategies 不能为空")
        if self.default_strategy not in self.allowed_strategies:
            raise ValueError("default_strategy 必须包含在 allowed_strategies 中")


@dataclass(frozen=True)
class SkillExecutionPolicy:
    reasoning: ReasoningStrategy
    orchestration: OrchestrationMode
    tool_policy: ToolPolicy
    allow_dynamic_selection: bool = False


@dataclass(frozen=True)
class ComplexityAssessment:
    candidate_skills: tuple[str, ...] = ()
    estimated_steps: int = 1
    has_dependencies: bool = False
    needs_dynamic_observation: bool = False
    has_side_effects: bool = False
    batch_items: int = 0
    independent_skills: int = 0
    missing_information: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"


@dataclass(frozen=True)
class CapabilityResolution:
    response_mode: Literal["answer", "skill", "multi_skill"]
    primary_skill: str | None
    candidate_skills: tuple[str, ...]
    reason: str
    confidence: Literal["high", "medium", "low"]
    complexity: ComplexityAssessment


@dataclass(frozen=True)
class StrategyRequest:
    goal: str
    arguments: dict[str, Any]
    capability: CapabilityResolution


@dataclass(frozen=True)
class StrategyResult:
    status: str
    output: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...] = ()
    metrics: dict[str, int | float] = field(default_factory=dict)
