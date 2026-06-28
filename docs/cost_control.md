关于运行成本，你的判断是对的：如果所有企业、所有任务都完整跑 intent → route → plan → plan review → approval analysis → execution preflight → output review → final generation，LLM token 和延迟都会偏重。工业级不等于每次都全流程，应该是“按风险和场景启用治理深度”。
我建议把运行模式分层：
lite：小企业/低风险流程。少量 LLM 调用，甚至 deterministic route + direct tool + final answer。
standard：默认企业模式。保留 intent/route/plan/execution/final，只有高风险时才触发 review/approval。
strict：金融、医疗、法务、人事等高风险。完整治理链路、fail-closed、审批、输出复核。
offline/eval：批量测试、回归、成本评估，不走真实业务副作用。
成本控制上，核心不是只换便宜模型，而是减少不必要调用：
用规则/配置先做 fast-path：明确 agent + 明确 skill 的请求不必每次 route。
简单任务跳过 plan review/output review，只对高风险 skill、敏感数据、外部发布、写操作启用。
模型路由：分类/路由用小模型，最终表达或复杂推理才用大模型。
Prompt 分层：公共治理 prompt 短化，业务上下文按需注入，不要每个节点都塞全量 tenant/工具说明。
记忆/RAG 控制 top-k 和摘要长度，避免把历史和知识库内容无脑拼进上下文。
每个 run 设 token/cost budget，超过预算降级或要求确认。
缓存稳定结果：tool catalog、routing hints、RAG 检索、低风险 plan 可以缓存。
观测成本：按 tenant / agent / skill / node 统计 token、耗时、失败率，才能知道哪个节点最贵。
最终建议：AgentKit 默认做成 standard，但 tenant 可以配置 runtime_profile=lite|standard|strict。这样小企业能低成本落地，大企业或高风险场景再打开完整治理链路。