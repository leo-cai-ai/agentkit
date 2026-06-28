from agentkit.runtime.bootstrap import build_runtime


def test_build_runtime_registers_expected_components(tmp_path):
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")

    skill_names = {s.name for s in runtime.gateway.skills.all()}
    agent_names = {a.name for a in runtime.gateway.agents.all()}

    assert "candidate.rank" in skill_names
    assert {"router", "general", "hr_recruiter"} <= agent_names
    assert runtime.tenant_config["tenant_id"]
    assert runtime.manifest
    assert runtime.manifest["tenant_config"]["sha256"]
    assert runtime.tenant_config["runtime_manifest"] == runtime.manifest
    assert (tmp_path / "audit.sqlite").exists()
