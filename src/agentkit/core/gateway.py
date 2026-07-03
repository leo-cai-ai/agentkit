"""UnifiedAgentGraph 的公开网关。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentkit.runtime.conversation_context import ConversationContextService
from agentkit.runtime.conversation_persistence import ConversationPersistenceService

from .artifacts import ArtifactStore
from .audit import InMemoryAuditLog, PostgresAuditLog, SQLiteAuditLog
from .contracts import TaskRequest, TaskResponse
from .execution.batch import BatchStrategy
from .execution.direct import DirectStrategy
from .execution.models import AutonomyBudget
from .execution.parallel import ParallelStrategy
from .execution.registry import StrategyRegistry
from .execution.selector import StrategySelector
from .execution.workflow import WorkflowStrategy
from .idempotency import IdempotencyStore
from .intent import IntentDecomposer
from .langgraph_agent import IntentResolver, UnifiedAgentGraph
from .registry import AgentRegistry, SkillRegistry, ToolRegistry
from .router import IntentRouter
from .tool_backends import ToolBackendRegistry


class AgentGateway:
    """所有 Agent 请求与审批恢复的唯一入口。"""

    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_selector: str,
        tenant_config: dict[str, Any],
        agents: AgentRegistry,
        skills: SkillRegistry,
        tools: ToolRegistry,
        audit: InMemoryAuditLog | SQLiteAuditLog | PostgresAuditLog,
        context_invoker: Any,
        checkpointer: Any = None,
        router: IntentRouter | None = None,
        selector: StrategySelector | None = None,
        strategies: StrategyRegistry | None = None,
        intent_resolver: IntentResolver | None = None,
        conversation_context: ConversationContextService | None = None,
        conversation_persistence: ConversationPersistenceService | None = None,
        artifact_store_factory: Callable[[str], ArtifactStore] | None = None,
        tool_backends: ToolBackendRegistry | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_selector = tenant_selector
        self._tenant_config = tenant_config
        self._agents = agents
        self._skills = skills
        self._tools = tools
        self._audit = audit
        self._context_invoker = context_invoker
        self._conversation_persistence = conversation_persistence
        checkpointer = checkpointer or build_checkpointer(mode="memory")
        router = router or IntentRouter(
            agents=agents,
            skills=skills,
            context_invoker=context_invoker,
            tenant_id=tenant_id,
            tenant_selector=tenant_selector,
        )
        selector = selector or StrategySelector(
            skills=skills,
            global_budget=AutonomyBudget(64, 128, 32, 32, 4, 200_000, 3600),
        )
        strategies = strategies or StrategyRegistry(
            [DirectStrategy(), WorkflowStrategy(), BatchStrategy(), ParallelStrategy()]
        )
        if intent_resolver is None:
            decomposer = IntentDecomposer(
                context_invoker=context_invoker,
                tenant_id=tenant_id,
                tenant_selector=tenant_selector,
            )
            intent_resolver = decomposer.decompose
        self._agent_graph = UnifiedAgentGraph(
            tenant_id=tenant_id,
            tenant_selector=tenant_selector,
            tenant_config=tenant_config,
            agents=agents,
            skills=skills,
            tools=tools,
            audit=audit,
            context_invoker=context_invoker,
            router=router,
            selector=selector,
            strategies=strategies,
            intent_resolver=intent_resolver,
            checkpointer=checkpointer,
            conversation_context=conversation_context,
            conversation_persistence=conversation_persistence,
            artifact_store_factory=artifact_store_factory,
            tool_backends=tool_backends,
            idempotency_store=idempotency_store,
        )

    def handle(self, request: TaskRequest) -> TaskResponse:
        return self._handle(request, create_conversation=True)

    def handle_delegated(self, request: TaskRequest) -> TaskResponse:
        """执行受 General Agent 委派的任务，不创建或写入业务 Agent 会话。"""
        return self._handle(request, create_conversation=False)

    def _handle(
        self, request: TaskRequest, *, create_conversation: bool
    ) -> TaskResponse:
        from .safety import REFUSAL_MESSAGE, build_safety_guard

        decision = build_safety_guard().inspect_input(request.text)
        if decision.action == "block":
            run_id = self._audit.start_run(
                tenant_id=self._tenant_id,
                user_id=request.user_id,
                text=request.text,
                agent_id=str(request.context.get("agent") or ""),
                parent_run_id=str(request.context.get("parent_run_id") or "") or None,
                conversation_id=(
                    str(
                        request.context.get("trace_conversation_id")
                        or request.context.get("conversation_id")
                        or ""
                    )
                    or None
                ),
            )
            self._audit.record(run_id, "safety_blocked", decision.to_audit())
            self._audit.record(run_id, "run_finished", {"status": "blocked"})
            return TaskResponse(
                status="blocked",
                output={"message": REFUSAL_MESSAGE},
                run_id=run_id,
                thread_id="",
                agent=str(request.context.get("agent") or ""),
                strategy="",
                conversation_id=str(request.context.get("conversation_id") or ""),
                governance={"safety": decision.to_audit()},
                audit_events=self._audit.events_for(run_id),
            )
        if (
            create_conversation
            and self._conversation_persistence is not None
            and not request.context.get("conversation_id")
        ):
            agent_id = str(request.context.get("agent") or "")
            conversation_id = self._conversation_persistence.create_conversation(
                tenant_id=self._tenant_id,
                agent_id=agent_id,
                user_id=request.user_id,
                title=request.text[:60],
            )
            request = replace(
                request,
                context={**request.context, "conversation_id": conversation_id},
            )
        return self._agent_graph.run(request, thread_id=str(uuid.uuid4()))

    def resume(
        self,
        thread_id: str,
        *,
        approved_skills: list[str] | tuple[str, ...] = (),
        rejected_skills: list[str] | tuple[str, ...] = (),
        decision_context: dict[str, Any] | None = None,
    ) -> TaskResponse:
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

    @property
    def context_invoker(self) -> Any:
        return self._context_invoker


def build_checkpointer(
    *,
    mode: str,
    sqlite_path: Path | None = None,
    settings: Any = None,
) -> Any:
    """构建 Memory、SQLite 或 PostgreSQL Checkpointer。"""

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
        connection = sqlite3.connect(str(sqlite_path), check_same_thread=False)
        sqlite_saver = SqliteSaver(connection)
        sqlite_saver.setup()
        return sqlite_saver
    if mode == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - 可选依赖
            raise RuntimeError("PostgreSQL Checkpointer 需要安装 agentkit[pg]") from exc
        from agentkit.core.pg import build_dsn, require_psycopg

        psycopg = require_psycopg()
        connection = psycopg.connect(
            build_dsn(settings),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        postgres_saver = PostgresSaver(connection)
        postgres_saver.setup()
        return postgres_saver
    if mode == "none":
        return None
    raise ValueError(f"不支持的 Checkpointer: {mode}")


__all__ = ["AgentGateway", "build_checkpointer"]
