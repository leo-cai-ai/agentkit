# 小红书研究与发布 Tool

XHS 浏览器是 `xhs_growth` 的特定 Tool 实现，不是 AgentKit 核心运行时的必要依赖。

## 位置

- `skills/xhs-growth-campaign/skill.yaml`：Tool/Capability 契约。
- `skills/xhs-growth-campaign/scripts/tools.py`：Tool 适配和租户 Provider 工厂。
- `skills/xhs-growth-campaign/scripts/providers.py`：Mock/Playwright Provider。
- `src/agentkit/connectors/xhs_playwright.py`：研究页适配。
- `src/agentkit/connectors/xhs_publisher_playwright.py`：创作中心发布适配。

## 交互式登录

```powershell
agentkit --tenant company_alpha browser-login xhs --target search
agentkit --tenant company_alpha browser-login xhs --target publish
```

CLI 通过声明目录中 XHS Tool 的 `factory_entrypoint` 获取交互式入口，不导入额外业务包。浏览器保持打开，直到页面通过登录完成检查或用户按 Ctrl+C。

## 研究策略

`xhs.trend.research` 是只读 ReAct Skill，可根据观测选择搜索 Tool，但不得执行发布。`xhs.growth.campaign` 是固定 Workflow，用于完整的研究、提取、对比、策略、文案、评审、冻结和指标流程。

## 发布安全

1. Workflow 生成文章和发布包。
2. 内容评审通过后计算不可变 Hash。
3. Runtime 返回预览并在 Checkpoint 暂停。
4. 审批后从原 Checkpoint 恢复，不重新生成文案。
5. 发布 Tool 校验幂等键和 Hash。
6. 页面或网络只能证明“可能提交”时，返回 outcome unknown，要求先对账。

Linux 服务器可以运行 Headless Chromium，但扫码、短信、风控和网络稳定性会影响 RPA。生产建议优先使用官方 API；必须使用 RPA 时，将浏览器 Worker 与主 Runtime 隔离，并使用队列、资源上限、会话锁和可观测诊断。
