"""Bootstrap helpers shared by the CLI demo and Flask console."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentkit.config import get_settings
from agentkit.core.audit import SQLiteAuditLog
from agentkit.core.contracts import AgentProfile
from agentkit.core.gateway import AgentGateway, build_checkpointer
from agentkit.core.prompts import load_prompt_files
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.core.skill_store import SkillFileStore, attach_skill_packages
from agentkit.runtime.pack_registry import discover_packs

DEMO_ROOT = Path(__file__).resolve().parents[3]  # repo root: src/agentkit/runtime/ -> repo
TENANTS_DIR = DEMO_ROOT / "tenants"
DATA_DIR = DEMO_ROOT / "data"
DEFAULT_TENANT_ID = "company_alpha"
TENANT_ENV_VAR = "AGENTKIT_TENANT_ID"


@dataclass(frozen=True)
class DemoRuntime:
    gateway: AgentGateway
    tenant_config: dict
    db_path: Path
    skill_store: SkillFileStore
    tenant_id: str
    chat_service: Any = None


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


def build_runtime(
    *,
    tenant_id: str | None = None,
    db_path: Path | None = None,
) -> DemoRuntime:
    resolved_tenant_id = resolve_tenant_id(tenant_id)
    if db_path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DATA_DIR / f"{resolved_tenant_id}.sqlite"
    tenant_config = load_tenant_config(resolved_tenant_id)
    tenant_config["prompts"] = load_prompt_files(
        base_dir=DEMO_ROOT,
        prompt_files=tenant_config.get("prompt_files", {}),
    )

    agents = AgentRegistry()
    skills = SkillRegistry()
    tools = ToolRegistry()
    audit = SQLiteAuditLog(db_path)

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

    skill_store = SkillFileStore(DEMO_ROOT / "skills", display_root=DEMO_ROOT)
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

    settings = get_settings()
    checkpointer = build_checkpointer(
        mode=settings.approval_checkpointer,
        sqlite_path=db_path.with_name(f"{resolved_tenant_id}_checkpoints.sqlite"),
    )
    gateway = AgentGateway(
        tenant_id=tenant_config["tenant_id"],
        tenant_config=tenant_config,
        agents=agents,
        skills=skills,
        tools=tools,
        audit=audit,
        checkpointer=checkpointer,
    )
    chat_service = _build_chat_service(
        tenant_id=tenant_config["tenant_id"],
        tenant_config=tenant_config,
        db_path=db_path,
        agents=agents,
        audit=audit,
    )
    return DemoRuntime(
        gateway=gateway,
        tenant_config=tenant_config,
        db_path=db_path,
        skill_store=skill_store,
        tenant_id=resolved_tenant_id,
        chat_service=chat_service,
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
