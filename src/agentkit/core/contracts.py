"""Shared contracts for the generic enterprise-agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    AutonomyLimits,
    ExecutionStrategyName,
    SkillExecutionPolicy,
)

IntentType = Literal[
    "business_task",
    "platform_question",
    "approval_decision",
    "chit_chat",
    "unknown",
]
IntentTargetKind = Literal["business_skill", "platform_handler", "none"]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class TaskRequest:
    user_id: str
    roles: list[str]
    text: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntentFrame:
    raw_text: str
    language: str
    intent_type: IntentType
    goal: str
    boundaries: dict[str, Any]
    entities: dict[str, Any]
    target: dict[str, Any]
    confidence: Confidence = "medium"
    clarification: str = ""
    signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentProfile:
    name: str
    domain: str
    description: str
    allowed_skills: list[str]
    execution_policy: AgentExecutionPolicy
    autonomy_budget: AutonomyBudget
    model: str = "default"
    max_tokens: int = 100_000
    prompt_file: str = ""
    # Agent 的上下文边界由声明式 agent.md 提供；平台级 Agent 可保留空策略。
    context_policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    domain: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: list[str]
    execution: SkillExecutionPolicy
    autonomy: AutonomyLimits
    tools: list[str]
    handler: Callable[[SkillContext, dict[str, Any]], dict[str, Any]]
    batch_key: str | None = None
    keywords: list[str] = field(default_factory=list)
    skill_folder: str = ""
    skill_file: str = ""
    skill_instructions: str = ""
    skill_resources: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    domain: str
    description: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    supports_batch: bool = False
    # Connector-grade execution metadata (consumed by the ToolExecutor):
    # - idempotent: safe to retry on transient failure without duplicate effects.
    # - timeout_seconds: per-tool timeout override (None -> use the global default).
    idempotent: bool = False
    timeout_seconds: float | None = None


@dataclass
class SkillContext:
    tenant_id: str
    tenant_config: dict[str, Any]
    tools: dict[str, ToolDefinition]
    request: TaskRequest
    # Optional hardened invoker (timeout/retry/idempotency/audit/tracing). When
    # absent (e.g. unit tests building a context directly), tool calls fall back
    # to invoking the handler in-process with no extra governance.
    invoker: Any = None
    # Optional run-scoped artifact store. Workflow skills use it to hand off
    # large step outputs by reference instead of putting everything in prompt
    # context.
    artifacts: Any = None

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools[name]
        if self.invoker is not None:
            return self.invoker.call(tool, args)
        return tool.handler(args)

    def write_artifact(
        self,
        *,
        kind: str,
        payload: Any,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if self.artifacts is None:
            return None
        return self.artifacts.put(
            kind=kind,
            payload=payload,
            summary=summary,
            metadata=metadata,
        ).ref()


@dataclass(frozen=True)
class RouteDecision:
    skill_name: str | None
    reason: str
    confidence: Confidence = "medium"


@dataclass(frozen=True)
class PlanStep:
    step_id: int
    skill_name: str
    mode: ExecutionStrategyName
    args: dict[str, Any]
    depends_on: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class TaskPlan:
    route: RouteDecision
    steps: list[PlanStep]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskResponse:
    output: dict[str, Any]
    plan: TaskPlan
    audit_events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "plan": asdict(self.plan),
            "audit_events": self.audit_events,
        }
