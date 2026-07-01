# Playwright 网站搜索与小红书发布接入

本文说明 AgentKit 的浏览器搜索架构、小红书搜索与发布启用方式、登录会话、二阶段审批、
错误处理，以及后续接入其他网站时的扩展边界。

## 1. 设计目标

小红书页面结构、登录策略和风控会持续变化，因此网页选择器不能进入 agent、skill 或
LangGraph 节点。当前实现分为三层：

1. `connectors/browser_search.py`：通用 Playwright 生命周期、超时、持久 profile、同一
   profile 的进程内串行化、资源释放和错误分类。
2. `connectors/xhs_playwright.py`：小红书搜索 URL、页面状态识别、滚动加载、DOM 提取、
   详情补全、指标解析和来源标准化。
3. `domain_packs/social_growth/providers.py`：把标准化结果映射到稳定的
   `XhsResearchProvider.search_top_notes()` 契约。
4. `connectors/xhs_publisher_playwright.py`：封装创作服务平台的封面生成、媒体上传、字段填充、
   发布确认，以及防重复发布 ledger；业务层只依赖 `XhsPublishingProvider`。

上层仍调用 `xhs.rpa.search_top_notes`，因此审批、RBAC、ToolExecutor 超时/审计和
`xhs.growth.campaign` 工作流都不需要感知 Playwright。

## 2. 安装

Playwright 是可选依赖。默认 `mock` 模式不需要浏览器。

```powershell
uv sync --extra dev --extra browser
uv run playwright install chromium
```

没有 `uv` 时：

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Linux 服务器通常需要浏览器系统依赖：

```bash
python -m playwright install --with-deps chromium
```

## 3. 首次登录

小红书未登录搜索可能只显示筛选框而不返回结果。先运行：

```powershell
agentkit --tenant company_alpha browser-login xhs --query "AI Agent"
```

命令会打开有界面的 Chromium，并使用配置的持久 profile。人工完成扫码登录；首次登录后如果
继续出现手机号验证或短信验证码，也在该浏览器窗口中人工完成。命令会持续等待，只有在目标
搜索页已经显示可用结果后才自动关闭浏览器并保存 profile，不需要回到终端按 Enter。
AgentKit 不读取、代填或绕过短信验证码/CAPTCHA；可按 `Ctrl+C` 主动取消等待。

创作服务平台可能要求独立的二次验证。发布前运行：

```powershell
agentkit --tenant company_alpha browser-login xhs --target publish
```

同样完成扫码和可能出现的手机号验证码，直到终端打印 `Authenticated target page detected`。

默认 profile 位于：

```text
data/browser-profiles/xiaohongshu/
```

该目录包含登录态，已加入 `data/.gitignore`。不要提交、上传或在不同租户之间共享。

## 4. 启用真实搜索

全局 `.env`：

```env
AGENTKIT_XHS_RESEARCH_PROVIDER=playwright
AGENTKIT_XHS_PUBLISHING_PROVIDER=mock
AGENTKIT_WEB_SEARCH_BROWSER=chromium
AGENTKIT_WEB_SEARCH_HEADLESS=true
AGENTKIT_WEB_SEARCH_PROFILE_ROOT=data/browser-profiles
AGENTKIT_WEB_SEARCH_STORAGE_STATE_ROOT=
AGENTKIT_WEB_SEARCH_TIMEOUT_SECONDS=30
AGENTKIT_WEB_SEARCH_MAX_SCROLLS=6
AGENTKIT_WEB_SEARCH_SCROLL_PAUSE_SECONDS=0.75
AGENTKIT_XHS_ENRICH_DETAILS=true
AGENTKIT_XHS_DETAIL_LIMIT=5
AGENTKIT_XHS_DETAIL_TIMEOUT_SECONDS=6
AGENTKIT_XHS_DETAIL_PAUSE_SECONDS=0.5
```

默认留空 `AGENTKIT_WEB_SEARCH_BROWSER_CHANNEL`，使用 Playwright 安装的 Chromium。若要使用
Google Chrome，需先执行：

```powershell
.\.venv\Scripts\python.exe -m playwright install chrome
```

然后设置 `AGENTKIT_WEB_SEARCH_BROWSER_CHANNEL=chrome`，并建议改用独立的
`AGENTKIT_WEB_SEARCH_PROFILE_ROOT=data/browser-profiles-chrome` 后重新登录。Windows 默认浏览器
不会影响 Playwright 的浏览器选择。

也可为单个 tenant 覆盖；tenant 配置优先于环境变量：

```json
{
  "social_growth": {
    "research_provider": "playwright",
    "browser_headless": true,
    "browser_profile_root": "data/browser-profiles",
    "browser_timeout_seconds": 30,
    "browser_max_scrolls": 6,
    "enrich_details": true,
    "detail_limit": 5,
    "detail_timeout_seconds": 6,
    "detail_pause_seconds": 0.5
  }
}
```

修改配置后重启 `agentkit web`，或通过已有的 admin reload 接口重建 runtime。

## 5. 启用真实发布

真实发布是外部不可逆副作用，默认始终为 `mock`。确认使用受控账号、完成平台授权并通过
内部合规评审后，显式启用：

```env
AGENTKIT_XHS_PUBLISHING_PROVIDER=playwright
AGENTKIT_XHS_PUBLISH_URL=https://creator.xiaohongshu.com/publish/publish?source=official
AGENTKIT_XHS_PUBLISH_ASSET_ROOT=data/xhs-publish-assets
AGENTKIT_XHS_PUBLISH_LEDGER_PATH=data/xhs-publish-ledger.sqlite
AGENTKIT_XHS_PUBLISH_MEDIA_STRATEGY=upload
AGENTKIT_XHS_TEXT_IMAGE_STYLE=涂鸦
AGENTKIT_XHS_TEXT_IMAGE_GENERATION_TIMEOUT_SECONDS=120
AGENTKIT_BROWSER_PUBLISH_OBSERVATION_SECONDS=90
```

tenant 的 `social_growth.publishing_mode` 必须为 `direct`。`company_alpha` 已采用该模式，但
provider 仍由环境变量控制，因此 CI/离线开发不会触达真实小红书。

发布媒体支持两种策略：

- `upload`：上传 `media_paths` 中已审核的本地图片。没有媒体时生成本地封面。
- `xhs_text_image`：进入创作中心“文字配图”，写入已审核的 `card_text`，由小红书生成卡片，
  选择 `card_style`（默认“涂鸦”），再进入最终编辑页。

可以在 tenant 的 `social_growth.publishing_media_strategy` 设置默认策略，并用
`social_growth.text_image_style`、`social_growth.text_image_generation_timeout_seconds` 调整风格和
生成等待时间。文章显式携带 `media_paths` 时自动选择 `upload`，因此外部图片生成 provider
可以直接复用媒体上传契约；文章显式传入 `media_strategy` 时以文章值为准。视频发布需增加独立
的 `video_upload` 策略和视频编辑页适配，不能伪装成当前的图片上传路径。

完整发布顺序：

1. 研究和生成文章。
2. 确定性规则检查标题/正文长度、证据、来源覆盖和违规承诺。
3. LLM 只做审查，不改写内容；确定性 error 不能被 LLM 覆盖。
4. 冻结标题、正文、标签和媒体策略。`upload` 对媒体文件字节计算 SHA-256；
   `xhs_text_image` 冻结卡片文字和风格。
5. LangGraph 在 `post_execution_approval` 暂停，Chat 展示最终正文、review findings，以及本地
   媒体预览或文字卡片策略、风格和卡片正文。
6. 人工点击 `Approve & Publish`，同一 checkpoint 注入批准的内容哈希。
7. `execute_deferred_action` 校验原 plan、skill tool allowlist 和内容哈希，然后调用
   `xhs.rpa.publish_note`。
8. 发布成功后调用指标 provider，并写入完整审计事件。

审批后不会重新执行搜索、文案生成或 review。若标题、正文、标签、媒体策略、卡片文字、卡片
风格或上传文件字节在审批后发生变化，哈希校验失败，不会发布。小红书原生卡片像素由平台在
发布阶段生成，审批准据是生成输入和风格，而不是平台输出图片的二进制哈希。

### 幂等与未知结果

发布工具是非幂等副作用，`ToolExecutor` 不自动重试，并在调用线程执行，避免超时后遗留仍在
点击的浏览器线程。`XhsPublishLedger` 在点击前写入 `submitting`，成功后写入 `published`；
相同 idempotency key 的已发布结果直接复用。若点击后未收到成功确认，ledger 标记 `unknown`，
后续自动重试会被拒绝，必须先到创作服务平台人工核对，避免重复发布。

当 `AGENTKIT_WEB_SEARCH_HEADLESS=false` 且发布结果未知时，
`AGENTKIT_BROWSER_PUBLISH_OBSERVATION_SECONDS`（默认 90 秒，范围 0 到 300）会让 AgentKit 保留
浏览器供人工查看平台提示；设为 `0` 则立即关闭。该窗口不会再次点击发布、不会绕过人机验证。
失败诊断只包含脱敏的同源请求方法、路径、状态码和资源类型，不包含查询参数、Cookie、请求体、
响应体或鉴权信息。

发布页面出现登录、手机号验证、短信验证码或 CAPTCHA 时立即停止。AgentKit 不读取或代填
验证码；重新运行 `agentkit --tenant company_alpha browser-login xhs --target publish`，在同一
profile 中人工完成验证。即使验证层下方仍存在上传控件，登录命令也会保持浏览器打开，直到
验证层消失且创作页面真实可用。

### Windows 登录、Linux Docker 搜索/发布

浏览器 profile 通常不适合跨操作系统复制。需要在 Windows 完成登录、再让 Linux Docker
执行搜索/发布时，改用 Playwright storage state：

```env
AGENTKIT_WEB_SEARCH_PROFILE_ROOT=
AGENTKIT_WEB_SEARCH_STORAGE_STATE_ROOT=data/browser-state
```

在 Windows 执行一次 `agentkit browser-login xhs`，会生成
`data/browser-state/xiaohongshu.json`。该文件同样是敏感认证材料且已 gitignore。随后使用浏览器
镜像 overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d --build
```

overlay 使用 Dockerfile 的 `browser-runtime` target、安装 Chromium，并把宿主机
`data/browser-state` 挂载到容器。`.env` 中同时设置
`AGENTKIT_XHS_RESEARCH_PROVIDER=playwright`、
`AGENTKIT_XHS_PUBLISHING_PROVIDER=playwright`、清空 `AGENTKIT_WEB_SEARCH_PROFILE_ROOT`，并设置
`AGENTKIT_WEB_SEARCH_STORAGE_STATE_ROOT=data/browser-state`。

## 6. 搜索与结果处理

一次搜索执行以下步骤：

1. 通过标准搜索 URL 打开结果页。
2. 等待可识别的笔记链接；若没有结果，区分登录、验证码、超时和页面结构漂移。
3. 有界滚动，最多执行 `WEB_SEARCH_MAX_SCROLLS` 次，不做无限抓取。
4. 按笔记 ID 去重，解析标题、作者、内容类型、点赞数和来源 URL。
5. 按互动分数稳定排序：`likes + saves * 2 + comments * 3`。
6. 可选地逐条打开前 `XHS_DETAIL_LIMIT` 条详情，补全文本、收藏、评论、标签和发布时间。
7. 输出中保留 `url/source/source_rank/captured_at`，供审计和人工回查。

这里的“top”是对当前搜索页可见样本按互动数据进行的排序，不是小红书官方全量榜单，也不
保证严格覆盖自然日内的全部内容。若业务 KPI 要求可证明的“当日榜单”，应接入授权数据 API
或合规数据供应商，并继续复用同一个 `XhsResearchProvider` 契约。

workflow 会同时输出 `research_quality`，记录请求/实际样本数、详情补全数、发布时间覆盖、
指标覆盖和限制说明。证据不足时允许生成内部草稿，但 review 状态为
`approved_with_warnings`；direct 模式仍必须由人工查看这些 warnings 后批准，发布 readiness 为
`ready_for_human_approval`。Chat 最终回复展示
Top 案例、数据驱动对比、限制、生成草稿和发布状态，不再只显示中间的文案生成流。

详情页失败时保留可用的搜索结果并记录 `detail_error`；登录失效或出现验证码时立即失败，
不会悄悄切回 mock 数据。外部网页文本进入 LLM 前被标记为不可信证据，prompt 明确禁止执行
网页内容中的指令，以降低间接 prompt injection 风险。

## 7. 常见错误

| 错误 | 含义 | 处理方式 |
| --- | --- | --- |
| `BrowserDependencyError` | Playwright 包或浏览器二进制不存在 | 安装 `agentkit[browser]` 并执行 `playwright install chromium` |
| `BrowserAuthenticationRequired` | 登录态不存在或已过期 | 重新执行 `agentkit browser-login xhs` |
| `BrowserChallengeRequired` | 出现验证码/人工验证 | 人工完成验证；不要自动绕过 |
| `BrowserPageChanged` | 结果未加载或 DOM 协议变化 | 保存页面证据并更新 XHS adapter；不要修改 agent/skill |
| `XhsPublishOutcomeUnknown` | 已点击发布但未取得明确成功结果 | 到创作服务平台人工核对；不要直接重试 |

生产环境应控制查询频率、遵守目标网站服务条款和数据合规要求。当前实现串行使用同一
profile，并在详情请求间加入间隔；跨进程部署时应确保同一 profile 只由一个 worker 使用，
或把浏览器搜索拆成独立 connector 服务。

## 8. 扩展其他网站

新增网站时实现 `SiteSearchAdapter`：

```python
class ExampleSearchAdapter:
    site_key = "example"

    def search_url(self, query: str) -> str:
        ...

    def search(
        self,
        page,
        *,
        query: str,
        limit: int,
        timeout_ms: int,
        max_scrolls: int,
        scroll_pause_ms: int,
    ) -> list[WebSearchResult]:
        ...
```

然后将 adapter 注入 `PlaywrightSearchClient`，再在对应 domain pack 的 provider 中把
`WebSearchResult` 映射到业务对象。通用浏览器层无需增加站点判断；站点选择器、登录检测和
字段语义只能存在于各自 adapter 中。

建议为每个新 adapter 准备固定 HTML/假 page 单元测试、登录页测试、验证码测试、正常结果
测试和 DOM 漂移告警。真实网站 smoke test 应独立于普通 CI，使用受控账号和低频定时任务。
