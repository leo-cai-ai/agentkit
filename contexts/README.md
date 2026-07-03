# Context Packs

本目录管理每个生产 LLM 节点的上下文契约。Context Pack 同时声明 System/User 模板、输入
Source 白名单、Agent/Skill 指令注入、Token 上限、输出 Schema 和审计策略。

- `agent.md` 正文仍是 Agent 长期指令的唯一来源。
- `SKILL.md` 正文仍是 Skill 业务指令的唯一来源。
- 本目录不保存会话、Memory、RAG 原文、Tool Observation、凭证或其他运行时数据。
- 动态数据只能通过 `context.yaml` 声明的 Source 进入 User Message。
- `overrides/<tenant-selector>/` 只能覆盖 `system.md` 或 `user.md`，不能修改安全 Fragment、
  输入白名单、Token 预算和输出 Schema。

使用 `agentkit --tenant <selector> validate-contexts` 在部署前验证全部 Context Pack。

## 目录与职责

- `runtime/`：意图、能力路由、ReAct、Plan、Memory 与 RAG 等框架节点。
- `skills/`：候选人摘要、小红书文章生成与内容审核等业务节点。
- `fragments/`：Runtime 强制注入的安全、不可信数据、证据与 JSON 输出规则。
- `overrides/<tenant-selector>/`：租户模板覆盖；只允许 `system.md` 和 `user.md`。

当前共 11 个 Pack。每个 Pack 必须声明 `context.yaml`、System/User 模板、输入 Source 白名单、Token 预算和输出模式；
JSON 输出还必须提供严格 Schema。动态输入不会进入 System Message，未声明输入不会被渲染。

## 修改流程

1. 创建或修改 Pack，并在生产节点通过 `ContextInvocationService` 调用。
2. 运行 `agentkit --tenant company_alpha validate-contexts`，确认 Registry、Override、Hash 和预算有效。
3. 更新 `tests/golden/contexts/` 中对应的完整脱敏 Render，并评审 System/User 分层变化。
4. 运行单元、隔离、并发与 Eval 测试。
5. 发布后使用 Runtime Manifest Hash 追溯版本；不要在运行中热改已被审批任务引用的 Pack。
