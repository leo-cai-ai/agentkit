"""统一 Agent Runtime 的启动器，供 CLI 与 Web 共用。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentkit.config import get_settings
from agentkit.core.artifacts import ArtifactRecord, build_artifact_store
from agentkit.core.audit import PostgresAuditLog, SQLiteAuditLog
from agentkit.core.context import (
    ContextAssembler,
    ContextDebugSampler,
    ContextInvocationService,
    ContextRegistry,
)
from agentkit.core.execution.batch import BatchStrategy
from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.llm_models import StructuredPlanModel, StructuredReactModel
from agentkit.core.execution.models import AutonomyBudget, ToolProvider
from agentkit.core.execution.parallel import ParallelStrategy
from agentkit.core.execution.plan import PlanExecuteStrategy
from agentkit.core.execution.protocol import ExecutionStrategy
from agentkit.core.execution.react import ReactStrategy
from agentkit.core.execution.registry import StrategyRegistry
from agentkit.core.execution.selector import StrategySelector
from agentkit.core.execution.workflow import WorkflowStrategy
from agentkit.core.gateway import AgentGateway, build_checkpointer
from agentkit.core.idempotency import build_idempotency_store
from agentkit.core.memory.embeddings import build_embedding_provider
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.retrieval import MemoryRetriever
from agentkit.core.memory.store import build_conversation_store
from agentkit.core.memory.summarizer import Summarizer
from agentkit.core.memory.vector_store import SqliteVectorStore, build_vector_store
from agentkit.core.migrations import run_storage_migrations
from agentkit.core.multi_agent import AgentDirectory, MultiAgentCoordinator
from agentkit.core.rag.service import build_knowledge_service
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.core.skill_store import SkillFileStore, attach_skill_packages
from agentkit.core.tool_backends import (
    McpToolBackend,
    PythonToolBackend,
    StdioMcpClient,
    ToolBackendRegistry,
)
from agentkit.runtime.conversation_context import ConversationContextService
from agentkit.runtime.conversation_deletion import ConversationDeletionService
from agentkit.runtime.conversation_persistence import (
    ConversationPersistenceService,
    ExtractingMemoryWriter,
)
from agentkit.runtime.conversation_projection import ConversationProjectionService
from agentkit.runtime.conversation_recovery import ConversationRecoveryService
from agentkit.runtime.conversation_runs import ConversationRunStateResolver
from agentkit.runtime.declarative_catalog import (
    load_catalog,
    register_catalog,
    resolve_enabled_agent_ids,
)
from agentkit.runtime.ocr import build_configured_ocr_provider

AGENTKIT_ROOT = Path(
    os.environ.get("AGENTKIT_ROOT") or Path(__file__).resolve().parents[3]
).resolve()
TENANTS_DIR = AGENTKIT_ROOT / "tenants"
DATA_DIR = AGENTKIT_ROOT / "data"
DEFAULT_TENANT_ID = "company_alpha"
TENANT_ENV_VAR = "AGENTKIT_TENANT_ID"


@dataclass(frozen=True)
class AgentKitRuntime:
    gateway: AgentGateway
    tenant_config: dict[str, Any]
    db_path: Path
    skill_store: SkillFileStore
    tenant_id: str
    strategy_names: tuple[str, ...]
    conversations: Any
    contexts: ContextRegistry
    context_invoker: ContextInvocationService
    conversation_deletion: ConversationDeletionService
    conversation_runs: ConversationRunStateResolver
    conversation_projection: ConversationProjectionService
    conversation_recovery: ConversationRecoveryService
    manifest: dict[str, Any] | None = None
    # 迁移期间保留属性形状，但不再存在第二套 Chat Runtime。
    chat_service: MultiAgentCoordinator | None = None


def list_tenants() -> list[str]:
    """返回可用租户 ID。"""
    if not TENANTS_DIR.is_dir():
        return []
    return sorted(path.stem for path in TENANTS_DIR.glob("*.json"))


def resolve_tenant_id(explicit: str | None = None) -> str:
    """按显式参数、环境变量、默认值的顺序选择租户。"""
    if explicit:
        return explicit
    return os.environ.get(TENANT_ENV_VAR) or DEFAULT_TENANT_ID


def load_tenant_config(tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
    path = TENANTS_DIR / f"{tenant_id}.json"
    if not path.is_file():
        available = ", ".join(list_tenants()) or "(none)"
        raise FileNotFoundError(
            f"未知租户 {tenant_id!r}，可用租户: {available}。"
            f"可使用 `agentkit new-tenant {tenant_id}` 创建。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_manifest(
    *,
    tenant_id: str,
    tenant_config: dict[str, Any],
    contexts: ContextRegistry,
) -> dict[str, Any]:
    tenant_path = TENANTS_DIR / f"{tenant_id}.json"
    return {
        "agentkit_root": str(AGENTKIT_ROOT),
        "tenant_id": tenant_config.get("tenant_id", tenant_id),
        "tenant_selector": tenant_id,
        "tenant_config": {
            "path": str(tenant_path.relative_to(AGENTKIT_ROOT).as_posix()),
            "sha256": _sha256_file(tenant_path) if tenant_path.is_file() else "",
        },
        "enabled_agents": list(tenant_config.get("enabled_agents", [])),
        "contexts": {
            "manifest_hash": contexts.manifest_hash,
            "packs": contexts.manifest(),
        },
    }


def build_runtime(
    *,
    tenant_id: str | None = None,
    db_path: Path | None = None,
) -> AgentKitRuntime:
    """从声明式目录编译唯一 Runtime，不注册隐式平台 Agent。"""
    resolved_tenant_id = resolve_tenant_id(tenant_id)
    if db_path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DATA_DIR / f"{resolved_tenant_id}.sqlite"
    tenant_config = load_tenant_config(resolved_tenant_id)
    tenant_key = str(tenant_config.get("tenant_id") or resolved_tenant_id)
    settings = get_settings()
    storage_backend = str(getattr(settings, "storage_backend", "sqlite")).lower()
    run_storage_migrations(settings, sqlite_path=db_path)
    audit: PostgresAuditLog | SQLiteAuditLog
    if storage_backend in {"postgres", "pg"}:
        audit = PostgresAuditLog(settings, tenant_id=tenant_key)
    elif storage_backend in {"", "sqlite"}:
        audit = SQLiteAuditLog(db_path)
    else:
        raise ValueError(f"不支持的 storage_backend: {storage_backend!r}")

    context_registry = ContextRegistry(
        root=AGENTKIT_ROOT / "contexts",
        tenant_selector=resolved_tenant_id,
        overrides=dict(tenant_config.get("context_overrides") or {}),
        global_token_limit=settings.llm_context_window_tokens,
    )
    debug_sampler = (
        ContextDebugSampler()
        if settings.runtime_environment == "development" and settings.context_debug_rendered_enabled
        else None
    )
    context_invoker = ContextInvocationService(
        assembler=ContextAssembler(context_registry),
        audit=audit,
        model_label=settings.openai_model or settings.llm_provider,
        debug_sampler=debug_sampler,
    )
    manifest = _runtime_manifest(
        tenant_id=resolved_tenant_id,
        tenant_config=tenant_config,
        contexts=context_registry,
    )
    tenant_config["runtime_manifest"] = manifest

    agents = AgentRegistry()
    skills = SkillRegistry()
    tools = ToolRegistry()
    global_budget = build_global_budget(settings)
    catalog = load_catalog(AGENTKIT_ROOT, global_budget=global_budget)
    enabled_agent_ids = resolve_enabled_agent_ids(catalog, tenant_config)
    register_catalog(
        catalog,
        enabled_agent_ids=enabled_agent_ids,
        agents=agents,
        skills=skills,
        tools=tools,
        tenant_config=tenant_config,
    )

    skill_store = SkillFileStore(AGENTKIT_ROOT / "skills", display_root=AGENTKIT_ROOT)
    attach_skill_packages(skills=skills, store=skill_store)
    tenant_config["skill_catalog"] = [
        {
            "name": skill.name,
            "domain": skill.domain,
            "description": skill.description,
            "reasoning": skill.execution.reasoning.value,
            "orchestration": skill.execution.orchestration.value,
            "tool_policy": skill.execution.tool_policy.value,
            "permissions": skill.permissions,
            "tools": skill.tools,
            "batch_key": skill.batch_key,
            "input_schema": skill.input_schema,
            "output_schema": skill.output_schema,
        }
        for skill in skills.all()
    ]

    checkpointer = build_checkpointer(
        mode=settings.approval_checkpointer,
        sqlite_path=db_path.with_name(f"{resolved_tenant_id}_checkpoints.sqlite"),
        settings=settings,
    )
    strategies = _build_strategies(checkpointer)
    conversation_store = build_conversation_store(settings, db_path)
    conversation_projection = ConversationProjectionService(
        store=conversation_store,
        audit=audit,
    )
    conversation_runs = ConversationRunStateResolver(
        audit=audit,
        timeout_seconds=float(settings.autonomy_timeout_seconds),
    )
    embeddings = build_embedding_provider(settings)
    vector_store = build_vector_store(settings, conversation_store)
    memory = MemoryRetriever(vector_store=vector_store, embeddings=embeddings)
    conversation_deletion = ConversationDeletionService(
        store=conversation_store,
        audit=audit,
        resolver=conversation_runs,
        external_memory_store=(
            None if isinstance(vector_store, SqliteVectorStore) else vector_store
        ),
    )
    knowledge = (
        build_knowledge_service(
            settings,
            tenant_id=tenant_key,
            tenant_selector=resolved_tenant_id,
            context_invoker=context_invoker,
            embeddings=embeddings,
            ocr_provider=build_configured_ocr_provider(settings),
        )
        if bool(getattr(settings, "rag_enabled", False))
        else None
    )
    conversation_context = ConversationContextService(
        store=conversation_projection,
        memory_reader=memory,
        knowledge_service=knowledge,
    )
    conversation_persistence = ConversationPersistenceService(
        store=conversation_store,
        projection=conversation_projection,
        memory_writer=ExtractingMemoryWriter(
            extractor=MemoryExtractor(
                context_invoker=context_invoker,
                tenant_selector=resolved_tenant_id,
            ),
            retriever=memory,
        ),
        summarizer=Summarizer(
            context_invoker=context_invoker,
            tenant_selector=resolved_tenant_id,
        ),
        audit=audit,
    )
    idempotency_store = build_idempotency_store(
        backend=storage_backend,
        tenant_id=tenant_key,
        sqlite_path=db_path,
        settings=settings,
    )

    def artifact_store_factory(run_id: str):
        return build_artifact_store(
            backend=storage_backend,
            tenant_id=tenant_key,
            run_id=run_id,
            sqlite_path=db_path,
            settings=settings,
            max_payload_bytes=settings.artifact_max_payload_bytes,
            on_write=lambda record: _record_persisted_artifact(
                audit=audit,
                run_id=run_id,
                backend=storage_backend,
                record=record,
            ),
        )

    gateway = AgentGateway(
        tenant_id=tenant_key,
        tenant_selector=resolved_tenant_id,
        tenant_config=tenant_config,
        agents=agents,
        skills=skills,
        tools=tools,
        audit=audit,
        context_invoker=context_invoker,
        checkpointer=checkpointer,
        selector=StrategySelector(skills=skills, global_budget=global_budget),
        strategies=strategies,
        conversation_context=conversation_context,
        conversation_persistence=conversation_persistence,
        artifact_store_factory=artifact_store_factory,
        tool_backends=_build_tool_backends(tools=tools, tenant_config=tenant_config),
        idempotency_store=idempotency_store,
    )
    directory = AgentDirectory(
        agents=agents,
        config=dict(tenant_config.get("agent_directory") or {}),
    )
    chat_service = MultiAgentCoordinator(
        tenant_id=tenant_key,
        tenant_selector=resolved_tenant_id,
        directory=directory,
        gateway=gateway,
        audit=audit,
        context_invoker=context_invoker,
        conversation_context=conversation_context,
        conversation_persistence=conversation_persistence,
        conversation_projection=conversation_projection,
        conversation_store=conversation_store,
    )
    conversation_recovery = ConversationRecoveryService(
        store=conversation_store,
        coordinator=chat_service,
        audit=audit,
    )
    conversation_recovery.reconcile(tenant_id=tenant_key)
    strategy_names = ("direct", "workflow", "batch", "parallel", "react", "plan_execute")
    return AgentKitRuntime(
        gateway=gateway,
        tenant_config=tenant_config,
        db_path=db_path,
        skill_store=skill_store,
        tenant_id=resolved_tenant_id,
        strategy_names=strategy_names,
        conversations=conversation_store,
        contexts=context_registry,
        context_invoker=context_invoker,
        conversation_deletion=conversation_deletion,
        conversation_runs=conversation_runs,
        conversation_projection=conversation_projection,
        conversation_recovery=conversation_recovery,
        manifest=manifest,
        chat_service=chat_service,
    )


def _build_strategies(checkpointer: Any) -> StrategyRegistry:
    direct = DirectStrategy()
    workflow = WorkflowStrategy()
    batch = BatchStrategy()
    parallel = ParallelStrategy()
    react = ReactStrategy(model=StructuredReactModel(), checkpointer=checkpointer)
    base: list[ExecutionStrategy] = [direct, workflow, batch, parallel, react]
    step_strategies = StrategyRegistry(base)
    plan = PlanExecuteStrategy(
        model=StructuredPlanModel(),
        strategies=step_strategies,
        checkpointer=checkpointer,
    )
    return StrategyRegistry([*base, plan])


def build_global_budget(settings: Any) -> AutonomyBudget:
    """把部署配置编译为 Agent/Skill 不可超过的全局预算。"""
    return AutonomyBudget(
        max_model_calls=int(settings.autonomy_max_model_calls),
        max_tool_calls=int(settings.autonomy_max_tool_calls),
        max_iterations=int(settings.autonomy_max_iterations),
        max_plan_steps=int(settings.autonomy_max_plan_steps),
        max_replans=int(settings.autonomy_max_replans),
        max_tokens=int(settings.autonomy_max_tokens),
        timeout_seconds=float(settings.autonomy_timeout_seconds),
    )


def _build_tool_backends(
    *,
    tools: ToolRegistry,
    tenant_config: dict[str, Any],
) -> ToolBackendRegistry:
    backends: dict[ToolProvider, Any] = {ToolProvider.PYTHON: PythonToolBackend()}
    mcp_tool_servers = {
        tool.mcp_server for tool in tools.all() if tool.provider is ToolProvider.MCP
    }
    if mcp_tool_servers:
        configured = tenant_config.get("mcp_servers", {})
        if not isinstance(configured, dict):
            raise ValueError("mcp_servers 必须是对象")
        clients: dict[str, StdioMcpClient] = {}
        for server_name in sorted(str(name) for name in mcp_tool_servers if name):
            raw = configured.get(server_name)
            if not isinstance(raw, dict) or not raw.get("command"):
                raise ValueError(f"MCP Server 未配置: {server_name}")
            transport = str(raw.get("transport") or "stdio")
            if transport != "stdio":
                raise ValueError(f"当前仅支持 stdio MCP: {server_name}")
            clients[server_name] = StdioMcpClient(
                command=str(raw["command"]),
                args=[str(item) for item in raw.get("args", [])],
            )
        backends[ToolProvider.MCP] = McpToolBackend(clients)
    return ToolBackendRegistry(backends)


def _record_persisted_artifact(
    *,
    audit: Any,
    run_id: str,
    backend: str,
    record: ArtifactRecord,
) -> None:
    audit.record(run_id, "artifact_written", record.ref())
    audit.record(
        run_id,
        "artifact_persisted",
        {
            "artifact_id": record.artifact_id,
            "kind": record.kind,
            "payload_sha256": record.payload_sha256,
            "payload_bytes": record.payload_bytes,
            "backend": backend,
        },
    )


__all__ = [
    "AGENTKIT_ROOT",
    "AgentKitRuntime",
    "build_global_budget",
    "build_runtime",
    "list_tenants",
    "load_tenant_config",
    "resolve_tenant_id",
]
