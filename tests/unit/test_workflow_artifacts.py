from agentkit.core.artifacts import InMemoryArtifactStore
from agentkit.core.contracts import SkillContext, TaskRequest, ToolDefinition
from agentkit.core.workflow import WorkflowRunner


def test_artifact_store_returns_refs_and_keeps_payloads_out_of_refs():
    store = InMemoryArtifactStore()
    record = store.put(
        kind="xhs.case.compare",
        payload={"large": ["payload"]},
        summary="Compared cases.",
        metadata={"step": "compare"},
    )

    assert store.get(record.artifact_id).payload == {"large": ["payload"]}
    assert record.ref() == {
        "artifact_id": record.artifact_id,
        "kind": "xhs.case.compare",
        "summary": "Compared cases.",
        "metadata": {"step": "compare"},
        "created_at": record.created_at,
        "payload_sha256": record.payload_sha256,
        "payload_bytes": record.payload_bytes,
    }


def test_workflow_runner_scopes_tools_and_writes_artifact():
    seen = {}

    def allowed_tool(args):
        return {"ok": args["value"]}

    def blocked_tool(args):
        return {"blocked": True}

    def step(ctx, args):
        seen["tools"] = sorted(ctx.tools)
        return {
            "summary": "step done",
            "value": ctx.call_tool("allowed.tool", {"value": args["value"]})["ok"],
        }

    parent = SkillContext(
        tenant_id="t",
        tenant_selector="company_alpha",
        run_id="r1",
        agent=object(),  # type: ignore[arg-type]
        skill=object(),  # type: ignore[arg-type]
        tenant_config={},
        tools={
            "allowed.tool": ToolDefinition(
                name="allowed.tool",
                domain="demo",
                description="",
                handler=allowed_tool,
            ),
            "blocked.tool": ToolDefinition(
                name="blocked.tool",
                domain="demo",
                description="",
                handler=blocked_tool,
            ),
        },
        request=TaskRequest(user_id="u", roles=[], text="run"),
        context_invoker=object(),
        artifacts=InMemoryArtifactStore(),
    )

    result = WorkflowRunner(parent).run_step(
        step_name="demo.step",
        handler=step,
        args={"value": 3},
        allowed_tools=["allowed.tool"],
        artifact_kind="demo.step",
    )

    assert seen["tools"] == ["allowed.tool"]
    assert result.output["value"] == 3
    assert result.artifact
    assert parent.artifacts.get(result.artifact["artifact_id"]).payload["value"] == 3
