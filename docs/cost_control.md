# Agent 成本与 Token 控制

企业 Agent 的成本不只是模型单价，还包括模型调用次数、上下文大小、Tool 调用、长任务占用时间、重试和人工审批等待。

## 优先级

1. 能用 Direct/Workflow 的任务不使用 ReAct/Plan。
2. 分类、路由和结构化提取使用小模型。
3. RAG 控制 Top-K、Chunk 和总 Token，默认不使用 LLM Query Rewrite/Rerank。
4. 长输出写 Artifact，图状态只传引用和摘要。
5. 只有幂等且确认为短暂错误的操作才能重试。

## 三层预算

```text
global deployment ceiling
  └─ Agent budget
      └─ Skill limits
```

三层按字段取最小值，包括 Model Calls、Tool Calls、Iterations、Plan Steps、Replans、Tokens 和 Timeout。部署变量以 `AGENTKIT_AUTONOMY_` 开头，启动时会拒绝超过全局上限的 Manifest。

## 上下文

- 会话只取 Agent 声明的窗口。
- Memory 只取相关 Top-K。
- RAG 同时限制 Collection、Top-K 和上下文 Token。
- Tool 列表只向 ReAct 暴露当前 Skill 允许的子集。
- Plan 只向 Planner 暴露当前 Agent 的候选 Skill。

## 度量

建议按 tenant/agent/skill/strategy/model 聚合：

- Token 和 Model Calls。
- Tool Calls、超时和重试。
- 平均与 P95 延迟。
- 审批率、拒绝率和恢复耗时。
- 预算耗尽、无进展和计划失效率。

成本优化必须和质量回归一起进行，不能只通过减少 Token 判断成功。
