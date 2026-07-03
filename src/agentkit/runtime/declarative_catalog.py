"""声明式 Agent、Skill 与 Tool 目录的严格解析和编译。"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agentkit.core.contracts import (
    AgentProfile,
    ArtifactContextPolicy,
    ContextPolicy,
    MemoryContextPolicy,
    RagContextPolicy,
    SkillDefinition,
    ToolDefinition,
)
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    AutonomyLimits,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    ToolPolicy,
    ToolProvider,
    ToolRisk,
)
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry

DEFAULT_GLOBAL_BUDGET = AutonomyBudget(
    max_model_calls=64,
    max_tool_calls=128,
    max_iterations=32,
    max_plan_steps=32,
    max_replans=4,
    max_tokens=200_000,
    timeout_seconds=3600,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _MemoryYaml(_StrictModel):
    enabled: bool = True
    scope: Literal["agent_user"] = "agent_user"
    window_turns: int = Field(default=6, gt=0)
    max_context_tokens: int = Field(default=4000, gt=0)


class _RagYaml(_StrictModel):
    enabled: bool = False
    collections: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, gt=0)
    max_context_tokens: int = Field(default=1200, gt=0)


class _ArtifactsYaml(_StrictModel):
    readable: list[str] = Field(default_factory=list)
    writable: list[str] = Field(default_factory=list)


class _ContextYaml(_StrictModel):
    memory: _MemoryYaml = Field(default_factory=_MemoryYaml)
    rag: _RagYaml = Field(default_factory=_RagYaml)
    artifacts: _ArtifactsYaml = Field(default_factory=_ArtifactsYaml)


class _AgentExecutionYaml(_StrictModel):
    default_strategy: ExecutionStrategyName
    allowed_strategies: list[ExecutionStrategyName] = Field(min_length=1)
    allow_dynamic_selection: bool = False
    allow_side_effects: bool = False

    @model_validator(mode="after")
    def validate_default_strategy(self) -> _AgentExecutionYaml:
        if self.default_strategy not in self.allowed_strategies:
            raise ValueError("default_strategy 必须包含在 allowed_strategies 中")
        return self


class _SkillExecutionYaml(_StrictModel):
    reasoning: ReasoningStrategy
    orchestration: OrchestrationMode
    tool_policy: ToolPolicy
    allow_dynamic_selection: bool = False

    @model_validator(mode="after")
    def validate_risk_boundary(self) -> _SkillExecutionYaml:
        if self.reasoning is ReasoningStrategy.REACT and self.tool_policy is ToolPolicy.SIDE_EFFECT:
            raise ValueError("ReAct 不能声明 side_effect")
        return self


class _AgentAutonomyYaml(_StrictModel):
    max_model_calls: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_iterations: int = Field(gt=0)
    max_plan_steps: int = Field(gt=0)
    max_replans: int = Field(ge=0)
    max_tokens: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)

    def to_runtime(self) -> AutonomyBudget:
        return AutonomyBudget(**self.model_dump())


class _SkillAutonomyYaml(_StrictModel):
    max_model_calls: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, gt=0)
    max_plan_steps: int | None = Field(default=None, gt=0)
    max_replans: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)

    def to_runtime(self) -> AutonomyLimits:
        return AutonomyLimits(**self.model_dump())


class _AgentYaml(_StrictModel):
    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prompt_file: str = ""
    skills: list[str]
    context: _ContextYaml
    execution: _AgentExecutionYaml
    autonomy: _AgentAutonomyYaml
    routing_hints: list[str] = Field(default_factory=list)


class _ToolYaml(_StrictModel):
    id: str = Field(min_length=1)
    provider: ToolProvider
    description: str = Field(min_length=1)
    risk: ToolRisk
    permissions: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    entrypoint: str | None = None
    factory_entrypoint: str | None = None
    server: str | None = None
    tool: str | None = None
    supports_batch: bool = False
    idempotent: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_provider_fields(self) -> _ToolYaml:
        if self.provider is ToolProvider.PYTHON and not self.entrypoint:
            raise ValueError("Python Tool 必须声明 entrypoint")
        if self.provider is ToolProvider.MCP and (not self.server or not self.tool):
            raise ValueError("MCP Tool 必须声明 server 和 tool")
        if self.provider is ToolProvider.MCP and self.factory_entrypoint:
            raise ValueError("MCP Tool 不能声明 factory_entrypoint")
        return self


class _CapabilityYaml(_StrictModel):
    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entrypoint: str = Field(min_length=1)
    execution: _SkillExecutionYaml
    autonomy: _SkillAutonomyYaml = Field(default_factory=_SkillAutonomyYaml)
    permissions: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    batch_key: str | None = None
    keywords: list[str] = Field(default_factory=list)


class _SkillPackageYaml(_StrictModel):
    package_id: str = Field(min_length=1)
    tools: list[_ToolYaml] = Field(default_factory=list)
    capabilities: list[_CapabilityYaml] = Field(default_factory=list)


@dataclass(frozen=True)
class AgentManifest:
    """单个 ``agent.md`` 的可执行元数据。"""

    agent_id: str
    domain: str
    description: str
    skills: tuple[str, ...]
    prompt_file: str
    context: ContextPolicy
    execution: AgentExecutionPolicy
    autonomy: AutonomyBudget
    routing_hints: tuple[str, ...]
    instructions: str
    source_path: Path


@dataclass(frozen=True)
class ToolManifest:
    """Skill 包中声明的受控工具入口。"""

    tool_id: str
    package_id: str
    description: str
    provider: ToolProvider
    risk: ToolRisk
    permissions: tuple[str, ...]
    input_schema: dict[str, Any]
    entrypoint: str | None
    factory_entrypoint: str | None
    mcp_server: str | None
    mcp_tool: str | None
    supports_batch: bool
    idempotent: bool
    timeout_seconds: float | None
    source_path: Path


@dataclass(frozen=True)
class CapabilityManifest:
    """Skill 包导出的一个运行时能力。"""

    capability_id: str
    package_id: str
    domain: str
    description: str
    entrypoint: str
    execution: SkillExecutionPolicy
    autonomy: AutonomyLimits
    permissions: tuple[str, ...]
    tools: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    batch_key: str | None
    keywords: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True)
class DeclarativeCatalog:
    """一个仓库根目录下发现的全部业务声明。"""

    root: Path
    agents: dict[str, AgentManifest]
    capabilities: dict[str, CapabilityManifest]
    tools: dict[str, ToolManifest]


def load_catalog(
    root: str | Path,
    *,
    global_budget: AutonomyBudget = DEFAULT_GLOBAL_BUDGET,
) -> DeclarativeCatalog:
    """加载声明目录，并在启动前完成全部交叉校验。"""

    resolved_root = Path(root).resolve()
    agents = _load_agents(resolved_root / "agents")
    capabilities, tools = _load_skill_packages(resolved_root / "skills")
    _validate_references(
        agents=agents,
        capabilities=capabilities,
        tools=tools,
        global_budget=global_budget,
    )
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
    """把租户启用的声明编译到运行时注册表。"""

    unknown_agents = sorted(enabled_agent_ids - set(catalog.agents))
    if unknown_agents:
        raise ValueError(f"引用了未知 Agent: {', '.join(unknown_agents)}")

    selected_agents = [catalog.agents[item] for item in sorted(enabled_agent_ids)]
    capability_ids = _unique_in_order(
        capability for agent in selected_agents for capability in agent.skills
    )
    tool_ids = _unique_in_order(
        tool
        for capability_id in capability_ids
        for tool in catalog.capabilities[capability_id].tools
    )
    factory_cache: dict[tuple[Path, str], dict[str, Callable[..., Any]]] = {}
    for tool_id in tool_ids:
        tools.register(
            _compile_tool(
                catalog.root,
                catalog.tools[tool_id],
                tenant_config=dict(tenant_config or {}),
                tool_factory_cache=factory_cache,
            )
        )
    for capability_id in capability_ids:
        skills.register(_compile_capability(catalog.root, catalog.capabilities[capability_id]))
    for manifest in selected_agents:
        agents.register(
            AgentProfile(
                name=manifest.agent_id,
                domain=manifest.domain,
                description=manifest.description,
                allowed_skills=list(manifest.skills),
                execution_policy=manifest.execution,
                autonomy_budget=manifest.autonomy,
                context_policy=manifest.context,
                prompt_file=manifest.prompt_file,
                max_tokens=manifest.autonomy.max_tokens,
                routing_hints=manifest.routing_hints,
            )
        )


def resolve_enabled_agent_ids(
    catalog: DeclarativeCatalog,
    tenant_config: Mapping[str, Any],
) -> set[str]:
    """读取显式 Agent 白名单；不再根据领域推断或扩大权限。"""

    configured = tenant_config.get("enabled_agents")
    if not isinstance(configured, list) or not configured:
        raise ValueError("租户必须显式配置非空 enabled_agents")
    selected = {str(value) for value in configured}
    unknown = sorted(selected - set(catalog.agents))
    if unknown:
        raise ValueError(f"租户引用了未知 Agent: {', '.join(unknown)}")
    return selected


def parse_agent_markdown(path: str | Path) -> tuple[dict[str, Any], str]:
    """读取带 YAML front matter 的 Agent Markdown。"""

    source_path = Path(path)
    lines = source_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source_path}: agent.md 必须以 YAML front matter 开始")
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise ValueError(f"{source_path}: agent.md 缺少 YAML front matter 结束标记")
    value = yaml.safe_load("\n".join(lines[1:closing]))
    if not isinstance(value, dict):
        raise ValueError(f"{source_path}: YAML front matter 必须是对象")
    return value, "\n".join(lines[closing + 1 :]).strip()


def _load_agents(root: Path) -> dict[str, AgentManifest]:
    if not root.exists():
        return {}
    result: dict[str, AgentManifest] = {}
    for source_path in sorted(root.glob("*/agent.md")):
        raw, body = parse_agent_markdown(source_path)
        parsed = _validate_model(_AgentYaml, raw, source_path)
        manifest = _build_agent_manifest(parsed, body, source_path)
        if manifest.agent_id in result:
            raise ValueError(f"{source_path}: 重复的 Agent ID {manifest.agent_id}")
        result[manifest.agent_id] = manifest
    return result


def _load_skill_packages(
    root: Path,
) -> tuple[dict[str, CapabilityManifest], dict[str, ToolManifest]]:
    capabilities: dict[str, CapabilityManifest] = {}
    tools: dict[str, ToolManifest] = {}
    if not root.exists():
        return capabilities, tools
    for source_path in sorted(root.glob("*/skill.yaml")):
        package = _validate_model(_SkillPackageYaml, _load_yaml_object(source_path), source_path)
        for raw_tool in package.tools:
            tool = _build_tool_manifest(raw_tool, package.package_id, source_path)
            if tool.tool_id in tools:
                raise ValueError(f"{source_path}: 重复的工具 ID {tool.tool_id}")
            tools[tool.tool_id] = tool
        for raw_capability in package.capabilities:
            capability = _build_capability_manifest(
                raw_capability, package.package_id, source_path
            )
            if capability.capability_id in capabilities:
                raise ValueError(
                    f"{source_path}: 重复的 capability ID {capability.capability_id}"
                )
            capabilities[capability.capability_id] = capability
    return capabilities, tools


def _build_agent_manifest(
    raw: _AgentYaml,
    body: str,
    source_path: Path,
) -> AgentManifest:
    memory = raw.context.memory
    rag = raw.context.rag
    artifacts = raw.context.artifacts
    return AgentManifest(
        agent_id=raw.id,
        domain=raw.domain,
        description=raw.description,
        skills=tuple(raw.skills),
        prompt_file=raw.prompt_file,
        context=ContextPolicy(
            memory=MemoryContextPolicy(
                enabled=memory.enabled,
                scope=memory.scope,
                window_turns=memory.window_turns,
                max_context_tokens=memory.max_context_tokens,
            ),
            rag=RagContextPolicy(
                enabled=rag.enabled,
                collections=tuple(rag.collections),
                top_k=rag.top_k,
                max_context_tokens=rag.max_context_tokens,
            ),
            artifacts=ArtifactContextPolicy(
                readable=tuple(artifacts.readable),
                writable=tuple(artifacts.writable),
            ),
        ),
        execution=AgentExecutionPolicy(
            default_strategy=raw.execution.default_strategy,
            allowed_strategies=tuple(raw.execution.allowed_strategies),
            allow_dynamic_selection=raw.execution.allow_dynamic_selection,
            allow_side_effects=raw.execution.allow_side_effects,
        ),
        autonomy=raw.autonomy.to_runtime(),
        routing_hints=tuple(raw.routing_hints),
        instructions=body,
        source_path=source_path,
    )


def _build_tool_manifest(
    raw: _ToolYaml,
    package_id: str,
    source_path: Path,
) -> ToolManifest:
    if raw.entrypoint:
        _validate_entrypoint_format(raw.entrypoint, source_path)
    if raw.factory_entrypoint:
        _validate_entrypoint_format(raw.factory_entrypoint, source_path)
    return ToolManifest(
        tool_id=raw.id,
        package_id=package_id,
        description=raw.description,
        provider=raw.provider,
        risk=raw.risk,
        permissions=tuple(raw.permissions),
        input_schema=dict(raw.input_schema),
        entrypoint=raw.entrypoint,
        factory_entrypoint=raw.factory_entrypoint,
        mcp_server=raw.server,
        mcp_tool=raw.tool,
        supports_batch=raw.supports_batch,
        idempotent=raw.idempotent,
        timeout_seconds=raw.timeout_seconds,
        source_path=source_path,
    )


def _build_capability_manifest(
    raw: _CapabilityYaml,
    package_id: str,
    source_path: Path,
) -> CapabilityManifest:
    _validate_entrypoint_format(raw.entrypoint, source_path)
    return CapabilityManifest(
        capability_id=raw.id,
        package_id=package_id,
        domain=raw.domain,
        description=raw.description,
        entrypoint=raw.entrypoint,
        execution=SkillExecutionPolicy(
            reasoning=raw.execution.reasoning,
            orchestration=raw.execution.orchestration,
            tool_policy=raw.execution.tool_policy,
            allow_dynamic_selection=raw.execution.allow_dynamic_selection,
        ),
        autonomy=raw.autonomy.to_runtime(),
        permissions=tuple(raw.permissions),
        tools=tuple(raw.tools),
        input_schema=dict(raw.input_schema),
        output_schema=dict(raw.output_schema),
        batch_key=raw.batch_key,
        keywords=tuple(raw.keywords),
        source_path=source_path,
    )


def _validate_references(
    *,
    agents: dict[str, AgentManifest],
    capabilities: dict[str, CapabilityManifest],
    tools: dict[str, ToolManifest],
    global_budget: AutonomyBudget,
) -> None:
    for agent in agents.values():
        unknown = sorted(set(agent.skills) - set(capabilities))
        if unknown:
            raise ValueError(f"{agent.source_path}: 引用了未知 capability: {', '.join(unknown)}")
        _validate_budget_not_greater(
            label="Agent 自主预算不能超过全局预算",
            values=agent.autonomy,
            ceiling=global_budget,
            source_path=agent.source_path,
        )
        for capability_id in agent.skills:
            _validate_skill_budget(agent, capabilities[capability_id])
    for capability in capabilities.values():
        unknown = sorted(set(capability.tools) - set(tools))
        if unknown:
            raise ValueError(
                f"{capability.source_path}: 引用了未知工具: {', '.join(unknown)}"
            )


def _validate_skill_budget(agent: AgentManifest, capability: CapabilityManifest) -> None:
    for item in fields(capability.autonomy):
        value = getattr(capability.autonomy, item.name)
        if value is not None and value > getattr(agent.autonomy, item.name):
            raise ValueError(
                f"{capability.source_path}: Skill 自主预算不能超过 Agent: {item.name}"
            )


def _validate_budget_not_greater(
    *,
    label: str,
    values: AutonomyBudget,
    ceiling: AutonomyBudget,
    source_path: Path,
) -> None:
    for item in fields(values):
        if getattr(values, item.name) > getattr(ceiling, item.name):
            raise ValueError(f"{source_path}: {label}: {item.name}")


def _validate_entrypoint_format(entrypoint: str, source_path: Path) -> None:
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
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    if manifest.provider is ToolProvider.PYTHON:
        if manifest.entrypoint is None:
            raise ValueError(f"{manifest.source_path}: Python Tool 缺少 entrypoint")
        handler = _resolve_python_tool_handler(
            root,
            manifest,
            tenant_config=tenant_config,
            tool_factory_cache=tool_factory_cache,
        )
    return ToolDefinition(
        name=manifest.tool_id,
        domain=_tool_domain(manifest.tool_id),
        description=manifest.description,
        handler=handler,
        provider=manifest.provider,
        risk=manifest.risk,
        permissions=list(manifest.permissions),
        input_schema=dict(manifest.input_schema),
        mcp_server=manifest.mcp_server,
        mcp_tool=manifest.mcp_tool,
        supports_batch=manifest.supports_batch,
        idempotent=manifest.idempotent,
        timeout_seconds=manifest.timeout_seconds,
    )


def _resolve_python_tool_handler(
    root: Path,
    manifest: ToolManifest,
    *,
    tenant_config: dict[str, Any],
    tool_factory_cache: dict[tuple[Path, str], dict[str, Callable[..., Any]]],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if manifest.factory_entrypoint is None:
        assert manifest.entrypoint is not None
        return _load_entrypoint(root, manifest.source_path.parent, manifest.entrypoint)
    cache_key = (manifest.source_path.parent, manifest.factory_entrypoint)
    handlers = tool_factory_cache.get(cache_key)
    if handlers is None:
        factory = _load_entrypoint(
            root, manifest.source_path.parent, manifest.factory_entrypoint
        )
        built = factory(tenant_config)
        if not isinstance(built, dict) or any(
            not isinstance(key, str) or not callable(value) for key, value in built.items()
        ):
            raise ValueError(f"{manifest.source_path}: 工具工厂必须返回工具 ID 到函数的字典")
        handlers = dict(built)
        tool_factory_cache[cache_key] = handlers
    handler = handlers.get(manifest.tool_id)
    if handler is None:
        raise ValueError(f"{manifest.source_path}: 工具工厂未返回 {manifest.tool_id}")
    return handler


def _compile_capability(root: Path, manifest: CapabilityManifest) -> SkillDefinition:
    handler = _load_entrypoint(root, manifest.source_path.parent, manifest.entrypoint)
    return SkillDefinition(
        name=manifest.capability_id,
        domain=manifest.domain,
        description=manifest.description,
        input_schema=dict(manifest.input_schema),
        output_schema=dict(manifest.output_schema),
        permissions=list(manifest.permissions),
        execution=manifest.execution,
        autonomy=manifest.autonomy,
        tools=list(manifest.tools),
        handler=handler,
        batch_key=manifest.batch_key,
        keywords=list(manifest.keywords),
    )


def _load_entrypoint(
    root: Path,
    package_root: Path,
    entrypoint: str,
) -> Callable[..., Any]:
    """在隔离命名空间中加载 Skill 脚本函数。"""

    del root
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
    module = _load_source_module(
        module_name=f"{package_name}.{module_path}",
        source_path=source_path,
    )
    handler = getattr(module, attribute, None)
    if not callable(handler):
        raise ValueError(f"{source_path}: 入口函数 {attribute!r} 不可调用")
    return handler


def _ensure_script_package(*, package_name: str, package_root: Path, scripts_dir: Path) -> None:
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


def _validate_model(
    model: type[_ModelT],
    raw: Any,
    source_path: Path,
) -> _ModelT:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{source_path}: {exc}") from exc


def _load_yaml_object(source_path: Path) -> dict[str, Any]:
    value = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{source_path}: YAML 根节点必须是对象")
    return value


def _unique_in_order(values: Any) -> list[str]:
    return list(dict.fromkeys(values))


def _tool_domain(tool_id: str) -> str:
    return tool_id.split(".", 1)[0]


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
