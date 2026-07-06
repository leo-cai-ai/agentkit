from pathlib import Path

from agentkit.runtime.bootstrap import build_runtime


def test_build_runtime_registers_expected_components(tmp_path):
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")

    skill_names = {s.name for s in runtime.gateway.skills.all()}
    agent_names = {a.name for a in runtime.gateway.agents.all()}

    assert "candidate.rank" in skill_names
    assert agent_names == {
        "general_agent",
        "customer_service",
        "hr_recruiter",
        "xhs_growth",
    }
    assert runtime.tenant_config["tenant_id"]
    assert runtime.manifest
    assert runtime.manifest["tenant_config"]["sha256"]
    assert runtime.tenant_config["runtime_manifest"] == runtime.manifest
    assert (tmp_path / "audit.sqlite").exists()


def test_legacy_runtime_is_removed() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    forbidden = [
        "actions_enabled",
        "ExecutionMode",
        "pack_registry",
        "domain_packs",
        "PlanExecutor",
        "ChatService",
        "deterministic_fastpath",
        "combined_intent_route",
        "fastpath_active",
        "combined_route_active",
        "enabled_domains",
        "execution_mode",
        "chat_agents",
        "domain_personas",
        "routing_hints",
    ]
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (repo_root / "src" / "agentkit").rglob("*.py")
    )
    for symbol in forbidden:
        assert symbol not in sources, symbol
