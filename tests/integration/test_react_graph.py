from langgraph.checkpoint.memory import MemorySaver

from agentkit.core.execution.react import ReactStrategy
from tests.unit.test_react_strategy import (
    FakeActionModel,
    _final,
    _react_context,
    _request,
    _tool_action,
)


def test_react_subgraph_writes_checkpoints_and_artifact_references() -> None:
    checkpointer = MemorySaver()
    strategy = ReactStrategy(
        model=FakeActionModel([_tool_action("web.search", {"query": "agent"}), _final("完成")]),
        checkpointer=checkpointer,
    )

    result = strategy.execute(context=_react_context(), request=_request())

    config = {"configurable": {"thread_id": "r1:react:demo.one"}}
    checkpoints = list(checkpointer.list(config))
    assert result.status == "completed"
    assert len(checkpoints) >= 2
    assert result.output["observations"][0].keys() == {
        "tool",
        "summary",
        "artifact_id",
    }
