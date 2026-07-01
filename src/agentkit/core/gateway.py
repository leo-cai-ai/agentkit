"""Public gateway that exposes the LangGraph enterprise agent."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .audit import InMemoryAuditLog, PostgresAuditLog, SQLiteAuditLog
from .contracts import RouteDecision, TaskPlan, TaskRequest, TaskResponse
from .executor import PlanExecutor
from .governance import HumanApprovalGate, OutputReviewer, PlanReviewer
from .hooks import AgentLifecycleHooks
from .idempotency import IdempotencyStore
from .input_resolution import SkillInputResolver
from .intent import IntentDecomposer
from .langgraph_agent import EnterpriseAgentGraph
from .planner import Planner
from .policy import PolicyGuard
from .prompt_library import PromptLibrary
from .registry import AgentRegistry, SkillRegistry, ToolRegistry
from .router import IntentRouter

_UNSET = object()


class AgentGateway:
    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_config: dict,
        agents: AgentRegistry,
        skills: SkillRegistry,
        tools: ToolRegistry,
        audit: InMemoryAuditLog | SQLiteAuditLog | PostgresAuditLog,
        checkpointer: Any = _UNSET,
        fastpath: bool | None = None,
        combined_intent_route: bool | None = None,
        artifact_store_factory: Callable[[str], ArtifactStore] | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_config = tenant_config
        self._agents = agents
        self._skills = skills
        self._tools = tools
        self._audit = audit
        prompt_library = PromptLibrary.from_tenant_config(tenant_config)
        self._intent_decomposer = IntentDecomposer(
            tenant_config=tenant_config,
            prompt_library=prompt_library,
        )
        self._router = IntentRouter(
            tenant_config=tenant_config,
            agents=agents,
            skills=skills,
            prompt_library=prompt_library,
        )
        self._planner = Planner(tenant_config=tenant_config, skills=skills)
        self._input_resolver = SkillInputResolver(
            tenant_config=tenant_config,
            skills=skills,
            prompt_library=prompt_library,
        )
        self._executor = PlanExecutor(
            tenant_id=tenant_id,
            tenant_config=tenant_config,
            skills=skills,
            tools=tools,
            policy=PolicyGuard(tenant_config),
            audit=audit,
            prompt_library=prompt_library,
            artifact_store_factory=artifact_store_factory,
            idempotency_store=idempotency_store,
        )
        self._agent_graph = EnterpriseAgentGraph(
            tenant_id=tenant_id,
            tenant_config=tenant_config,
            intent_decomposer=self._intent_decomposer,
            router=self._router,
            input_resolver=self._input_resolver,
            planner=self._planner,
            executor=self._executor,
            audit=audit,
            plan_reviewer=PlanReviewer(tenant_config, prompt_library=prompt_library),
            approval_gate=HumanApprovalGate(tenant_config, prompt_library=prompt_library),
            output_reviewer=OutputReviewer(tenant_config, prompt_library=prompt_library),
            hooks=AgentLifecycleHooks(),
            checkpointer=(_build_checkpointer() if checkpointer is _UNSET else checkpointer),
            fastpath=(_fastpath_enabled() if fastpath is None else fastpath),
            combiner=self._build_combiner(
                tenant_config=tenant_config,
                prompt_library=prompt_library,
                enabled=combined_intent_route,
            ),
        )

    def _build_combiner(
        self,
        *,
        tenant_config: dict,
        prompt_library: PromptLibrary,
        enabled: bool | None,
    ) -> Any:
        if (_combined_enabled() if enabled is None else enabled) is not True:
            return None
        from .intent_route import CombinedIntentRouter

        return CombinedIntentRouter(
            intent_decomposer=self._intent_decomposer,
            router=self._router,
            tenant_config=tenant_config,
            prompt_library=prompt_library,
        )

    def handle(self, request: TaskRequest) -> TaskResponse:
        # Each task runs on its own checkpoint thread; a thread that pauses for
        # human approval is resumed in-place via ``resume``.
        from .cost import cost_tracking
        from .safety import build_safety_guard
        from .tracing import span

        # Content-safety input gate runs before the graph: a blocked request is
        # refused without any LLM call; a flagged request is annotated + audited.
        guard = build_safety_guard()
        decision = guard.inspect_input(request.text)
        if decision.action == "block":
            return self._blocked_response(request, decision)
        if decision.findings:
            request = replace(
                request,
                context={**request.context, "safety": decision.to_audit()},
            )

        with (
            span("agent.handle", **{"agentkit.tenant_id": self._tenant_id}),
            cost_tracking(self._audit),
        ):
            return self._agent_graph.run(request, thread_id=str(uuid.uuid4()))

    def _blocked_response(self, request: TaskRequest, decision: Any) -> TaskResponse:
        """Build an audited refusal for a request blocked by content safety."""
        from .safety import REFUSAL_MESSAGE

        audit = decision.to_audit()
        run_id = self._audit.start_run(
            tenant_id=self._tenant_id, user_id=request.user_id, text=request.text
        )
        self._audit.record(run_id, "safety_blocked", audit)
        self._audit.record(run_id, "run_finished", {"status": "blocked"})
        output = {
            "status": "blocked",
            "run_id": run_id,
            "final": {"message": REFUSAL_MESSAGE},
            "safety": audit,
        }
        plan = TaskPlan(
            route=RouteDecision(skill_name=None, reason="blocked by content safety"),
            steps=[],
            warnings=["blocked by content safety"],
        )
        return TaskResponse(output=output, plan=plan, audit_events=self._audit.events_for(run_id))

    def resume(
        self,
        thread_id: str,
        *,
        approved_skills: list[str] | tuple[str, ...] = (),
        rejected_skills: list[str] | tuple[str, ...] = (),
        decision_context: dict[str, Any] | None = None,
    ) -> TaskResponse:
        from .cost import cost_tracking
        from .tracing import span

        with (
            span("agent.resume", **{"agentkit.tenant_id": self._tenant_id}),
            cost_tracking(self._audit),
        ):
            return self._agent_graph.resume(
                thread_id,
                approved_skills=approved_skills,
                rejected_skills=rejected_skills,
                decision_context=decision_context,
            )

    @property
    def agents(self) -> AgentRegistry:
        return self._agents

    @property
    def skills(self) -> SkillRegistry:
        return self._skills

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    @property
    def audit(self) -> InMemoryAuditLog | SQLiteAuditLog | PostgresAuditLog:
        return self._audit


def build_checkpointer(*, mode: str, sqlite_path: Path | None = None, settings: Any = None) -> Any:
    """Build an approval checkpointer.

    - ``memory``: in-process saver (resume works within one process only).
    - ``sqlite``: on-disk saver so paused approvals survive restarts and are
      resumable across processes/workers. Falls back to in-memory when no
      ``sqlite_path`` is supplied (e.g. lightweight direct construction).
    - ``postgres``: Postgres-backed saver using the configured PG connection.
    - ``none``: disabled (waiting output + protected full resubmit path).
    """
    if mode == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    if mode == "sqlite":
        if sqlite_path is None:
            from langgraph.checkpoint.memory import InMemorySaver

            return InMemorySaver()
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the Flask/gunicorn worker pool resumes from
        # threads other than the one that created the connection.
        conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)
        sqlite_saver = SqliteSaver(conn)
        sqlite_saver.setup()
        return sqlite_saver
    if mode == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - requires optional extra
            raise RuntimeError(
                "Postgres approval checkpointing requires the PostgreSQL extra. "
                "Install with: pip install 'agentkit[pg]'"
            ) from exc

        from agentkit.core.pg import build_dsn, require_psycopg

        psycopg = require_psycopg()
        conn = psycopg.connect(
            build_dsn(settings),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        postgres_saver = PostgresSaver(conn)
        postgres_saver.setup()
        return postgres_saver
    return None


def _build_checkpointer() -> Any:
    """Default checkpointer from settings (no path -> sqlite falls back to memory)."""
    try:
        from agentkit.config import get_settings

        mode = getattr(get_settings(), "approval_checkpointer", "memory")
    except Exception:  # noqa: BLE001 - settings optional in lightweight tests
        mode = "memory"
    return build_checkpointer(mode=mode)


def _fastpath_enabled() -> bool:
    """Read the deterministic fast-path flag from settings (default off)."""
    try:
        from agentkit.config import get_settings

        return bool(getattr(get_settings(), "deterministic_fastpath", False))
    except Exception:  # noqa: BLE001 - settings optional in lightweight tests
        return False


def _combined_enabled() -> bool:
    """Read the combined intent+route flag from settings (default off)."""
    try:
        from agentkit.config import get_settings

        return bool(getattr(get_settings(), "combined_intent_route", False))
    except Exception:  # noqa: BLE001 - settings optional in lightweight tests
        return False
