---
id: xhs_growth
domain: marketing.social_growth
description: 小红书研究、策略、文案、审核、发布准备和指标跟踪 Agent。
prompt_file: prompts/agents/social_growth.md
skills:
  - xhs.growth.campaign
  - xhs.trend.research
  - xhs.case.extract
  - xhs.case.compare
  - xhs.strategy.plan
  - xhs.copy.generate
  - xhs.copy.review
  - xhs.publish.prepare
  - xhs.metrics.track
context:
  memory: {enabled: true, scope: agent_user, window_turns: 6, max_context_tokens: 4000, retrieval_k: 4}
  rag: {enabled: false, collections: [], top_k: 3, max_context_tokens: 600}
  artifacts:
    readable: [xhs.trend.research, xhs.case.extract, xhs.case.compare, xhs.strategy.plan, xhs.copy.generate, xhs.copy.review, xhs.publish.prepare, xhs.metrics.track]
    writable: [xhs.trend.research, xhs.case.extract, xhs.case.compare, xhs.strategy.plan, xhs.copy.generate, xhs.copy.review, xhs.publish.prepare, xhs.metrics.track]
execution:
  default_strategy: workflow
  allowed_strategies: [direct, workflow, react, plan_execute]
  allow_dynamic_selection: true
  allow_side_effects: true
autonomy:
  max_model_calls: 20
  max_tool_calls: 24
  max_iterations: 10
  max_plan_steps: 12
  max_replans: 1
  max_tokens: 50000
  timeout_seconds: 900
routing_keywords: [小红书, 涨粉, 笔记, 选题, 发布]
---

# 小红书增长 Agent

研究步骤可以使用只读 ReAct；完整活动使用固定 Workflow。发布只允许提交人工审核后冻结的内容、哈希和幂等键。
