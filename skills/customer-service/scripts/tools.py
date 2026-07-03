"""客服示例 Tool；生产部署可替换为企业订单与退款 API。"""

from __future__ import annotations

from typing import Any


def get_order(args: dict[str, Any]) -> dict[str, Any]:
    order_id = str(args["order_id"])
    return {
        "order_id": order_id,
        "status": "shipped",
        "tracking_id": f"TRK-{order_id}",
        "owner_user_id": str(args.get("user_id") or "current-user"),
    }


def track_logistics(args: dict[str, Any]) -> dict[str, Any]:
    order_id = str(args["order_id"])
    return {
        "order_id": order_id,
        "status": "delayed",
        "last_event": "包裹已到达区域分拨中心",
        "estimated_delivery": "2 days",
    }


def submit_refund(args: dict[str, Any]) -> dict[str, Any]:
    order_id = str(args["order_id"])
    return {
        "refund_id": f"RF-{order_id}",
        "order_id": order_id,
        "status": "submitted",
        "reason": str(args.get("reason") or "用户申请退款"),
    }
