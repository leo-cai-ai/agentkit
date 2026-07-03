"""ExecutionStrategy 协议与单次运行上下文。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.core.artifacts import ArtifactStore
from agentkit.core.contracts import (
    AgentProfile,
    SkillContext,
    SkillDefinition,
    TaskRequest,
    ToolDefinition,
)

from .models import StrategyRequest, StrategyResult

AnswerHandler = Callable[["ExecutionContext", StrategyRequest], dict[str, Any]]


@dataclass(frozen=True)
class ExecutionContext:
    tenant_id: str
    run_id: str
    agent: AgentProfile
    request: TaskRequest
    skills: Mapping[str, SkillDefinition]
    tools: Mapping[str, ToolDefinition]
    tenant_config: dict[str, Any]
    artifacts: ArtifactStore
    invoker: Any = None
    answer_handler: AnswerHandler | None = None
    batch_size: int = 20
    max_concurrency: int = 8

    def skill(self, name: str) -> SkillDefinition:
        if name not in self.agent.allowed_skills:
            raise ValueError(f"Capability 未绑定到 Agent {self.agent.name}: {name}")
        try:
            return self.skills[name]
        except KeyError as exc:
            raise ValueError(f"Capability 未注册: {name}") from exc

    def skill_context(self, skill: SkillDefinition) -> SkillContext:
        scoped_tools = {name: self.tools[name] for name in skill.tools if name in self.tools}
        return SkillContext(
            tenant_id=self.tenant_id,
            tenant_config=self.tenant_config,
            tools=scoped_tools,
            request=self.request,
            invoker=self.invoker,
            artifacts=self.artifacts,
        )


class ExecutionStrategy(Protocol):
    name: str

    def execute(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
    ) -> StrategyResult: ...


__all__ = ["ExecutionContext", "ExecutionStrategy"]
