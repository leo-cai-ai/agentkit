"""声明式租户、Agent 与 Skill 骨架生成器。"""

from __future__ import annotations

import json
import re
from pathlib import Path


def _validate_id(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", normalized):
        raise ValueError(f"{label} 只允许小写字母、数字、下划线和连字符")
    return normalized


def render_tenant_config(tenant_id: str) -> str:
    return json.dumps(
        {
            "tenant_id": tenant_id,
            "enabled_agents": [],
            "role_permissions": {},
            "principal_business_roles": {},
            "context_overrides": {},
            "mcp_servers": {},
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def render_agent_manifest(agent_id: str) -> str:
    agent_id = _validate_id(agent_id, label="Agent ID")
    return f"""---
id: {agent_id}
domain: example.{agent_id}
description: 请填写 {agent_id} 的业务职责。
skills: []
context:
  memory:
    {{enabled: true, scope: agent_user, window_turns: 6,
      max_context_tokens: 4000, retrieval_k: 4}}
  rag: {{enabled: false, collections: [], top_k: 5, max_context_tokens: 1200}}
  artifacts: {{readable: [], writable: []}}
execution:
  default_strategy: direct
  allowed_strategies: [direct]
  allow_dynamic_selection: false
  allow_side_effects: false
autonomy:
  max_model_calls: 8
  max_tool_calls: 8
  max_iterations: 6
  max_plan_steps: 6
  max_replans: 1
  max_tokens: 20000
  timeout_seconds: 180
routing_keywords: []
---

# {agent_id} Agent

请在这里编写 Agent 的业务边界、安全约束和输出规则。
"""


def render_skill_manifest(package_id: str) -> str:
    package_id = _validate_id(package_id, label="Skill Package ID")
    capability = package_id.replace("-", ".") + ".run"
    return f"""package_id: {package_id}
tools: []
capabilities:
  - id: {capability}
    domain: example.{package_id.replace('-', '_')}
    description: 请填写该能力的用途。
    entrypoint: scripts.handlers:run
    execution: {{reasoning: direct, orchestration: single, tool_policy: none}}
    autonomy:
      {{max_model_calls: 2, max_tool_calls: 2, max_iterations: 2,
        max_tokens: 4000, timeout_seconds: 30}}
    permissions: []
    tools: []
    input_schema: {{type: object, properties: {{}}}}
    output_schema: {{type: object, properties: {{}}}}
    keywords: []
"""


def render_skill_readme(package_id: str) -> str:
    return f"""# {package_id}

## 目标

请说明该 Skill 包解决的业务问题。

## 流程

1. 验证输入。
2. 执行受治理的业务逻辑。
3. 返回可追溯结果。
"""


def create_tenant(tenant_id: str, *, root: Path, force: bool = False) -> Path:
    path = Path(root) / f"{tenant_id}.json"
    if path.exists() and not force:
        raise FileExistsError(f"租户配置已存在: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_tenant_config(tenant_id), encoding="utf-8")
    return path


def create_agent(agent_id: str, *, root: Path) -> Path:
    agent_id = _validate_id(agent_id, label="Agent ID")
    path = Path(root) / agent_id / "agent.md"
    if path.exists():
        raise FileExistsError(f"Agent 已存在: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_agent_manifest(agent_id), encoding="utf-8")
    return path


def create_skill(package_id: str, *, root: Path) -> Path:
    package_id = _validate_id(package_id, label="Skill Package ID")
    package = Path(root) / package_id
    targets = [package / "skill.yaml", package / "SKILL.md", package / "scripts" / "__init__.py"]
    if any(path.exists() for path in targets):
        raise FileExistsError(f"Skill 包已存在: {package}")
    (package / "scripts").mkdir(parents=True, exist_ok=True)
    targets[0].write_text(render_skill_manifest(package_id), encoding="utf-8")
    targets[1].write_text(render_skill_readme(package_id), encoding="utf-8")
    targets[2].write_text('"""Skill 脚本包。"""\n', encoding="utf-8")
    return package


__all__ = [
    "create_agent",
    "create_skill",
    "create_tenant",
    "render_agent_manifest",
    "render_skill_manifest",
    "render_skill_readme",
    "render_tenant_config",
]
