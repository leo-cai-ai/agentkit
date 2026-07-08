# XHS Native Text Image Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把完整已审核正文一次填入小红书“写文字”输入框，再由平台原生生成并自动分页。

**Architecture:** 发布契约使用单一 `card_text` 与 `card_style`。Playwright 不规划页面、不创建额外编辑器，只验证全文填充并点击平台“生成图片”。

**Tech Stack:** Python 3.12、Playwright、JavaScript 审批预览、pytest。

---

### Task 1: 恢复单一文字图片发布契约

**Files:**
- Modify: `src/agentkit/connectors/xhs_publication.py`
- Delete: `src/agentkit/connectors/xhs_text_image_cards.py`
- Modify: `tests/unit/test_xhs_publication.py`
- Delete: `tests/unit/test_xhs_text_image_cards.py`

- [ ] 先把契约测试改为断言 `card_text` 默认等于正文并参与哈希。
- [ ] 运行测试，确认当前 `card_pages` 实现失败。
- [ ] 删除分页规划器，恢复 `card_text` 规范化、策略解析和哈希。
- [ ] 运行内容契约测试。

### Task 2: 改为平台原生自动分页

**Files:**
- Modify: `src/agentkit/connectors/xhs_publisher_playwright.py`
- Modify: `tests/unit/test_xhs_publication.py`

- [ ] 修改 Fake Page，使其只有一个编辑器，并断言全文填入后生成按钮被点击。
- [ ] 运行目标测试，确认当前代码因等待多个编辑器而失败。
- [ ] 删除添加页面、编辑器计数和逐页校验逻辑；保留单编辑器回读校验。
- [ ] 运行 Playwright Provider 单元测试。

### Task 3: 清理分页配置和 Provider 透传

**Files:**
- Modify: `src/agentkit/config.py`
- Modify: `skills/xhs-growth-campaign/scripts/providers.py`
- Modify: `.env.example`
- Modify: `tenants/company_alpha.json`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_social_growth_workflow.py`

- [ ] 删除页数和目标字符数配置的测试预期并确认失败。
- [ ] 删除三个配置字段、交叉校验和 Provider 参数透传。
- [ ] 运行配置与 Provider 测试。

### Task 4: 统一审批、追踪和前端预览

**Files:**
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tests/integration/test_xhs_publish_approval.py`
- Modify: `tests/integration/test_web_ui_redesign.py`

- [ ] 把测试预期从 `card_pages` 改为 `card_text` 并确认失败。
- [ ] 延迟动作、审批预览和执行包统一传递相同 `card_text`。
- [ ] 前端展示完整文字来源并标注“由小红书自动分页”。
- [ ] 运行工作流与 UI 测试。

### Task 5: 调整内容长度并完成回归

**Files:**
- Modify: `contexts/business/xhs-growth-campaign/article-generate/system.md`
- Modify: `tests/golden/contexts/skill.xhs-growth-campaign.article-generate.json`

- [ ] 调整中文正文长度约束，使完整内容更适合平台生成多页，同时不超过发布上限。
- [ ] 更新 Context Golden 并运行 Context 测试。
- [ ] 运行 XHS 相关测试与完整测试集。

