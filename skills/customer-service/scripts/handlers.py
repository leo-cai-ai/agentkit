"""客服 Capability Handler。"""

from __future__ import annotations

from typing import Any

from agentkit.core.contracts import SkillContext


def answer_question(ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
    del args
    agent_context = ctx.request.context.get("agent_context", {})
    knowledge = agent_context.get("knowledge", []) if isinstance(agent_context, dict) else []
    answer = str(knowledge[0]) if knowledge else "已收到你的问题，我会依据客服规则协助处理。"
    return {"answer": answer}


def lookup_order(ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.call_tool("commerce.order.get", args)


def diagnose_logistics(ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
    order = ctx.call_tool("commerce.order.get", args)
    logistics = ctx.call_tool("logistics.track", {"order_id": args["order_id"]})
    return {
        "summary": f"订单 {args['order_id']} 当前物流状态为 {logistics['status']}",
        "evidence": [order, logistics],
    }


def apply_refund(ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
    order_id = str(args["order_id"])
    return ctx.call_tool(
        "refund.submit",
        {
            **args,
            "_idempotency_key": f"refund:{ctx.request.user_id}:{order_id}",
        },
    )
