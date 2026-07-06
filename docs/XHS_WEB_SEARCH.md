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

## Docker 浏览器运行时

默认 Compose 文件显式构建 Dockerfile 的 `browser-runtime` 阶段，该阶段会安装 Playwright Python 包、Chromium 二进制和浏览器所需的 Linux 系统库。Windows 宿主机 `.venv` 中安装的 Playwright 或 Chromium 不会进入容器。

Compose 固定使用镜像内置 Chromium，并在容器中启用 Headless 模式；宿主机 `.env` 中的 Chrome/Edge `browser_channel` 和 `executable_path` 不会传入容器。修改构建配置后需执行：

```powershell
docker compose build --no-cache web
docker compose up -d
```

浏览器登录态同样不会自动从 Windows 宿主机复制到容器，生产部署应通过受保护的持久卷或 Storage State 注入，不能把登录态写进镜像。

## 研究策略

`xhs.trend.research` 是只读 ReAct Skill，可根据观测选择搜索 Tool，但不得执行发布。`xhs.growth.campaign` 是固定 Workflow，用于完整的研究、提取、对比、策略、文案、评审、冻结和指标流程。

## 发布安全

1. Workflow 生成文章和发布包。
2. 内容评审通过后计算不可变 Hash。
3. Runtime 返回预览并在 Checkpoint 暂停。
4. 审批后从原 Checkpoint 恢复，不重新生成文案。
5. 发布 Tool 校验幂等键和 Hash。
6. 页面或网络只能证明“可能提交”时，返回 outcome unknown，要求先对账。

### 文字图片分页

当 `publishing_media_strategy=xhs_text_image` 时，发布包冻结完整的已审核 `card_text` 和卡片样式，并把两者写入不可变 Hash。Playwright 进入“写文字”后只填写第一个输入框，回读确认全文持久化，然后立即点击“生成图片”；封面和正文分页由小红书原生生成器完成。

框架不再逐页点击“再写一张”，也不再配置或承诺精确页数。中文文案生成约束会提供适合自动分页的完整正文，但最终图片数量仍由平台页面决定。生成按钮、样式或下一步不可用时会在发布前停止并保存诊断信息。

Linux 服务器可以运行 Headless Chromium，但扫码、短信、风控和网络稳定性会影响 RPA。生产建议优先使用官方 API；必须使用 RPA 时，将浏览器 Worker 与主 Runtime 隔离，并使用队列、资源上限、会话锁和可观测诊断。
