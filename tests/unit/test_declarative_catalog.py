from __future__ import annotations

from pathlib import Path

import yaml

from agentkit.core.execution.models import (
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    ToolProvider,
)
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.declarative_catalog import (
    load_catalog,
    register_catalog,
    resolve_enabled_agent_ids,
)


def test_catalog_compiles_agent_context_execution_and_mcp_tool(tmp_path: Path) -> None:
    _write_catalog(tmp_path)

    catalog = load_catalog(tmp_path)

    agent = catalog.agents["research"]
    capability = catalog.capabilities["research.explore"]
    tool = catalog.tools["github.search"]
    assert agent.context.rag.enabled is True
    assert agent.execution.default_strategy is ExecutionStrategyName.REACT
    assert capability.execution.reasoning is ReasoningStrategy.REACT
    assert capability.execution.orchestration is OrchestrationMode.SINGLE
    assert tool.provider is ToolProvider.MCP
    assert tool.mcp_server == "github"
    assert tool.mcp_tool == "search_code"


def test_catalog_registers_new_runtime_contracts(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    catalog = load_catalog(tmp_path)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()

    register_catalog(
        catalog,
        enabled_agent_ids={"research"},
        agents=agents,
        skills=skills,
        tools=tools,
    )

    profile = agents.get("research")
    skill = skills.get("research.explore")
    python_tool = tools.get("docs.lookup")
    assert profile.execution_policy.default_strategy is ExecutionStrategyName.REACT
    assert profile.context_policy.rag.collections == ("research-docs",)
    assert profile.instructions == "# 研究 Agent"
    assert not hasattr(profile, "prompt_file")
    assert skill.execution.reasoning is ReasoningStrategy.REACT
    assert python_tool.handler is not None
    assert python_tool.handler({"query": "agent"}) == {"query": "agent"}


def test_enabled_agents_are_explicit_and_validated(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    catalog = load_catalog(tmp_path)

    assert resolve_enabled_agent_ids(catalog, {"enabled_agents": ["research"]}) == {
        "research"
    }


def _write_catalog(
    root: Path,
    *,
    agent_changes: dict | None = None,
    capability_changes: dict | None = None,
    tool_changes: dict | None = None,
) -> None:
    agent_dir = root / "agents" / "research"
    skill_dir = root / "skills" / "research"
    scripts_dir = skill_dir / "scripts"
    agent_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)

    agent = {
        "id": "research",
        "domain": "knowledge.research",
        "description": "企业研究 Agent",
        "skills": ["research.explore"],
        "context": {
            "memory": {
                "enabled": True,
                "scope": "agent_user",
                "window_turns": 6,
                "max_context_tokens": 4000,
            },
            "rag": {
                "enabled": True,
                "collections": ["research-docs"],
                "top_k": 5,
                "max_context_tokens": 1200,
            },
            "artifacts": {"readable": ["report"], "writable": ["report"]},
        },
        "execution": {
            "default_strategy": "react",
            "allowed_strategies": ["direct", "react", "plan_execute"],
            "allow_dynamic_selection": True,
            "allow_side_effects": False,
        },
        "autonomy": {
            "max_model_calls": 12,
            "max_tool_calls": 16,
            "max_iterations": 8,
            "max_plan_steps": 8,
            "max_replans": 1,
            "max_tokens": 30000,
            "timeout_seconds": 300,
        },
        "routing_keywords": ["研究", "调研"],
    }
    agent.update(agent_changes or {})
    (agent_dir / "agent.md").write_text(
        f"---\n{yaml.safe_dump(agent, allow_unicode=True, sort_keys=False)}---\n\n# 研究 Agent\n",
        encoding="utf-8",
    )

    mcp_tool = {
        "id": "github.search",
        "provider": "mcp",
        "server": "github",
        "tool": "search_code",
        "description": "检索代码",
        "risk": "read_only",
        "permissions": ["source.read"],
        "idempotent": True,
        "timeout_seconds": 30,
    }
    mcp_tool.update(tool_changes or {})
    capability = {
        "id": "research.explore",
        "domain": "knowledge.research",
        "description": "检索并汇总资料",
        "entrypoint": "scripts.handlers:explore",
        "execution": {
            "reasoning": "react",
            "orchestration": "single",
            "tool_policy": "read_only",
            "allow_dynamic_selection": True,
        },
        "autonomy": {
            "max_iterations": 5,
            "max_model_calls": 8,
            "max_tool_calls": 8,
            "max_plan_steps": 1,
            "max_replans": 0,
            "max_tokens": 10000,
            "timeout_seconds": 120,
        },
        "permissions": ["source.read"],
        "tools": ["docs.lookup", "github.search"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "keywords": ["研究"],
    }
    capability.update(capability_changes or {})
    package = {
        "package_id": "research",
        "tools": [
            {
                "id": "docs.lookup",
                "provider": "python",
                "entrypoint": "scripts.tools:lookup",
                "description": "查询内部文档",
                "risk": "read_only",
                "permissions": ["source.read"],
                "idempotent": True,
                "timeout_seconds": 10,
            },
            mcp_tool,
        ],
        "capabilities": [capability],
    }
    (skill_dir / "skill.yaml").write_text(
        yaml.safe_dump(package, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
    (scripts_dir / "handlers.py").write_text(
        "def explore(ctx, args):\n    return {'summary': 'ok'}\n", encoding="utf-8"
    )
    (scripts_dir / "tools.py").write_text(
        "def lookup(args):\n    return {'query': args['query']}\n", encoding="utf-8"
    )
