---
name: customer.answer
description: 使用客服 FAQ、会话与长期 Memory 回答一般问题；支持订单查询、物流跟踪与退款申请。
---

# 客服能力包

提供 FAQ 回答、订单查询、物流诊断和退款申请四个能力。

## 工具

1. `commerce.order.get`：查询当前用户的订单摘要（只读）。
2. `logistics.track`：根据订单号查询物流轨迹（只读）。
3. `refund.submit`：提交一次具有幂等键的退款申请（副作用）。

## 治理

- 查询类工具只读，使用前按权限校验（order.read / logistics.read）。
- 退款提交（`refund.submit`）必须经过人工审批、幂等键，并统一走 ToolExecutor，
  提交前向用户确认退款原因与金额。
- 回答一般问题优先使用客服 FAQ 与长期 Memory，证据不足时如实说明。
