"""Small workflow runner for isolated multi-skill business flows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .contracts import SkillContext


@dataclass(frozen=True)
class WorkflowStepResult:
    step_name: str
    output: dict[str, Any]
    summary: str
    artifact: dict[str, Any] | None = None

    def compact(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step": self.step_name,
            "summary": self.summary,
        }
        if self.artifact:
            data["artifact"] = self.artifact
        return data


class WorkflowRunner:
    """Run workflow steps with per-step tool scope and artifact handoff."""

    def __init__(self, parent: SkillContext) -> None:
        self._parent = parent
        self._results: list[WorkflowStepResult] = []

    @property
    def results(self) -> list[WorkflowStepResult]:
        return list(self._results)

    def run_step(
        self,
        *,
        step_name: str,
        handler: Callable[[SkillContext, dict[str, Any]], dict[str, Any]],
        args: dict[str, Any],
        allowed_tools: list[str] | None = None,
        artifact_kind: str | None = None,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowStepResult:
        tool_names = allowed_tools if allowed_tools is not None else list(self._parent.tools)
        scoped_tools = {
            name: self._parent.tools[name] for name in tool_names if name in self._parent.tools
        }
        scoped_ctx = SkillContext(
            tenant_id=self._parent.tenant_id,
            tenant_config=self._parent.tenant_config,
            tools=scoped_tools,
            request=self._parent.request,
            invoker=self._parent.invoker,
            artifacts=self._parent.artifacts,
        )
        output = handler(scoped_ctx, args)
        step_summary = summary or str(output.get("summary") or output.get("campaign_summary") or "")
        artifact = None
        if artifact_kind and self._parent.artifacts is not None:
            artifact = self._parent.artifacts.put(
                kind=artifact_kind,
                payload=output,
                summary=step_summary,
                metadata={"step": step_name, **(metadata or {})},
            ).ref()
        result = WorkflowStepResult(
            step_name=step_name,
            output=output,
            summary=step_summary,
            artifact=artifact,
        )
        self._results.append(result)
        return result

    def compact_trace(self) -> list[dict[str, Any]]:
        return [result.compact() for result in self._results]


__all__ = ["WorkflowRunner", "WorkflowStepResult"]
