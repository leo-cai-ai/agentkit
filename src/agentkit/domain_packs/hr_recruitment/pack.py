"""HR 声明式 Agent 的过渡兼容桥接层。"""

from __future__ import annotations

from pathlib import Path

from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.declarative_catalog import load_catalog, register_catalog

DOMAIN = "hr.recruitment"


def register(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    tenant_config: dict,
) -> None:
    """供旧 Pack 发现器调用，业务定义始终来自声明式目录。"""
    del tenant_config
    root = Path(__file__).resolve().parents[4]
    catalog = load_catalog(root)
    register_catalog(
        catalog,
        enabled_agent_ids={"hr_recruiter"},
        agents=agents,
        skills=skills,
        tools=tools,
    )
