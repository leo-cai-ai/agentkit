---
id: hr_recruiter
domain: hr.recruitment
description: 招聘筛选与候选人排序 Agent。
prompt_file: prompts/agents/recruitment.md
skills: [candidate.rank]
context:
  memory: {enabled: true, scope: agent_user, window_turns: 6, max_context_tokens: 4000, retrieval_k: 4}
  rag: {enabled: true, collections: [recruitment-policy, job-requisitions], top_k: 5, max_context_tokens: 1200}
  artifacts:
    readable: [candidate-ranking-report]
    writable: [candidate-ranking-report]
execution:
  default_strategy: direct
  allowed_strategies: [direct, batch, plan_execute]
  allow_dynamic_selection: true
  allow_side_effects: false
autonomy:
  max_model_calls: 12
  max_tool_calls: 20
  max_iterations: 8
  max_plan_steps: 8
  max_replans: 1
  max_tokens: 30000
  timeout_seconds: 300
routing_hints: [招聘, 候选人, 简历, 排序]
---

# 招聘 Agent

只读取当前租户授权的招聘知识、职位和候选人数据，不得访问客服或社媒 Agent 的上下文。
