---
schema_version: 1
release_version: 1.0.0
id: customer_service
domain: support.customer_service
description: 客服问答、订单查询、物流诊断与退款处理 Agent。
skills:
  - customer.answer
  - order.lookup
  - logistics.diagnose
  - refund.apply
context:
  memory:
    enabled: true
    scope: agent_user
    window_turns: 6
    max_context_tokens: 4000
    retrieval_k: 4
  rag:
    enabled: true
    collections: [customer-service-faq]
    top_k: 5
    max_context_tokens: 1200
  artifacts:
    readable: [support-case]
    writable: [support-case]
execution:
  default_strategy: direct
  allowed_strategies: [direct, workflow, react, plan_execute]
  allow_dynamic_selection: true
  allow_side_effects: true
autonomy:
  max_model_calls: 12
  max_tool_calls: 16
  max_iterations: 8
  max_plan_steps: 8
  max_replans: 1
  max_tokens: 30000
  timeout_seconds: 300
routing_keywords: [客服, 售后, 订单, 物流, 退款]
---

# 客服 Agent

仅使用当前租户、Agent、用户与会话作用域内的 Memory、客服知识和订单工具。
退款等副作用必须在固定 Workflow 中等待人工审批，禁止在 ReAct 循环中直接执行。
