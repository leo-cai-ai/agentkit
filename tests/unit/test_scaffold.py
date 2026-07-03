"""声明式 Agent、Skill 和租户骨架测试。"""

from __future__ import annotations

import json

import pytest

from agentkit.runtime import scaffold


def test_render_tenant_config_uses_explicit_agents() -> None:
    data = json.loads(scaffold.render_tenant_config("acme"))
    assert data == {
        "tenant_id": "acme",
        "enabled_agents": [],
        "role_permissions": {},
        "principal_business_roles": {},
        "prompt_files": {},
        "mcp_servers": {},
    }


def test_create_agent_writes_single_manifest(tmp_path) -> None:
    path = scaffold.create_agent("finance_agent", root=tmp_path / "agents")
    assert path == tmp_path / "agents" / "finance_agent" / "agent.md"
    assert "id: finance_agent" in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        scaffold.create_agent("finance_agent", root=tmp_path / "agents")


def test_create_skill_writes_portable_package(tmp_path) -> None:
    package = scaffold.create_skill("invoice-query", root=tmp_path / "skills")
    files = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file()
    }
    assert files == {
        "SKILL.md",
        "scripts/__init__.py",
        "skill.yaml",
    }
    with pytest.raises(FileExistsError):
        scaffold.create_skill("invoice-query", root=tmp_path / "skills")


def test_invalid_ids_are_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        scaffold.create_agent("Bad Agent", root=tmp_path)
