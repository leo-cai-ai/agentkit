"""声明式 Agent 与 Skill 目录加载测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.declarative_catalog import load_catalog, register_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_load_catalog_parses_agent_context_and_capabilities(tmp_path: Path) -> None:
    """加载器会解析 Agent 上下文策略和 Skill capability。"""
    (tmp_path / "agents" / "hr-recruiter").mkdir(parents=True)
    (tmp_path / "skills" / "candidate-rank" / "scripts").mkdir(parents=True)
    (tmp_path / "agents" / "hr-recruiter" / "agent.md").write_text(
        "---\n"
        "id: hr_recruiter\n"
        "domain: hr.recruitment\n"
        "description: 招聘助手\n"
        "skills: [candidate.rank]\n"
        "context:\n"
        "  memory_scope: agent_user\n"
        "  session_key: tenant/agent/user/thread\n"
        "  knowledge_collections: [recruitment-policy]\n"
        "  readable_artifact_kinds: []\n"
        "  writable_artifact_kinds: []\n"
        "---\n\n# 招聘助手\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "candidate-rank" / "skill.yaml").write_text(
        "package_id: candidate-rank\n"
        "tools: []\n"
        "capabilities:\n"
        "  - id: candidate.rank\n"
        "    domain: hr.recruitment\n"
        "    description: 候选人排序\n"
        "    entrypoint: scripts.handler:run\n"
        "    execution_mode: plan_execute\n"
        "    permissions: []\n"
        "    tools: []\n"
        "    input_schema: {type: object}\n"
        "    output_schema: {type: object}\n"
        "    keywords: [候选人]\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "candidate-rank" / "scripts" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "skills" / "candidate-rank" / "scripts" / "handler.py").write_text(
        "def run(ctx, args):\n    return args\n", encoding="utf-8"
    )

    catalog = load_catalog(tmp_path)

    assert catalog.agents["hr_recruiter"].context["memory_scope"] == "agent_user"
    assert catalog.capabilities["candidate.rank"].package_id == "candidate-rank"


def test_catalog_rejects_entrypoint_outside_scripts(tmp_path: Path) -> None:
    """声明不可把可执行入口指向 Skill 包外部。"""
    _write_valid_catalog(tmp_path, entrypoint="../outside:run")

    with pytest.raises(ValueError, match="scripts 目录"):
        load_catalog(tmp_path)


def test_register_catalog_derives_agent_tools_from_capabilities(tmp_path: Path) -> None:
    """Agent 的工具白名单必须由引用的 capability 推导。"""
    _write_valid_catalog(tmp_path)
    catalog = load_catalog(tmp_path)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()

    register_catalog(
        catalog,
        enabled_agent_ids={"hr_recruiter"},
        agents=agents,
        skills=skills,
        tools=tools,
    )

    assert agents.get("hr_recruiter").allowed_skills == ["candidate.rank"]
    assert agents.get("hr_recruiter").allowed_tools == ["ats.get_job", "ats.get_candidates"]
    assert callable(skills.get("candidate.rank").handler)


def test_hr_manifest_compiles_existing_candidate_rank_contract() -> None:
    """HR 声明迁移后必须保持原有 Skill 契约。"""
    catalog = load_catalog(REPO_ROOT)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()

    register_catalog(
        catalog,
        enabled_agent_ids={"hr_recruiter"},
        agents=agents,
        skills=skills,
        tools=tools,
    )

    profile = agents.get("hr_recruiter")
    skill = skills.get("candidate.rank")
    assert profile.domain == "hr.recruitment"
    assert profile.allowed_skills == ["candidate.rank"]
    assert profile.allowed_tools == ["ats.get_job", "ats.get_candidates"]
    assert skill.permissions == ["hr.job.read", "hr.candidate.read"]
    assert skill.execution_mode == "plan_execute"
    assert skill.batch_key == "candidate_ids"
    assert skill.tools == ["ats.get_job", "ats.get_candidates"]


def test_customer_service_manifest_has_no_business_capabilities() -> None:
    """客服 Agent 仅使用会话运行时，不注册业务 capability。"""
    catalog = load_catalog(REPO_ROOT)

    agent = catalog.agents["customer_service"]

    assert agent.skills == ()
    assert agent.context["memory_scope"] == "agent_user"


def _write_valid_catalog(tmp_path: Path, *, entrypoint: str = "scripts.handler:run") -> None:
    """写入一个包含两个工具和一个 capability 的最小有效目录。"""
    (tmp_path / "agents" / "hr-recruiter").mkdir(parents=True)
    scripts = tmp_path / "skills" / "candidate-rank" / "scripts"
    scripts.mkdir(parents=True)
    (tmp_path / "agents" / "hr-recruiter" / "agent.md").write_text(
        "---\n"
        "id: hr_recruiter\n"
        "domain: hr.recruitment\n"
        "description: 招聘助手\n"
        "skills: [candidate.rank]\n"
        "context:\n"
        "  memory_scope: agent_user\n"
        "  session_key: tenant/agent/user/thread\n"
        "  knowledge_collections: []\n"
        "  readable_artifact_kinds: []\n"
        "  writable_artifact_kinds: []\n"
        "---\n\n# 招聘助手\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "candidate-rank" / "skill.yaml").write_text(
        "package_id: candidate-rank\n"
        "tools:\n"
        "  - id: ats.get_job\n"
        "    description: 获取岗位\n"
        "    entrypoint: scripts.tools:get_job\n"
        "  - id: ats.get_candidates\n"
        "    description: 获取候选人\n"
        "    entrypoint: scripts.tools:get_candidates\n"
        "    supports_batch: true\n"
        "capabilities:\n"
        "  - id: candidate.rank\n"
        "    domain: hr.recruitment\n"
        "    description: 候选人排序\n"
        f"    entrypoint: {entrypoint}\n"
        "    execution_mode: plan_execute\n"
        "    permissions: [hr.job.read, hr.candidate.read]\n"
        "    tools: [ats.get_job, ats.get_candidates]\n"
        "    input_schema: {type: object}\n"
        "    output_schema: {type: object}\n"
        "    keywords: [候选人]\n",
        encoding="utf-8",
    )
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "handler.py").write_text(
        "def run(ctx, args):\n    return {'result': args}\n", encoding="utf-8"
    )
    (scripts / "tools.py").write_text(
        "def get_job(args):\n    return {'job_id': args['job_id']}\n\n"
        "def get_candidates(args):\n    return {'candidate_ids': args['candidate_ids']}\n",
        encoding="utf-8",
    )
