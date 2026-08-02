"""红楼梦知识图谱 Capability Handler。"""

from __future__ import annotations

from typing import Any

from agentkit.core.contracts import SkillContext


def answer_question(ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
    """端到端问答：实体解析 -> Text2Cypher/邻域检索 -> LLM 合成回答。"""
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("hongloumeng.qa 需要提供 question")
    return ctx.call_tool("hlm.kg.ask", {"question": question})


__all__ = ["answer_question"]
