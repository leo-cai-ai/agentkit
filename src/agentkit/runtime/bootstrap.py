"""Bootstrap helpers shared by the CLI and Flask console."""

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
from agentkit.core.contracts import AgentProfile
from agentkit.core.gateway import AgentGateway, build_checkpointer
from agentkit.core.idempotency import build_idempotency_store
from agentkit.core.migrations import run_storage_migrations
from agentkit.core.prompts import load_prompt_files
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.core.skill_store import SkillFileStore, attach_skill_packages
from agentkit.runtime.pack_registry import discover_packs

# Root that holds the editable config tree (tenants/ prompts/ skills/ data/).
# When running from a source checkout this is the repo root
# (src/agentkit/runtime/ -> repo). When the package is pip-installed (e.g. inside
# the Docker image) __file__ lives under site-packages, so parents[3] would point
# into the venv; AGENTKIT_ROOT lets the deployment pin the real config root
# (the Dockerfile/compose set it to /app).
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
    tenant_config: dict
    db_path: Path
    skill_store: SkillFileStore
    tenant_id: str
    chat_service: Any = None
    manifest: dict[str, Any] | None = None


def list_tenants() -> list[str]:
    """Return the available tenant ids (filenames of tenants/*.json), sorted."""
    if not TENANTS_DIR.is_dir():
        return []
    return sorted(path.stem for path in TENANTS_DIR.glob("*.json"))


def resolve_tenant_id(explicit: str | None = None) -> str:
    """Pick the tenant id: explicit arg > env var > default."""
    if explicit:
        return explicit
    return os.environ.get(TENANT_ENV_VAR) or DEFAULT_TENANT_ID


def load_tenant_config(tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    path = TENANTS_DIR / f"{tenant_id}.json"
    if not path.is_file():
        available = ", ".join(list_tenants()) or "(none)"
        raise FileNotFoundError(
            f"Unknown tenant '{tenant_id}'. Available tenants: {available}. "
            f"Create one with `agentkit new-tenant {tenant_id}`."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _runtime_manifest(*, tenant_id: str, tenant_config: dict[str, Any]) -> dict[str, Any]:
    tenant_path = TENANTS_DIR / f"{tenant_id}.json"
    prompts: dict[str, Any] = {}
    prompt_files = tenant_config.get("prompt_files", {})
    if isinstance(prompt_files, dict):
        for name, rel_path in sorted(prompt_files.items()):
            prompt_path = (AGENTKIT_ROOT / str(rel_path)).resolve()
            prompts[str(name)] = {
                "path": str(Path(str(rel_path)).as_posix()),
                "sha256": _sha256_file(prompt_path) if prompt_path.is_file() else "",
            }
    return {
        "agentkit_root": str(AGENTKIT_ROOT),
        "tenant_id": tenant_config.get("tenant_id", tenant_id),
        "tenant_selector": tenant_id,
        "tenant_config": {
            "path": str((TENANTS_DIR / f"{tenant_id}.json").relative_to(AGENTKIT_ROOT).as_posix()),
            "sha256": _sha256_file(tenant_path) if tenant_path.is_file() else "",
        },
        "enabled_domains": list(tenant_config.get("enabled_domains", [])),
        "prompt_files": prompts,
    }


def build_runtime(
    *,
    tenant_id: str | None = None,
    db_path: Path | None = None,
) -> AgentKitRuntime:
    resolved_tenant_id = resolve_tenant_id(tenant_id)
    if db_path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DATA_DIR / f"{resolved_tenant_id}.sqlite"
    tenant_config = load_tenant_config(resolved_tenant_id)
    manifest = _runtime_manifest(tenant_id=resolved_tenant_id, tenant_config=tenant_config)
    tenant_config["prompts"] = load_prompt_files(
        base_dir=AGENTKIT_ROOT,
        prompt_files=tenant_config.get("prompt_files", {}),
    )
    tenant_config["runtime_manifest"] = manifest

    agents = AgentRegistry()
    skills = SkillRegistry()
    tools = ToolRegistry()
    settings = get_settings()
    storage_backend = str(getattr(settings, "storage_backend", "sqlite")).lower()
    run_storage_migrations(settings, sqlite_path=db_path)
    audit: SQLiteAuditLog
    if storage_backend in ("postgres", "pg"):
        audit = PostgresAuditLog(
            settings,
            tenant_id=str(tenant_config.get("tenant_id") or resolved_tenant_id),
        )
    elif storage_backend in ("", "sqlite"):
        audit = SQLiteAuditLog(db_path)
    else:
        raise ValueError(
            f"Unsupported storage_backend: {storage_backend!r}. "
            "Supported backends: 'sqlite', 'postgres'."
        )

    # Load only the domain packs this tenant enabled. Packs are discovered at
    # runtime (in-repo scan + installed entry points), so adding a business
    # domain is a single pack plus an `enabled_domains` entry -- no edits here.
    available_packs = discover_packs()
    for domain in tenant_config.get("enabled_domains", []):
        register_pack = available_packs.get(domain)
        if register_pack is None:
            continue
        register_pack(
            agents=agents,
            skills=skills,
            tools=tools,
            tenant_config=tenant_config,
        )

    # Platform agents are business-agnostic and always available. The router is
    # allowed to dispatch to whatever skills the enabled packs registered.
    _register_platform_agents(agents=agents, skills=skills, tenant_config=tenant_config)

    skill_store = SkillFileStore(AGENTKIT_ROOT / "skills", display_root=AGENTKIT_ROOT)
    attach_skill_packages(skills=skills, store=skill_store)
    tenant_config["skill_catalog"] = [
        {
            "name": skill.name,
            "domain": skill.domain,
            "description": skill.description,
            "execution_mode": skill.execution_mode,
            "permissions": skill.permissions,
            "tools": skill.tools,
            "batch_key": skill.batch_key,
            "input_schema": skill.input_schema,
            "output_schema": skill.output_schema,
            "requires_approval": skill.name in tenant_config.get("approval_required_skills", []),
        }
        for skill in skills.all()
    ]

    checkpointer = build_checkpointer(
        mode=settings.approval_checkpointer,
        sqlite_path=db_path.with_name(f"{resolved_tenant_id}_checkpoints.sqlite"),
        settings=settings,
    )
    idempotency_store = build_idempotency_store(
        backend=storage_backend,
        tenant_id=tenant_config["tenant_id"],
        sqlite_path=db_path,
        settings=settings,
    )

    def artifact_store_factory(run_id: str):
        return build_artifact_store(
            backend=storage_backend,
            tenant_id=tenant_config["tenant_id"],
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
        tenant_id=tenant_config["tenant_id"],
        tenant_config=tenant_config,
        agents=agents,
        skills=skills,
        tools=tools,
        audit=audit,
        checkpointer=checkpointer,
        artifact_store_factory=artifact_store_factory,
        idempotency_store=idempotency_store,
    )
    chat_service = _build_chat_service(
        tenant_id=tenant_config["tenant_id"],
        tenant_config=tenant_config,
        db_path=db_path,
        agents=agents,
        audit=audit,
    )
    return AgentKitRuntime(
        gateway=gateway,
        tenant_config=tenant_config,
        db_path=db_path,
        skill_store=skill_store,
        tenant_id=resolved_tenant_id,
        chat_service=chat_service,
        manifest=manifest,
    )


def _record_persisted_artifact(
    *, audit: Any, run_id: str, backend: str, record: ArtifactRecord
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


def _build_chat_service(
    *,
    tenant_id: str,
    tenant_config: dict,
    db_path: Path,
    agents: AgentRegistry,
    audit: Any,
) -> Any:
    """Build the conversational-agent service (memory stack). Import-safe."""
    from agentkit.config import get_settings
    from agentkit.runtime.chat_service import ChatService

    return ChatService(
        tenant_id=tenant_id,
        tenant_config=tenant_config,
        db_path=db_path,
        agents=agents,
        audit=audit,
        settings=get_settings(),
    )


def _register_platform_agents(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tenant_config: dict,
) -> None:
    prompt_files = tenant_config.get("prompt_files", {})
    agents.register(
        AgentProfile(
            name="router",
            domain="platform",
            description="Business-agnostic LangGraph router and dispatcher.",
            allowed_skills=[skill.name for skill in skills.all()],
            allowed_tools=[],
            prompt_file=prompt_files.get("agents.router", ""),
        )
    )
    agents.register(
        AgentProfile(
            name="general",
            domain="platform",
            description="Runtime conversational fallback for platform questions.",
            allowed_skills=[],
            allowed_tools=[],
            prompt_file=prompt_files.get("agents.general", ""),
        )
    )
