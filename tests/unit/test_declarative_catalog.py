"""声明式 Agent 与 Skill 目录加载测试。"""

from __future__ import annotations

from pathlib import Path

from agentkit.runtime.declarative_catalog import load_catalog


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
