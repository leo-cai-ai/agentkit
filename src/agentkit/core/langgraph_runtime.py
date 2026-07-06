"""LangGraph 1.x 调用协议适配。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def invoke_graph_v2(
    graph: Any,
    inputs: Any,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """使用 v2 协议调用图，并只向业务层返回状态值。"""
    output = graph.invoke(inputs, config=dict(config or {}), version="v2")
    value = output.value
    if not isinstance(value, dict):
        raise TypeError("LangGraph v2 状态输出必须是 dict")
    return value
