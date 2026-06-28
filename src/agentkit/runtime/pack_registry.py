"""Discover business domain packs without hardcoding imports.

A domain pack is a module exposing two attributes:

* ``DOMAIN``: the domain string a tenant turns on via ``enabled_domains``.
* ``register(*, agents, skills, tools, tenant_config)``: registration entrypoint.

Packs are found two ways:

1. In-repo scan of ``agentkit.domain_packs.*`` (each subpackage's ``pack`` module).
2. Installed plugins declaring the ``agentkit.domain_packs`` entry point group.

Entry-point packs are loaded last and may override in-repo packs of the same
domain. A pack that fails to import is logged and skipped, never fatal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from pkgutil import iter_modules
from typing import Any, Protocol

from agentkit import domain_packs
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry

logger = logging.getLogger("agentkit.packs")

ENTRY_POINT_GROUP = "agentkit.domain_packs"


class RegisterFn(Protocol):
    def __call__(
        self,
        *,
        agents: Any,
        skills: Any,
        tools: Any,
        tenant_config: dict,
    ) -> None: ...


@dataclass(frozen=True)
class PackContractResult:
    domain: str
    agents: tuple[str, ...]
    skills: tuple[str, ...]
    tools: tuple[str, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "passed": self.passed,
            "agents": list(self.agents),
            "skills": list(self.skills),
            "tools": list(self.tools),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def iter_entry_points(*, group: str) -> list[EntryPoint]:
    """Wrapper around importlib.metadata for easy monkeypatching in tests."""
    return list(entry_points(group=group))


def _pack_from_module(module: object) -> tuple[str, RegisterFn] | None:
    domain = getattr(module, "DOMAIN", None)
    register = getattr(module, "register", None)
    if not isinstance(domain, str) or not callable(register):
        return None
    return domain, register  # type: ignore[return-value]


def discover_packs() -> dict[str, RegisterFn]:
    """Return ``domain -> register`` for every discoverable pack, sorted by domain."""
    found: dict[str, RegisterFn] = {}

    # 1. In-repo scan.
    for module_info in iter_modules(domain_packs.__path__, domain_packs.__name__ + "."):
        if not module_info.ispkg:
            continue
        pack_module_name = f"{module_info.name}.pack"
        try:
            module = import_module(pack_module_name)
        except Exception:  # pragma: no cover - exercised via monkeypatch
            logger.warning("Skipping pack %s: import failed", pack_module_name, exc_info=True)
            continue
        pack = _pack_from_module(module)
        if pack is None:
            logger.warning("Skipping %s: missing DOMAIN or register", pack_module_name)
            continue
        found[pack[0]] = pack[1]

    # 2. Entry-point plugins (may override in-repo packs).
    for entry_point in iter_entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = entry_point.load()
        except Exception:
            logger.warning("Skipping entry point %s: load failed", entry_point.name, exc_info=True)
            continue
        pack = _pack_from_module(module)
        if pack is None:
            logger.warning("Skipping entry point %s: missing DOMAIN or register", entry_point.name)
            continue
        found[pack[0]] = pack[1]

    return {domain: found[domain] for domain in sorted(found)}


def validate_pack_contract(
    domain: str,
    register: RegisterFn,
    *,
    tenant_config: dict[str, Any] | None = None,
) -> PackContractResult:
    """Validate one domain pack's registration contract without running the graph."""
    errors: list[str] = []
    warnings: list[str] = []
    agents = AgentRegistry()
    skills = SkillRegistry()
    tools = ToolRegistry()
    config = {
        "tenant_id": "contract-test",
        "enabled_domains": [domain],
        "prompt_files": {},
        **(tenant_config or {}),
    }

    try:
        register(agents=agents, skills=skills, tools=tools, tenant_config=config)
    except Exception as exc:  # noqa: BLE001 - contract report should isolate a pack failure
        return PackContractResult(
            domain=domain,
            agents=(),
            skills=(),
            tools=(),
            errors=(f"register failed: {exc}",),
        )

    agent_items = agents.all()
    skill_items = skills.all()
    tool_items = tools.all()
    agent_names = tuple(sorted(agent.name for agent in agent_items))
    skill_names = tuple(sorted(skill.name for skill in skill_items))
    tool_names = tuple(sorted(tool.name for tool in tool_items))
    skill_name_set = set(skill_names)
    tool_name_set = set(tool_names)

    if not agent_items and not skill_items and not tool_items:
        errors.append("pack registered no agents, skills, or tools")

    for agent in agent_items:
        if not isinstance(agent.name, str) or not agent.name:
            errors.append("agent name must be a non-empty string")
        if agent.domain != domain:
            errors.append(f"agent {agent.name} has domain {agent.domain!r}, expected {domain!r}")
        agent_allowed_skills = _validate_str_list(
            agent.allowed_skills,
            label=f"agent {agent.name} allowed_skills",
            errors=errors,
        )
        agent_allowed_tools = _validate_str_list(
            agent.allowed_tools,
            label=f"agent {agent.name} allowed_tools",
            errors=errors,
        )
        for skill_name in agent_allowed_skills:
            if skill_name not in skill_name_set:
                errors.append(f"agent {agent.name} references missing skill {skill_name}")
        for tool_name in agent_allowed_tools:
            if tool_name not in tool_name_set:
                errors.append(f"agent {agent.name} references missing tool {tool_name}")

    for skill in skill_items:
        if not isinstance(skill.name, str) or not skill.name:
            errors.append("skill name must be a non-empty string")
        if skill.domain != domain:
            errors.append(f"skill {skill.name} has domain {skill.domain!r}, expected {domain!r}")
        if not callable(skill.handler):
            errors.append(f"skill {skill.name} handler is not callable")
        if skill.execution_mode not in {"react", "plan_execute", "batch", "workflow", "no_tool"}:
            errors.append(f"skill {skill.name} has invalid execution_mode {skill.execution_mode!r}")
        _validate_str_list(
            skill.permissions,
            label=f"skill {skill.name} permissions",
            errors=errors,
        )
        skill_tools = _validate_str_list(
            skill.tools,
            label=f"skill {skill.name} tools",
            errors=errors,
        )
        _validate_str_list(
            skill.keywords,
            label=f"skill {skill.name} keywords",
            errors=errors,
        )
        if skill.batch_key is not None:
            if not isinstance(skill.batch_key, str) or not skill.batch_key:
                errors.append(f"skill {skill.name} batch_key must be a non-empty string")
            elif not isinstance(skill.input_schema, dict):
                errors.append(f"skill {skill.name} batch_key requires dict input_schema")
            elif skill.batch_key not in skill.input_schema.get("properties", {}):
                errors.append(
                    f"skill {skill.name} batch_key {skill.batch_key!r} is not in input_schema"
                )
        if skill.execution_mode == "batch" and not skill.batch_key:
            errors.append(f"skill {skill.name} execution_mode 'batch' requires batch_key")
        for tool_name in skill_tools:
            if tool_name not in tool_name_set:
                errors.append(f"skill {skill.name} references missing tool {tool_name}")
        _validate_json_schema(
            skill.input_schema,
            label=f"skill {skill.name} input_schema",
            errors=errors,
        )
        _validate_json_schema(
            skill.output_schema,
            label=f"skill {skill.name} output_schema",
            errors=errors,
        )

    for tool in tool_items:
        if not isinstance(tool.name, str) or not tool.name:
            errors.append("tool name must be a non-empty string")
        if tool.domain != domain:
            warnings.append(f"tool {tool.name} has non-pack domain {tool.domain!r}")
        if not callable(tool.handler):
            errors.append(f"tool {tool.name} handler is not callable")
        if not isinstance(tool.idempotent, bool):
            errors.append(f"tool {tool.name} idempotent must be a bool")
        if not isinstance(tool.supports_batch, bool):
            errors.append(f"tool {tool.name} supports_batch must be a bool")
        if tool.timeout_seconds is not None:
            try:
                timeout = float(tool.timeout_seconds)
            except (TypeError, ValueError):
                errors.append(f"tool {tool.name} timeout_seconds must be numeric or null")
            else:
                if timeout < 0:
                    errors.append(f"tool {tool.name} timeout_seconds must be >= 0")

    return PackContractResult(
        domain=domain,
        agents=agent_names,
        skills=skill_names,
        tools=tool_names,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def validate_pack_contracts(
    *,
    domains: set[str] | None = None,
    tenant_config: dict[str, Any] | None = None,
) -> list[PackContractResult]:
    packs = discover_packs()
    selected = {
        domain: register
        for domain, register in packs.items()
        if domains is None or domain in domains
    }
    missing = sorted((domains or set()) - set(selected))
    results = [
        validate_pack_contract(domain, register, tenant_config=tenant_config)
        for domain, register in selected.items()
    ]
    results.extend(
        PackContractResult(
            domain=domain,
            agents=(),
            skills=(),
            tools=(),
            errors=("pack not found",),
        )
        for domain in missing
    )
    return sorted(results, key=lambda result: result.domain)


def _validate_json_schema(schema: Any, *, label: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{label} must be a dict")
        return
    try:
        from jsonschema import Draft7Validator

        Draft7Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 - include schema failure in report
        errors.append(f"{label} is not a valid JSON schema: {exc}")


def _validate_str_list(value: Any, *, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be a list")
        return []
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            errors.append(f"{label}[{index}] must be a non-empty string")
            continue
        items.append(item)
    return items


__all__ = [
    "discover_packs",
    "validate_pack_contract",
    "validate_pack_contracts",
    "PackContractResult",
    "RegisterFn",
    "ENTRY_POINT_GROUP",
]
