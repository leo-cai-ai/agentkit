"""Runtime wiring for durable execution state."""

from __future__ import annotations

import json
import os
import uuid

import pytest

import agentkit.config as config_mod
import agentkit.core.llm_client as llm_client
from agentkit.config import Settings
from agentkit.core import pg
from agentkit.core.artifacts import build_artifact_store
from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import (
    IntentFrame,
    PlanStep,
    RouteDecision,
    SkillDefinition,
    TaskPlan,
    TaskRequest,
    ToolDefinition,
)
from agentkit.core.executor import PlanExecutor
from agentkit.core.idempotency import build_idempotency_store
from agentkit.core.migrations import run_postgres_migrations
from agentkit.core.policy import PolicyGuard
from agentkit.core.registry import SkillRegistry, ToolRegistry
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


def _postgres_contract_scope() -> str:
    return uuid.uuid4().hex


def test_postgres_contract_scope_is_unique() -> None:
    first = _postgres_contract_scope()
    second = _postgres_contract_scope()

    assert first != second
    assert f"contract-{first}" != f"contract-{second}"
    assert f"{first}-artifact-run" != f"{second}-artifact-run"
    assert f"{first}-contract-key" != f"{second}-contract-key"


@pytest.mark.skipif(
    not os.getenv("AGENTKIT_TEST_PG_DSN"),
    reason="AGENTKIT_TEST_PG_DSN is not configured",
)
def test_postgres_storage_contracts() -> None:
    scope = _postgres_contract_scope()
    tenant_id = f"contract-{scope}"
    run_id = f"{scope}-artifact-run"
    idempotency_key = f"{scope}-contract-key"
    settings = Settings(
        _env_file=None,
        storage_backend="postgres",
        pg_dsn=os.environ["AGENTKIT_TEST_PG_DSN"],
    )
    run_postgres_migrations(settings)

    try:
        artifacts = build_artifact_store(
            backend="postgres",
            tenant_id=tenant_id,
            run_id=run_id,
            settings=settings,
        )
        written = artifacts.put(kind="contract.value", payload={"value": 1})
        assert artifacts.get(written.artifact_id).payload == {"value": 1}

        store = build_idempotency_store(
            backend="postgres",
            tenant_id=tenant_id,
            settings=settings,
        )
        claim = store.begin(
            tool_name="contract.write",
            idempotency_key=idempotency_key,
            args={"value": 1},
        )
        assert claim.claimed is True
        store.finish_success(claim, {"written": True})

        cached = build_idempotency_store(
            backend="postgres",
            tenant_id=tenant_id,
            settings=settings,
        ).begin(
            tool_name="contract.write",
            idempotency_key=idempotency_key,
            args={"value": 1},
        )
        assert cached.claimed is False
        assert cached.result == {"written": True}
    finally:
        with pg.connection(settings) as conn:
            conn.execute(
                "DELETE FROM workflow_artifacts WHERE tenant_id = %s",
                (tenant_id,),
            )
            conn.execute(
                "DELETE FROM tool_idempotency_records WHERE tenant_id = %s",
                (tenant_id,),
            )


def test_runtime_artifact_factory_persists_and_audits_without_payload(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENTKIT_STORAGE_BACKEND", "sqlite")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "runtime.sqlite")
        run_id = runtime.gateway.audit.start_run(
            tenant_id=runtime.tenant_config["tenant_id"],
            user_id="user-1",
            text="persist artifact",
        )

        factory = runtime.gateway._executor._artifact_store_factory
        assert factory is not None
        written = factory(run_id).put(
            kind="workflow.result",
            payload={"secret": "payload", "value": 7},
            summary="Stored workflow result",
        )

        restored = factory(run_id).get(written.artifact_id)
        assert restored.payload == {"secret": "payload", "value": 7}

        events = runtime.gateway.audit.events_for(run_id)
        assert [event["type"] for event in events].count("artifact_written") == 1
        persisted = [event for event in events if event["type"] == "artifact_persisted"]
        assert [event["payload"] for event in persisted] == [
            {
                "artifact_id": written.artifact_id,
                "kind": "workflow.result",
                "payload_sha256": written.payload_sha256,
                "payload_bytes": written.payload_bytes,
                "backend": "sqlite",
            }
        ]
        assert "secret" not in str(persisted)
    finally:
        config_mod.get_settings.cache_clear()


def test_injected_durable_ledger_reuses_keyed_mutation_across_executors(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(
            responder=lambda _system, _user: json.dumps(
                {"execution_goal": "mutate", "expected_outputs": [], "risks": []}
            )
        ),
    )
    calls = {"count": 0}

    def mutate(args: dict) -> dict:
        calls["count"] += 1
        return {"value": args["value"], "execution": calls["count"]}

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="demo.mutate",
            domain="demo",
            description="",
            handler=mutate,
        )
    )
    skills = SkillRegistry()
    skills.register(
        SkillDefinition(
            name="demo.workflow",
            domain="demo",
            description="",
            input_schema={},
            output_schema={},
            permissions=[],
            execution_mode="plan_execute",
            tools=["demo.mutate"],
            handler=lambda ctx, args: {
                "mutation": ctx.call_tool(
                    "demo.mutate",
                    {"value": args["value"], "_idempotency_key": "mutation-1"},
                )
            },
        )
    )
    tenant_config: dict = {}
    ledger = build_idempotency_store(
        backend="sqlite",
        tenant_id="tenant-a",
        sqlite_path=tmp_path / "runtime.sqlite",
    )

    def executor() -> PlanExecutor:
        return PlanExecutor(
            tenant_id="tenant-a",
            tenant_config=tenant_config,
            skills=skills,
            tools=tools,
            policy=PolicyGuard(tenant_config),
            audit=InMemoryAuditLog(),
            idempotency_store=ledger,
        )

    request = TaskRequest(user_id="user-1", roles=[], text="mutate")
    plan = TaskPlan(
        route=RouteDecision(skill_name="demo.workflow", reason="test"),
        steps=[
            PlanStep(
                step_id=1,
                skill_name="demo.workflow",
                mode="plan_execute",
                args={"value": 7},
            )
        ],
    )
    intent = IntentFrame(
        raw_text="mutate",
        language="en",
        intent_type="business_task",
        goal="mutate",
        boundaries={},
        entities={},
        target={"kind": "business_skill", "name": "demo.workflow"},
    )

    first = executor().execute(run_id="run-a", request=request, plan=plan, intent=intent)
    assert first["final"] == {
        "mutation": {"value": 7, "execution": 1}
    }
    second = executor().execute(run_id="run-b", request=request, plan=plan, intent=intent)
    assert second["final"] == {
        "mutation": {"value": 7, "execution": 1}
    }
    assert calls == {"count": 1}
