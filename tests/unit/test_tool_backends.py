from __future__ import annotations

from typing import Any

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import ToolDefinition
from agentkit.core.execution.models import ToolPolicy, ToolProvider, ToolRisk
from agentkit.core.tool_backends import (
    McpToolBackend,
    PythonToolBackend,
    ToolBackendError,
    ToolBackendRegistry,
)
from agentkit.core.tool_executor import (
    ToolExecutor,
    ToolPermissionError,
    ToolRiskError,
    ToolSchemaError,
)


class FakeMcpClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self.result


def _registry(client: FakeMcpClient | None = None) -> ToolBackendRegistry:
    return ToolBackendRegistry(
        {
            ToolProvider.PYTHON: PythonToolBackend(),
            ToolProvider.MCP: McpToolBackend({"github": client or FakeMcpClient({})}),
        }
    )


def _mcp_tool() -> ToolDefinition:
    return ToolDefinition(
        name="github.search",
        domain="knowledge",
        description="搜索代码",
        provider=ToolProvider.MCP,
        risk=ToolRisk.READ_ONLY,
        permissions=["source.read"],
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
        mcp_server="github",
        mcp_tool="search_code",
    )


def test_executor_routes_mcp_tool_through_same_governance() -> None:
    audit = InMemoryAuditLog()
    client = FakeMcpClient({"items": ["a.py"]})
    executor = ToolExecutor(
        tenant_id="t1",
        audit=audit,
        run_id="r1",
        backends=_registry(client),
        permissions={"source.read"},
        tool_policy=ToolPolicy.READ_ONLY,
    )

    result = executor.call(_mcp_tool(), {"query": "AgentProfile"})

    assert result == {"items": ["a.py"]}
    assert client.calls == [("search_code", {"query": "AgentProfile"})]
    assert "tool_call_finished" in [item["type"] for item in audit.events_for("r1")]


def test_executor_rejects_tool_without_permission() -> None:
    executor = ToolExecutor(
        tenant_id="t1",
        backends=_registry(),
        permissions=set(),
        tool_policy=ToolPolicy.READ_ONLY,
    )

    with pytest.raises(ToolPermissionError, match="source.read"):
        executor.call(_mcp_tool(), {"query": "AgentProfile"})


def test_executor_validates_input_schema_before_backend() -> None:
    client = FakeMcpClient({"items": []})
    executor = ToolExecutor(
        tenant_id="t1",
        backends=_registry(client),
        permissions={"source.read"},
        tool_policy=ToolPolicy.READ_ONLY,
    )

    with pytest.raises(ToolSchemaError):
        executor.call(_mcp_tool(), {"query": 3})
    assert client.calls == []


def test_read_only_policy_rejects_side_effect_before_backend() -> None:
    calls: list[dict[str, Any]] = []
    tool = ToolDefinition(
        name="refund.submit",
        domain="support",
        description="提交退款",
        handler=lambda args: calls.append(args) or {"ok": True},
        provider=ToolProvider.PYTHON,
        risk=ToolRisk.SIDE_EFFECT,
        permissions=["refund.write"],
    )
    executor = ToolExecutor(
        tenant_id="t1",
        backends=_registry(),
        permissions={"refund.write"},
        tool_policy=ToolPolicy.READ_ONLY,
    )

    with pytest.raises(ToolRiskError):
        executor.call(tool, {"order_id": "O-1"})
    assert calls == []


def test_backend_registry_rejects_unregistered_provider() -> None:
    registry = ToolBackendRegistry({ToolProvider.PYTHON: PythonToolBackend()})

    with pytest.raises(ToolBackendError, match="未注册"):
        registry.get(ToolProvider.MCP)
