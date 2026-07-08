from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agentkit.core.langgraph_runtime import invoke_graph_v2


class _State(TypedDict):
    value: int


def test_invoke_graph_v2_returns_state_value() -> None:
    builder = StateGraph(_State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)

    result = invoke_graph_v2(builder.compile(), {"value": 1})

    assert result == {"value": 2}
