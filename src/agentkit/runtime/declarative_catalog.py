"""声明式 Agent 与 Skill 目录的安全解析和校验。"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from agentkit.core.contracts import AgentProfile, ExecutionMode, SkillDefinition, ToolDefinition
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry

_EXECUTION_MODES: set[str] = {"react", "plan_execute", "batch", "workflow", "no_tool"}
_CONTEXT_LIST_FIELDS = {
    "knowledge_collections",
    "readable_artifact_kinds",
    "writable_artifact_kinds",
}


@dataclass(frozen=True)
class AgentManifest:
    """单个 `agent.md` 的可执行元数据。"""

    agent_id: str
    domain: str
    description: str
    skills: tuple[str, ...]
    prompt_file: str
    max_tokens: int
    context: dict[str, Any]
    source_path: Path


@dataclass(frozen=True)
class ToolManifest:
    """Skill 包中声明的受控工具入口。"""

    tool_id: str
    package_id: str
    description: str
    entrypoint: str
    factory_entrypoint: str | None
    supports_batch: bool
    idempotent: bool
    timeout_seconds: float | None
    source_path: Path


@dataclass(frozen=True)
class CapabilityManifest:
    """Skill 包导出的一个 AgentKit 运行时能力。"""

    capability_id: str
    package_id: str
    domain: str
    description: str
    entrypoint: str
    execution_mode: ExecutionMode
    permissions: tuple[str, ...]
    tools: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    batch_key: str | None
    keywords: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True)
class DeclarativeCatalog:
    """一个仓库根目录下发现的所有业务声明。"""

    root: Path
    agents: dict[str, AgentManifest]
    capabilities: dict[str, CapabilityManifest]
    tools: dict[str, ToolManifest]


def load_catalog(root: str | Path) -> DeclarativeCatalog:
    """加载 `agents/` 与 `skills/`，并在启动前校验所有引用。"""
    resolved_root = Path(root).resolve()
    agents = _load_agents(resolved_root / "agents")
    capabilities, tools = _load_skill_packages(resolved_root / "skills")
    _validate_references(agents=agents, capabilities=capabilities, tools=tools)
    return DeclarativeCatalog(
        root=resolved_root,
        agents=agents,
        capabilities=capabilities,
        tools=tools,
    )


def register_catalog(
    catalog: DeclarativeCatalog,
    *,
    enabled_agent_ids: set[str],
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    tenant_config: dict[str, Any] | None = None,
) -> None:
    """将指定 Agent 的声明编译并写入既有运行时注册表。"""
    unknown_agents = sorted(enabled_agent_ids - set(catalog.agents))
    if unknown_agents:
        raise ValueError(f"引用了未知 Agent: {', '.join(unknown_agents)}")

    selected_agents = [catalog.agents[agent_id] for agent_id in sorted(enabled_agent_ids)]
    capability_ids = _unique_in_order(
        capability_id for agent in selected_agents for capability_id in agent.skills
    )
    tool_ids = _unique_in_order(
        tool_id
        for capability_id in capability_ids
        for tool_id in catalog.capabilities[capability_id].tools
    )

    tool_factory_cache: dict[tuple[Path, str], dict[str, Callable[..., Any]]] = {}
    config = dict(tenant_config or {})
    for tool_id in tool_ids:
        tools.register(
            _compile_tool(
                catalog.root,
                catalog.tools[tool_id],
                tenant_config=config,
                tool_factory_cache=tool_factory_cache,
            )
        )
    for capability_id in capability_ids:
        skills.register(_compile_capability(catalog.root, catalog.capabilities[capability_id]))
    for manifest in selected_agents:
        allowed_tools = _unique_in_order(
            tool_id
            for capability_id in manifest.skills
            for tool_id in catalog.capabilities[capability_id].tools
        )
        agents.register(
            AgentProfile(
                name=manifest.agent_id,
                domain=manifest.domain,
                description=manifest.description,
                allowed_skills=list(manifest.skills),
                allowed_tools=allowed_tools,
                max_tokens=manifest.max_tokens,
                prompt_file=manifest.prompt_file,
                context_policy=dict(manifest.context),
            )
        )


def resolve_enabled_agent_ids(
    catalog: DeclarativeCatalog,
    tenant_config: dict[str, Any],
) -> set[str]:
    """优先读取显式 Agent 列表，并兼容旧领域开关。"""
    configured = tenant_config.get("enabled_agents")
    if isinstance(configured, list) and configured:
        selected = {str(value) for value in configured}
        unknown = sorted(selected - set(catalog.agents))
        if unknown:
            raise ValueError(f"租户引用了未知 Agent: {', '.join(unknown)}")
        return selected

    enabled_domains = {str(value) for value in tenant_config.get("enabled_domains", [])}
    return {
        manifest.agent_id
        for manifest in catalog.agents.values()
        if manifest.domain in enabled_domains
    }


def parse_agent_markdown(path: str | Path) -> tuple[dict[str, Any], str]:
    """读取带 YAML front matter 的 Agent Markdown 文件。"""
    source_path = Path(path)
    lines = source_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source_path}: agent.md 必须以 YAML front matter 开始")

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise ValueError(f"{source_path}: agent.md 缺少 YAML front matter 结束标记")

    value = yaml.safe_load("\n".join(lines[1:closing_index]))
    if not isinstance(value, dict):
        raise ValueError(f"{source_path}: YAML front matter 必须是对象")
    return value, "\n".join(lines[closing_index + 1 :]).strip()


def _load_agents(root: Path) -> dict[str, AgentManifest]:
    if not root.exists():
        return {}

    agents: dict[str, AgentManifest] = {}
    for source_path in sorted(root.glob("*/agent.md")):
        raw, _body = parse_agent_markdown(source_path)
        manifest = _build_agent_manifest(raw, source_path)
        if manifest.agent_id in agents:
            raise ValueError(f"{source_path}: 重复的 Agent ID {manifest.agent_id}")
        agents[manifest.agent_id] = manifest
    return agents


def _load_skill_packages(
    root: Path,
) -> tuple[dict[str, CapabilityManifest], dict[str, ToolManifest]]:
    if not root.exists():
        return {}, {}

    capabilities: dict[str, CapabilityManifest] = {}
    tools: dict[str, ToolManifest] = {}
    for source_path in sorted(root.glob("*/skill.yaml")):
        raw = _load_yaml_object(source_path)
        package_id = _required_string(raw, "package_id", source_path)
        for tool_raw in _required_list(raw, "tools", source_path):
            tool = _build_tool_manifest(tool_raw, package_id, source_path)
            if tool.tool_id in tools:
                raise ValueError(f"{source_path}: 重复的工具 ID {tool.tool_id}")
            tools[tool.tool_id] = tool
        for capability_raw in _required_list(raw, "capabilities", source_path):
            capability = _build_capability_manifest(capability_raw, package_id, source_path)
            if capability.capability_id in capabilities:
                raise ValueError(f"{source_path}: 重复的 capability ID {capability.capability_id}")
            capabilities[capability.capability_id] = capability
    return capabilities, tools


def _build_agent_manifest(raw: dict[str, Any], source_path: Path) -> AgentManifest:
    context = raw.get("context")
    if not isinstance(context, dict):
        raise ValueError(f"{source_path}: context 必须是对象")
    _validate_context(context, source_path)
    max_tokens = raw.get("max_tokens", 100_000)
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError(f"{source_path}: max_tokens 必须是正整数")
    prompt_file = raw.get("prompt_file", "")
    if not isinstance(prompt_file, str):
        raise ValueError(f"{source_path}: prompt_file 必须是字符串")
    return AgentManifest(
        agent_id=_required_string(raw, "id", source_path),
        domain=_required_string(raw, "domain", source_path),
        description=_required_string(raw, "description", source_path),
        skills=tuple(_required_string_list(raw, "skills", source_path)),
        prompt_file=prompt_file,
        max_tokens=max_tokens,
        context=dict(context),
        source_path=source_path,
    )


def _build_tool_manifest(raw: Any, package_id: str, source_path: Path) -> ToolManifest:
    value = _require_object(raw, source_path, "tools 项")
    timeout = value.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, int | float) or timeout < 0):
        raise ValueError(f"{source_path}: timeout_seconds 必须是非负数字或 null")
    entrypoint = _required_string(value, "entrypoint", source_path)
    _validate_entrypoint_format(entrypoint, source_path)
    factory_entrypoint = _optional_string(value, "factory_entrypoint", source_path)
    if factory_entrypoint is not None:
        _validate_entrypoint_format(factory_entrypoint, source_path)
    return ToolManifest(
        tool_id=_required_string(value, "id", source_path),
        package_id=package_id,
        description=_required_string(value, "description", source_path),
        entrypoint=entrypoint,
        factory_entrypoint=factory_entrypoint,
        supports_batch=_optional_bool(value, "supports_batch", source_path, default=False),
        idempotent=_optional_bool(value, "idempotent", source_path, default=False),
        timeout_seconds=float(timeout) if timeout is not None else None,
        source_path=source_path,
    )


def _build_capability_manifest(
    raw: Any, package_id: str, source_path: Path
) -> CapabilityManifest:
    value = _require_object(raw, source_path, "capabilities 项")
    execution_mode = _required_string(value, "execution_mode", source_path)
    if execution_mode not in _EXECUTION_MODES:
        raise ValueError(f"{source_path}: 不支持的 execution_mode {execution_mode!r}")
    input_schema = value.get("input_schema")
    output_schema = value.get("output_schema")
    if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
        raise ValueError(f"{source_path}: input_schema 和 output_schema 必须是对象")
    batch_key = value.get("batch_key")
    if batch_key is not None and (not isinstance(batch_key, str) or not batch_key):
        raise ValueError(f"{source_path}: batch_key 必须是非空字符串或 null")
    entrypoint = _required_string(value, "entrypoint", source_path)
    _validate_entrypoint_format(entrypoint, source_path)
    return CapabilityManifest(
        capability_id=_required_string(value, "id", source_path),
        package_id=package_id,
        domain=_required_string(value, "domain", source_path),
        description=_required_string(value, "description", source_path),
        entrypoint=entrypoint,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        permissions=tuple(_required_string_list(value, "permissions", source_path)),
        tools=tuple(_required_string_list(value, "tools", source_path)),
        input_schema=dict(input_schema),
        output_schema=dict(output_schema),
        batch_key=batch_key,
        keywords=tuple(_required_string_list(value, "keywords", source_path)),
        source_path=source_path,
    )


def _validate_context(context: dict[str, Any], source_path: Path) -> None:
    if context.get("memory_scope") != "agent_user":
        raise ValueError(f"{source_path}: context.memory_scope 必须是 agent_user")
    if context.get("session_key") != "tenant/agent/user/thread":
        raise ValueError(f"{source_path}: context.session_key 必须是 tenant/agent/user/thread")
    for field_name in _CONTEXT_LIST_FIELDS:
        value = context.get(field_name)
        is_string_list = isinstance(value, list) and all(
            isinstance(item, str) and item for item in value
        )
        if not is_string_list:
            raise ValueError(f"{source_path}: context.{field_name} 必须是非空字符串列表")


def _validate_references(
    *,
    agents: dict[str, AgentManifest],
    capabilities: dict[str, CapabilityManifest],
    tools: dict[str, ToolManifest],
) -> None:
    for agent in agents.values():
        unknown = sorted(set(agent.skills) - set(capabilities))
        if unknown:
            raise ValueError(f"{agent.source_path}: 引用了未知 capability: {', '.join(unknown)}")
    for capability in capabilities.values():
        unknown = sorted(set(capability.tools) - set(tools))
        if unknown:
            raise ValueError(f"{capability.source_path}: 引用了未知工具: {', '.join(unknown)}")


def _validate_entrypoint_format(entrypoint: str, source_path: Path) -> None:
    """拒绝不在 Skill `scripts/` 包内的脚本入口。"""
    module_path, separator, attribute = entrypoint.partition(":")
    components = module_path.split(".")
    if (
        separator != ":"
        or not attribute
        or components[0] != "scripts"
        or len(components) < 2
        or any(not component.isidentifier() for component in components)
        or not attribute.isidentifier()
    ):
        raise ValueError(f"{source_path}: 入口必须是 scripts 目录内的模块:function")


def _compile_tool(
    root: Path,
    manifest: ToolManifest,
    *,
    tenant_config: dict[str, Any],
    tool_factory_cache: dict[tuple[Path, str], dict[str, Callable[..., Any]]],
) -> ToolDefinition:
    """将一个工具声明编译为运行时工具定义。"""
    if manifest.factory_entrypoint is None:
        handler = _load_entrypoint(root, manifest.source_path.parent, manifest.entrypoint)
    else:
        cache_key = (manifest.source_path.parent, manifest.factory_entrypoint)
        handlers = tool_factory_cache.get(cache_key)
        if handlers is None:
            factory = _load_entrypoint(
                root,
                manifest.source_path.parent,
                manifest.factory_entrypoint,
            )
            built = factory(tenant_config)
            if not isinstance(built, dict):
                raise ValueError(
                    f"{manifest.source_path}: 工具工厂必须返回工具 ID 到可调用对象的字典"
                )
            normalized_handlers: dict[str, Callable[..., Any]] = {}
            for tool_id, candidate in built.items():
                if not isinstance(tool_id, str) or not callable(candidate):
                    raise ValueError(
                        f"{manifest.source_path}: 工具工厂必须返回工具 ID 到可调用对象的字典"
                    )
                normalized_handlers[tool_id] = candidate
            handlers = normalized_handlers
            tool_factory_cache[cache_key] = handlers
        factory_handler = handlers.get(manifest.tool_id)
        if factory_handler is None:
            raise ValueError(f"{manifest.source_path}: 工具工厂未返回 {manifest.tool_id}")
        handler = factory_handler
    return ToolDefinition(
        name=manifest.tool_id,
        domain=_tool_domain(manifest.tool_id),
        description=manifest.description,
        handler=handler,
        supports_batch=manifest.supports_batch,
        idempotent=manifest.idempotent,
        timeout_seconds=manifest.timeout_seconds,
    )


def _compile_capability(root: Path, manifest: CapabilityManifest) -> SkillDefinition:
    """将一个 capability 声明编译为运行时 Skill 定义。"""
    handler = _load_entrypoint(root, manifest.source_path.parent, manifest.entrypoint)
    return SkillDefinition(
        name=manifest.capability_id,
        domain=manifest.domain,
        description=manifest.description,
        input_schema=dict(manifest.input_schema),
        output_schema=dict(manifest.output_schema),
        permissions=list(manifest.permissions),
        execution_mode=manifest.execution_mode,
        tools=list(manifest.tools),
        handler=handler,
        batch_key=manifest.batch_key,
        keywords=list(manifest.keywords),
    )


def _load_entrypoint(
    root: Path, package_root: Path, entrypoint: str
) -> Callable[..., Any]:
    """按受限模块名加载 Skill 脚本函数，支持脚本包内的相对导入。"""
    del root  # 保留根目录参数，调用处可明确表达加载范围。
    module_path, _, attribute = entrypoint.partition(":")
    scripts_dir = (package_root / "scripts").resolve()
    source_path = (package_root / (module_path.replace(".", "/") + ".py")).resolve()
    if not source_path.is_file() or scripts_dir not in source_path.parents:
        raise ValueError(f"{package_root}: 入口脚本不存在或不在 scripts 目录")
    init_path = scripts_dir / "__init__.py"
    if not init_path.is_file():
        raise ValueError(f"{package_root}: scripts 目录必须包含 __init__.py")

    package_name = f"agentkit_skill_{hashlib.sha256(str(package_root).encode()).hexdigest()[:16]}"
    _ensure_script_package(
        package_name=package_name,
        package_root=package_root,
        scripts_dir=scripts_dir,
    )
    module_name = f"{package_name}.{module_path}"
    module = _load_source_module(module_name=module_name, source_path=source_path)
    handler = getattr(module, attribute, None)
    if not callable(handler):
        raise ValueError(f"{source_path}: 入口函数 {attribute!r} 不可调用")
    return handler


def _ensure_script_package(*, package_name: str, package_root: Path, scripts_dir: Path) -> None:
    """为一个 Skill 构造独立模块命名空间，避免不同包的脚本冲突。"""
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(package_root)]
        package.__package__ = package_name
        sys.modules[package_name] = package

    scripts_name = f"{package_name}.scripts"
    if scripts_name in sys.modules:
        return
    init_path = scripts_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        scripts_name,
        init_path,
        submodule_search_locations=[str(scripts_dir)],
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"{scripts_dir}: 无法创建脚本包加载器")
    module = importlib.util.module_from_spec(spec)
    sys.modules[scripts_name] = module
    spec.loader.exec_module(module)


def _load_source_module(*, module_name: str, source_path: Path) -> ModuleType:
    """加载一个未安装的 Skill 脚本模块。"""
    existing = sys.modules.get(module_name)
    if isinstance(existing, ModuleType):
        return existing
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{source_path}: 无法创建脚本加载器")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _unique_in_order(values: Any) -> list[str]:
    """去重并保持声明顺序，避免迁移改变既有工具白名单顺序。"""
    return list(dict.fromkeys(values))


def _tool_domain(tool_id: str) -> str:
    """工具没有单独声明 domain 时，使用其首段作为稳定归属。"""
    return tool_id.split(".", 1)[0]


def _load_yaml_object(source_path: Path) -> dict[str, Any]:
    value = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{source_path}: YAML 根节点必须是对象")
    return value


def _required_string(raw: dict[str, Any], key: str, source_path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source_path}: {key} 必须是非空字符串")
    return value.strip()


def _required_list(raw: dict[str, Any], key: str, source_path: Path) -> list[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{source_path}: {key} 必须是列表")
    return value


def _required_string_list(raw: dict[str, Any], key: str, source_path: Path) -> list[str]:
    values = _required_list(raw, key, source_path)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{source_path}: {key} 必须是非空字符串列表")
    return [value.strip() for value in values]


def _optional_bool(raw: dict[str, Any], key: str, source_path: Path, *, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{source_path}: {key} 必须是布尔值")
    return value


def _optional_string(raw: dict[str, Any], key: str, source_path: Path) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source_path}: {key} 必须是非空字符串或 null")
    return value.strip()


def _require_object(value: Any, source_path: Path, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{source_path}: {label} 必须是对象")
    return value


__all__ = [
    "AgentManifest",
    "CapabilityManifest",
    "DeclarativeCatalog",
    "ToolManifest",
    "load_catalog",
    "parse_agent_markdown",
    "register_catalog",
    "resolve_enabled_agent_ids",
]
