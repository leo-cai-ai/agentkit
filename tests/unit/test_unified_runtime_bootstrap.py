from agentkit.runtime.bootstrap import build_runtime


def test_runtime_registers_only_enabled_business_agents(tmp_path) -> None:
    runtime = build_runtime(tenant_id="company_alpha", db_path=tmp_path / "runtime.sqlite")

    agents = {agent.name: agent for agent in runtime.gateway.agents.all()}
    assert set(agents) == {"customer_service", "hr_recruiter", "xhs_growth"}
    assert agents["customer_service"].context_policy.rag.enabled is True
    assert agents["xhs_growth"].context_policy.rag.enabled is False
    assert runtime.contexts.get("runtime.intent").model.id == "runtime.intent"
    assert runtime.context_invoker.manifest_hash == runtime.manifest["contexts"]["manifest_hash"]
    assert len(runtime.manifest["contexts"]["packs"]) == 8
    assert "prompt_files" not in runtime.manifest
    assert runtime.chat_service is None


def test_runtime_exposes_unified_strategy_catalog(tmp_path) -> None:
    runtime = build_runtime(tenant_id="company_alpha", db_path=tmp_path / "runtime.sqlite")

    assert set(runtime.strategy_names) == {
        "direct",
        "workflow",
        "batch",
        "parallel",
        "react",
        "plan_execute",
    }
