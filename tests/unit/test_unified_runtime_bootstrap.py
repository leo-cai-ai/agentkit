from agentkit.runtime.bootstrap import build_runtime


def test_runtime_registers_only_enabled_business_agents(tmp_path) -> None:
    runtime = build_runtime(tenant_id="company_alpha", db_path=tmp_path / "runtime.sqlite")

    agents = {agent.name: agent for agent in runtime.gateway.agents.all()}
    assert set(agents) == {
        "general_agent",
        "customer_service",
        "hr_recruiter",
        "xhs_growth",
    }
    assert agents["customer_service"].context_policy.rag.enabled is True
    assert agents["xhs_growth"].context_policy.rag.enabled is False
    assert runtime.contexts.get("runtime.intent").model.id == "runtime.intent"
    assert runtime.context_invoker.manifest_hash == runtime.manifest["contexts"]["manifest_hash"]
    assert len(runtime.manifest["contexts"]["packs"]) == 15
    assert "prompt_files" not in runtime.manifest
    assert runtime.chat_service is not None


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


def test_runtime_startup_reconciles_stale_queued_attempts(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    first = build_runtime(tenant_id="company_alpha", db_path=db_path)
    accepted = first.conversations.accept_turn(
        tenant_id=first.tenant_config["tenant_id"],
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="恢复",
        client_message_id="stale-startup",
        user_content="持久化后进程退出",
        user_token_estimate=8,
    )
    with first.conversations._connect() as connection:
        connection.execute(
            "UPDATE conversation_attempts SET started_at = 0 WHERE id = ?",
            (accepted.attempt_id,),
        )

    restarted = build_runtime(tenant_id="company_alpha", db_path=db_path)

    assert restarted.conversation_recovery is not None
    assert restarted.conversations.get_attempt(accepted.attempt_id)["status"] == "interrupted"
