from types import SimpleNamespace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agentkit.core.contracts import TaskRequest, ToolDefinition
from agentkit.core.execution.models import StrategyResult
from agentkit.core.langgraph_agent import UnifiedAgentGraph
from agentkit.core.langgraph_runtime import invoke_graph_v2
from agentkit.core.registry import ToolRegistry


class _State(TypedDict):
    value: int


def test_invoke_graph_v2_returns_state_value() -> None:
    builder = StateGraph(_State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)

    result = invoke_graph_v2(builder.compile(), {"value": 1})

    assert result == {"value": 2}


def test_deferred_approval_injects_action_level_tool_idempotency_key(monkeypatch) -> None:
    calls: list[dict] = []

    class CapturingExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        def call(self, tool, args):
            calls.append(dict(args))
            return {"published": True}

    monkeypatch.setattr("agentkit.core.langgraph_agent.ToolExecutor", CapturingExecutor)
    tools = ToolRegistry()
    tools.register(ToolDefinition(name="notice.publish", domain="hr", description="publish"))
    graph = object.__new__(UnifiedAgentGraph)
    graph._tenant_id = "tenant-a"
    graph._tenant_config = {}
    graph._audit = None
    graph._tool_backends = None
    graph._idempotency_store = None
    graph._tools = tools
    result = StrategyResult(
        status="deferred_action",
        output={
            "deferred_action": {
                "tool_name": "notice.publish",
                "arguments": {"candidate_id": "candidate-1"},
            }
        },
    )

    projected = graph._deferred_approval(
        {
            "result": result,
            "resolution": SimpleNamespace(primary_skill="candidate.rank"),
            "request": TaskRequest(
                user_id="u1",
                roles=["recruiter"],
                text="publish",
                context={
                    "approved_skills": ["candidate.rank"],
                    "approval_decision": {"tool_idempotency_key": "approval:action-1:command-1"},
                },
            ),
            "run_id": "run-1",
            "approval_required": ["candidate.rank"],
        }
    )

    assert projected["result"].status == "completed"
    assert calls == [
        {
            "candidate_id": "candidate-1",
            "_idempotency_key": "approval:action-1:command-1:0:notice.publish",
        }
    ]
