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
