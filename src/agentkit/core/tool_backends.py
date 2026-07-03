"""Python 与 MCP Tool 的统一执行后端。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .contracts import ToolDefinition
from .execution.models import ToolProvider


class ToolBackendError(RuntimeError):
    """Tool 后端缺失、配置错误或返回非法结果。"""


class McpClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ToolExecutionBackend(Protocol):
    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]: ...


class PythonToolBackend:
    """执行声明式 Python Handler。"""

    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        if tool.handler is None:
            raise ToolBackendError(f"Python Tool {tool.name} 没有 Handler")
        result = tool.handler(args)
        if not isinstance(result, dict):
            raise ToolBackendError(f"Python Tool {tool.name} 必须返回对象")
        return result


class McpToolBackend:
    """通过已配置的 MCP Client 调用远端 Tool。"""

    def __init__(self, clients: Mapping[str, McpClient]) -> None:
        self._clients = dict(clients)

    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        server = tool.mcp_server or ""
        client = self._clients.get(server)
        if client is None:
            raise ToolBackendError(f"MCP Server 未配置: {server or '<empty>'}")
        if not tool.mcp_tool:
            raise ToolBackendError(f"MCP Tool {tool.name} 未声明远端工具名")
        result = client.call_tool(tool.mcp_tool, args)
        if not isinstance(result, dict):
            raise ToolBackendError(f"MCP Tool {tool.name} 必须返回对象")
        return result


class ToolBackendRegistry:
    """根据 Provider 获取唯一执行后端。"""

    def __init__(self, backends: Mapping[ToolProvider, ToolExecutionBackend]) -> None:
        self._backends = dict(backends)

    def get(self, provider: ToolProvider) -> ToolExecutionBackend:
        try:
            return self._backends[provider]
        except KeyError as exc:
            raise ToolBackendError(f"未注册 Tool Provider: {provider.value}") from exc


class StdioMcpClient:
    """官方 MCP Python SDK 的同步 stdio 适配器。

    每次调用建立并关闭独立 Session，避免跨线程共享异步连接。Runtime 后续可通过
    Client 工厂替换为长连接实现，但治理边界保持不变。
    """

    def __init__(self, *, command: str, args: list[str] | None = None) -> None:
        self._command = command
        self._args = list(args or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        import anyio

        async def invoke() -> dict[str, Any]:
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
            except ImportError as exc:  # pragma: no cover - 取决于可选依赖
                raise ToolBackendError("未安装 MCP 可选依赖，请安装 agentkit[mcp]") from exc

            parameters = StdioServerParameters(command=self._command, args=self._args)
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)
            if bool(getattr(result, "isError", False)):
                raise ToolBackendError(f"MCP Tool {name} 返回错误")
            structured = getattr(result, "structuredContent", None)
            if isinstance(structured, dict):
                return dict(structured)
            content = getattr(result, "content", [])
            return {
                "content": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item)
                    for item in content
                ]
            }

        return anyio.run(invoke)


__all__ = [
    "McpClient",
    "McpToolBackend",
    "PythonToolBackend",
    "StdioMcpClient",
    "ToolBackendError",
    "ToolBackendRegistry",
    "ToolExecutionBackend",
]
