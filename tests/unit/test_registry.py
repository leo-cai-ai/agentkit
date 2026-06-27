from agentkit.core.contracts import ToolDefinition
from agentkit.core.registry import ToolRegistry


def _tool(name):
    return ToolDefinition(name=name, domain="d", description="", handler=lambda args: {})


def test_register_get_and_all():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("b"))
    assert reg.get("a").name == "a"
    assert {t.name for t in reg.all()} == {"a", "b"}


def test_subset_returns_requested_tools():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("b"))
    subset = reg.subset(["a"])
    assert list(subset.keys()) == ["a"]
