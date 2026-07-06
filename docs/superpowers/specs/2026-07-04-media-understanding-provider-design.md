# 可插拔媒体理解 Provider 与小红书证据链设计

## 1. 背景

小红书搜索结果能够提供卡片标题、作者、互动数和封面地址，但大量正文信息位于图片中。当前连接器只提取 DOM 文本：虽然搜索阶段已经拿到 `cover_url`，该字段会在 Provider 转换和 Skill 压缩阶段被丢弃；详情页提取也没有输出轮播图片。结果是内容生成只能使用卡片文字，容易把标题暗示扩写成没有证据支持的具体推荐。

当前运行还存在一个状态表达问题：第一条详情触发登录或风控挑战后，批次会立即停止，但剩余案例被统一写成相同的 `detail_error`。这会让审计结果误以为每条详情都被独立尝试过。

本期不实现 OCR 或多模态模型，只建立通用、可替换、可追溯的 Provider 契约，并注册安全的 `none` 实现。未配置媒体理解时继续现有文本链路，不新增硬门禁；未来注册真实 Provider 后，媒体证据自动进入生成与 Review 上下文，用于验证具体工具、场景和推荐。

## 2. 目标

- 媒体理解能力属于通用框架接口，不写死在小红书 Agent 或 Prompt 中。
- 本期只可用 `none`；明确配置 `none` 时记录 `skipped`，不调用 OCR、视觉模型或外部服务。
- Provider 使用稳定注册 ID。未来可注册 `paddleocr`、`openai_compatible`、`hybrid`，而无需修改 XHS Workflow。
- 未注册的 Provider 必须在运行时构建阶段明确失败，不能静默降级为 `none`。
- 保留搜索封面和详情媒体地址，使后续 Provider 能直接消费。
- 媒体识别结果必须包含来源、Provider、模型、置信度和证据文本，进入 Artifact 与 Review。
- 未配置媒体理解时继续现有文本生成和 Review 行为；Review 仍可因无证据事实而阻止发布。
- 配置真实 Provider 后，Review 必须验证具体推荐是否有 DOM 或媒体证据支持。
- 修正详情批次状态，区分“实际尝试失败”和“因会话挑战而未尝试”。

## 3. 非目标

- 本期不实现 PaddleOCR、云 OCR、OpenAI 兼容视觉模型或混合路由。
- 不下载或持久化小红书图片二进制，不绕过登录、验证码、短信验证或平台风控。
- 不降低现有内容 Review 标准，不把证据错误自动降级成 warning。
- 不允许 LLM 根据图片地址猜测图片内容。
- 不修改其他 Agent 的 Workflow 和 Review 策略。

## 4. 方案比较

### 4.1 通用 Provider 注册表与结构化证据（采用）

在框架层定义 `MediaUnderstandingProvider`、输入资产、输出证据和 Provider 注册表。XHS 连接器只采集媒体资产，XHS Skill 只消费结构化证据。

优点：边界清楚、可测试、可复用、可追溯；后续 OCR、视觉模型或 MCP 实现都可以接入同一契约。缺点：本期需要增加少量通用类型和工厂代码。

### 4.2 XHS 专用 OCR Hook

在 `XhsSearchAdapter` 中增加可选 OCR 回调。

优点：改动少。缺点：能力与平台连接器耦合，招聘附件、客服截图等场景无法复用；Provider 使用量和证据来源也难以统一治理。

### 4.3 直接把图片交给生成 LLM

将图片 URL 或二进制直接加入文案生成请求。

优点：实现看似直接。缺点：Token 与延迟不可控、缺少独立证据 Artifact、难以缓存和审计，也无法区分 OCR 事实与模型推断，不适合企业级稳定性目标。

## 5. 通用契约

新增与业务平台无关的媒体理解契约，建议位于 `agentkit.core.media`：

```python
MediaAsset(
    asset_id: str,
    source_url: str,
    media_type: Literal["image"],
    source_kind: Literal["cover", "detail"],
    index: int,
    metadata: dict[str, Any],
)

MediaEvidence(
    asset_id: str,
    text: str,
    provider: str,
    model: str,
    confidence: float | None,
    metadata: dict[str, Any],
)

MediaUnderstandingResult(
    status: Literal["completed", "skipped", "failed"],
    provider: str,
    evidence: tuple[MediaEvidence, ...],
    reason: str,
    usage: dict[str, Any],
)
```

Provider 接口只负责把一组 `MediaAsset` 转换成 `MediaUnderstandingResult`。它不得修改浏览器状态、发布内容或 Review 决策。

`none` Provider 固定返回：

```json
{
  "status": "skipped",
  "provider": "none",
  "evidence": [],
  "reason": "not_configured",
  "usage": {}
}
```

Provider 注册表使用显式注册 ID。当前仅注册 `none`；未知 ID 抛出配置错误，并包含可用 Provider 列表。

## 6. 配置

框架级默认配置：

```env
AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=none
AGENTKIT_MEDIA_UNDERSTANDING_MODEL=
AGENTKIT_MEDIA_UNDERSTANDING_MAX_IMAGES=3
AGENTKIT_MEDIA_UNDERSTANDING_MIN_CONFIDENCE=0.75
```

租户可在 `social_growth` 下覆盖：

```json
{
  "media_understanding_provider": "none",
  "media_understanding_model": "",
  "media_understanding_max_images": 3,
  "media_understanding_min_confidence": 0.75
}
```

优先级为租户配置高于环境默认。`max_images` 必须大于等于 0，`min_confidence` 必须位于 0 到 1 之间。

Provider 名称使用普通字符串并交给注册表验证，不在 `Settings` 中使用封闭的 `Literal`。这样安装新 Provider 后不需要修改核心配置模型，同时未知名称仍会失败关闭。

## 7. 小红书数据流

### 7.1 媒体资产采集

搜索卡片保留封面地址，详情页在可访问时提取正文区域中的图片地址。连接器负责：

- 只接受 `https` 且属于允许域名的媒体地址；
- 去重并保持页面顺序；
- 标记 `source_kind=cover|detail`；
- 最多保留配置允许的图片数量；
- 不读取图片内容，不调用 LLM。

`PlaywrightXhsResearchProvider._to_note()` 和 `compact_cases()` 必须保留 `media_assets`，不能再次丢弃。

### 7.2 媒体理解调用

研究 Provider 在文本和媒体资产收集完成后调用已配置的通用 Provider：

- `none`：返回 `skipped`，继续文本链路；
- 已注册真实 Provider：返回 `completed` 或 `failed`；
- Provider 执行失败：记录失败原因，保留已有文本证据，不伪造媒体结果；
- 未注册 Provider：构建阶段失败，不启动浏览器。

每个案例新增：

- `media_assets`
- `media_understanding.status`
- `media_understanding.provider`
- `media_understanding.evidence`
- `media_understanding.reason`

### 7.3 生成与 Review

当 Provider 为 `none` 时，生成和 Review 继续使用现有文本证据；`skipped` 本身不是 Review 失败条件。

当 Provider 返回 `completed` 时：

- 媒体证据与 DOM 证据一并进入文案生成上下文；
- 生成器必须区分原始证据和原创建议；
- Review 上下文接收结构化证据摘要；
- 文章中的具体工具、场景、效果和推荐必须能关联到 DOM 或媒体证据；
- 无法关联的事实性推荐继续返回 `failed`，不得因为启用了 OCR 就自动放行。

当 Provider 返回 `failed` 时，不把失败结果当作空白证据伪装成 `skipped`。Review 能看到失败状态，并按实际文本证据范围判断。

## 8. 详情批次状态

第一条详情触发 `BrowserChallengeRequired` 时：

- 当前案例记录 `detail_attempted=true`、`detail_error=BrowserChallengeRequired`；
- 后续未打开的案例记录 `detail_attempted=false`、`detail_skipped_reason=session_challenge`；
- 批次立即停止，不继续触发平台风控；
- 研究质量分别统计尝试数、成功数、失败数和跳过数。

这避免把“一个页面触发会话挑战”错误表述为“所有页面均独立抓取失败”。

## 9. 可观测性与成本

Artifact 和运行追踪至少记录：

- Provider 名称、模型和状态；
- 媒体资产数、实际处理数、证据条数；
- 置信度分布；
- Provider 延迟与 usage；
- 失败原因，但不记录 Cookie、验证码或登录凭证。

未来真实 Provider 应按媒体内容哈希缓存结果，并遵守 `max_images`。`hybrid` Provider 可以先执行本地 OCR，仅对低置信度结果调用视觉模型，但该策略不在本期实现。

## 10. 错误处理

- `provider=none`：正常 `skipped`。
- Provider 未注册：构建失败，并列出可用 Provider。
- 媒体地址非法：丢弃该资产并记录计数，不调用 Provider。
- Provider 运行失败：返回 `failed`，保留文本证据，禁止伪造识别结果。
- 登录或风控挑战：按现有人工验证边界处理，不由媒体 Provider 重试。
- Review 无法验证推荐：保持 `blocked`，不进入发布。

## 11. 测试设计

### 11.1 通用契约

- `none` 返回稳定的 `skipped/not_configured` 结果且 usage 为空。
- 注册表能够构建 `none`。
- 未知 Provider 明确失败并列出可用名称。
- 配置边界验证覆盖 `max_images` 和 `min_confidence`。

### 11.2 XHS 连接器和 Provider

- 搜索卡片封面被转换为 `MediaAsset`。
- 详情图片按顺序提取、去重、过滤域名并限制数量。
- `_to_note()` 和 `compact_cases()` 保留媒体资产与理解状态。
- `none` 不调用任何 OCR/LLM，文本搜索行为不变。
- 模拟已配置 Provider 时，其证据进入案例输出。
- 会话挑战只标记当前案例为失败，其余案例标记为未尝试跳过。

### 11.3 Skill 与 Review

- `none/skipped` 不会因为缺少媒体 Provider 自动阻断现有文本链路。
- 模拟媒体证据能够进入生成和 Review 上下文。
- 有媒体证据支持的推荐可以通过现有安全规则。
- 没有 DOM 或媒体证据支持的具体推荐仍被阻断。
- Provider `failed` 状态可见且不会被当作 `skipped`。

### 11.4 回归

- XHS 搜索、详情回填、文案 Review、人工审批与发布测试通过。
- 完整测试、Ruff、Mypy 和声明式目录校验通过。
- 自动化测试不执行真实发布。

## 12. 验收标准

- 默认配置只构建 `none`，不增加模型调用、浏览器请求或 Token 花费。
- Top Cases 和 Artifact 中能够看到媒体资产及 `skipped` 状态。
- 未知 Provider 不会静默运行。
- 后续新增 Provider 只需实现通用契约并注册，无需修改 XHS Workflow 控制流。
- 配置真实 Provider 后，其证据进入生成与 Review，具体推荐必须有证据支持。
- 详情挑战的审计信息能区分实际失败与未尝试跳过。
- 现有人工审批、幂等发布、治理指标和运行追踪保持有效。
