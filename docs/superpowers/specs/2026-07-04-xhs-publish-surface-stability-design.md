# 小红书发布页面稳定性设计

## 背景与真实证据

2026-07-04 使用 `company_alpha` 租户完成了一次真实审批与发布复现。发布器准确识别了关闭 Shadow DOM 内的“发布”按钮，宿主区域为 `x=280.4, y=630, width=680, height=90`，按钮中心为 `x=692.4, y=675`。

点击后平台没有产生创建笔记或提交发布的网络请求，页面仍停留在 `/publish/publish`。同时网络记录显示正文标签触发了话题推荐接口。失败后的截图中发布按钮正常可见，说明最可能的时序是：点击发生时话题联想层仍覆盖按钮，联想层接收点击后关闭，最终页面恢复正常，但发布动作没有发生。

该次幂等记录已标记为 `unknown`，不得自动重试，必须先在创作中心人工核对。

## 目标

1. headed Chromium 使用真实最大化窗口，降低响应式布局和固定 viewport 带来的差异。
2. 自动拒绝非必要的浏览器权限提示，减少浏览器层遮挡。
3. 正文填写后主动关闭话题联想等临时浮层。
4. 点击前验证按钮中心点的最上层 DOM 节点属于目标发布按钮。
5. 无法确认按钮未被遮挡时安全停止，保留诊断信息，禁止盲点和自动重试。

## 设计

### 浏览器启动

共享 `PlaywrightSearchClient` 在 headed Chromium 下增加：

- Chromium 启动参数 `--start-maximized`；
- Chromium 启动参数 `--deny-permission-prompts`；
- persistent context 与普通 context 均设置 `no_viewport=True`。

headless、Firefox 与 WebKit 保持现有行为，避免无关兼容性变化。

### 发布表面稳定化

XHS 发布器在标题和正文回读通过后、定位发布按钮前执行一次稳定化：

1. 向当前页面发送 `Escape`；
2. 对当前活动输入元素执行 `blur()`；
3. 短暂等待页面完成浮层退出动画；
4. 重新读取发布按钮及其关闭 Shadow DOM 内部按钮的坐标。

### 遮挡检查

使用现有 CDP session 调用 `DOM.getNodeForLocation` 获取按钮中心点的命中节点。通过 `DOM.pushNodesByBackendIdsToFrontend` 和 `DOM.describeNode` 沿父节点向上检查，只有命中节点等于目标按钮或位于目标按钮内部时才允许发送鼠标事件。

如果中心点被其他元素覆盖，则抛出 `BrowserPageChanged`，错误明确说明发布按钮被浮层遮挡，并附带现有脱敏诊断截图和页面状态。

### 失败与幂等

- 点击前发现遮挡：属于已知未提交，不产生平台副作用，可以由上层按现有工具失败语义处理。
- 点击已发送但平台未确认：继续标记 `unknown`，禁止自动重试。
- 不改变现有审批 token、内容哈希和发布账本契约。

## 测试

1. headed Chromium persistent context 使用最大化参数和 `no_viewport=True`。
2. headed Chromium 普通 context 使用最大化参数，并在 `new_context` 设置 `no_viewport=True`。
3. headless 模式不注入 headed 参数。
4. XHS 填写正文后执行 Escape、blur 和稳定等待。
5. CDP 中心点命中目标按钮时允许点击。
6. CDP 中心点命中覆盖层时拒绝点击，且不产生鼠标按下/释放事件。
7. 原有发布成功、未知结果、幂等和审批测试全部保持通过。

## 验收标准

- 定向及全量自动化测试通过；Ruff 与 Mypy 通过。
- 新内容哈希的一次真实发布测试产生平台发布请求并获得明确成功信号，或在点击前以“按钮被遮挡”安全停止。
- 不对本次已标记 `unknown` 的内容执行重试。
