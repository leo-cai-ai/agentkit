"""Lifecycle hooks for integrating enterprise-specific behavior."""

from __future__ import annotations

from typing import Any

from .contracts import RouteDecision, TaskPlan, TaskRequest


class AgentLifecycleHooks:
    """No-op hook surface for tenant or product-specific extensions.

    A real deployment can subclass this class to add tracing, request shaping,
    budget checks, tenant enrichment, custom observability, or event publishing
    without changing the LangGraph topology.
    """

    def on_run_started(self, *, run_id: str, request: TaskRequest) -> None:
        pass

    def before_route(self, *, run_id: str, request: TaskRequest) -> None:
        pass

    def after_route(self, *, run_id: str, request: TaskRequest, route: RouteDecision) -> None:
        pass

    def after_plan(self, *, run_id: str, request: TaskRequest, plan: TaskPlan) -> None:
        pass

    def after_execute(self, *, run_id: str, request: TaskRequest, output: dict[str, Any]) -> None:
        pass

    def on_run_finished(self, *, run_id: str, request: TaskRequest, output: dict[str, Any]) -> None:
        pass
