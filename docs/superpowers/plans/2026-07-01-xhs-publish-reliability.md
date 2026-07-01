# XHS Publish Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 让小红书图文发布在点击前验证表单、点击后保留脱敏证据，并消除无意义的浏览器启动。

**Architecture:** 保持现有 Python Playwright、独立持久化登录档案和 SQLite 幂等账本。XhsPublishAdapter 负责页面契约、字段回读、点击几何和提交证据；Skill 内 provider 只负责按媒体策略决定是否需要浏览器，以及把租户配置传入连接器。

**Tech Stack:** Python 3.12、Playwright Sync API、Pydantic Settings、Pytest、Ruff、Mypy。

---

## 文件结构

- 修改：src/agentkit/connectors/xhs_publisher_playwright.py — 发布包构造、表单回读、发布点击、脱敏网络证据、未知结果观察窗口。
- 修改：skills/xhs-growth-campaign/scripts/providers.py — 按媒体策略跳过无须浏览器的发布包创建，并传递观察窗口配置。
- 修改：src/agentkit/config.py — 声明全局观察窗口设置。
- 修改：.env.example、docs/XHS_WEB_SEARCH.md — 记录配置含义及人工观察行为。
- 修改：tests/unit/test_xhs_publication.py — 覆盖连接器、幂等与无浏览器发布包路径。
- 修改：tests/unit/test_config.py — 覆盖新设置的默认值与环境变量覆盖。
- Create: tests/unit/test_xhs_skill_providers.py — 直接加载声明式 Skill 脚本，覆盖发布 provider 的租户配置传递。

### Task 1: 发布包构造按需启动浏览器

**Files:**

- Modify: src/agentkit/connectors/xhs_publisher_playwright.py:304-335, 805-814
- Modify: skills/xhs-growth-campaign/scripts/providers.py:197-266
- Test: tests/unit/test_xhs_publication.py:88-220

- [ ] **Step 1: 写出无需浏览器的失败测试**

在 tests/unit/test_xhs_publication.py 增加会在 perform() 被调用时失败的假客户端，断言 xhs_text_image 和已有媒体的 upload 发布包都不触发它：

    class _NoBrowserClient:
        def perform(self, **_kwargs):
            raise AssertionError("发布包构造不应启动浏览器")

    def test_provider_prepares_text_images_without_opening_browser(tmp_path) -> None:
        adapter = XhsPublishAdapter(
            asset_root=tmp_path / "assets",
            media_strategy="xhs_text_image",
        )
        provider = PlaywrightXhsPublishingProvider(
            _NoBrowserClient(), adapter, XhsPublishLedger(tmp_path / "ledger.sqlite")
        )

        package = provider.create_publish_package(
            article={"title": "标题", "body": "正文"}, mode="direct"
        )

        assert package["media_strategy"] == "xhs_text_image"

增加第二个测试，向 article 提供一个已存在的 PNG 路径，并断言 upload 策略同样不调用浏览器。

- [ ] **Step 2: 运行测试并确认失败原因正确**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py::test_provider_prepares_text_images_without_opening_browser -q

Expected: FAIL，失败来自当前 PlaywrightXhsPublishingProvider.create_publish_package() 无条件调用 client.perform()。

- [ ] **Step 3: 以最小实现区分“构造包”与“渲染封面”**

将 XhsPublishAdapter.prepare_package() 的 page 参数改为 Any | None；仅当 media_strategy == "upload" 且没有媒体文件时要求 page 非空并调用 _render_cover()。添加如下公开判断方法：

    def needs_browser_to_prepare(self, *, article: dict[str, Any]) -> bool:
        content = resolve_publish_content(
            article,
            default_media_strategy=self.media_strategy,
            default_card_style=self.text_image_style,
        )
        return content["media_strategy"] == "upload" and not content["media_paths"]

将 provider 改为：

    def create_publish_package(self, *, article: dict[str, Any], mode: str) -> dict[str, Any]:
        if not self.adapter.needs_browser_to_prepare(article=article):
            return self.adapter.prepare_package(None, article=article, mode=mode, timeout_ms=0)
        return self.client.perform(
            site_key=self.adapter.site_key,
            operation=lambda page, timeout_ms: self.adapter.prepare_package(
                page, article=article, mode=mode, timeout_ms=timeout_ms
            ),
        )

在 prepare_package() 的封面分支中明确抛出 RuntimeError("渲染小红书封面需要浏览器页面")，防止调用方误传 None。

- [ ] **Step 4: 运行连接器单元测试**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py -q

Expected: PASS；图文生成和已有上传媒体不创建浏览器，缺少媒体的上传策略仍通过假页面渲染封面。

- [ ] **Step 5: 提交该独立改动**

    git add src/agentkit/connectors/xhs_publisher_playwright.py skills/xhs-growth-campaign/scripts/providers.py tests/unit/test_xhs_publication.py
    git commit -m "fix: avoid unnecessary browser during xhs package creation"

### Task 2: 点击前回读标题和正文，并记录发布点击几何

**Files:**

- Modify: src/agentkit/connectors/xhs_publisher_playwright.py:337-420, 590-635
- Test: tests/unit/test_xhs_publication.py:111-320

- [ ] **Step 1: 写出字段回读和右侧点击点的失败测试**

扩展 _Locator，让其具备 input_value() 与 inner_text()，并新增会吞掉 fill() 结果的 locator。测试必须证明字段不一致时不会点击发布：

    def test_publish_stops_before_click_when_title_does_not_persist(tmp_path) -> None:
        page = _PublishPage()
        page.title = _DiscardingLocator()
        adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
        media = tmp_path / "cover.png"
        media.write_bytes(b"png")

        with pytest.raises(BrowserPageChanged, match="title.*value mismatch"):
            adapter.publish(
                page,
                package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
                timeout_ms=1000,
            )

        assert page.button.clicked is False

再增加一个测试，令发布宿主边界为 {"x": 100, "y": 200, "width": 252, "height": 40}，断言点击点位于右侧红色按钮的水平中部（x == 192、y == 20），并且诊断元数据包含宿主边界与点击点。

- [ ] **Step 2: 运行测试并确认失败原因正确**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py::test_publish_stops_before_click_when_title_does_not_persist -q

Expected: FAIL，因为当前实现只调用 fill()，不会回读，也会继续点击发布。

- [ ] **Step 3: 实现回读校验和命名的点击位置计算**

在适配器中增加三个私有方法：

    @staticmethod
    def _normalized_field_value(value: str) -> str:
        return " ".join(value.split())

    def _read_locator_value(self, locator: Any) -> str:
        for name in ("input_value", "inner_text", "text_content"):
            reader = getattr(locator, name, None)
            if callable(reader):
                try:
                    value = reader()
                except Exception:
                    continue
                if value is not None:
                    return str(value)
        return ""

    def _fill_and_verify(self, *, page: Any, locator: Any, expected: str, field_name: str) -> None:
        locator.fill(expected)
        actual = self._read_locator_value(locator)
        if self._normalized_field_value(actual) != self._normalized_field_value(expected):
            diagnostic = self._capture_diagnostics(page, field_name=f"{field_name}-value")
            raise BrowserPageChanged(
                f"Xiaohongshu {field_name} value mismatch; "
                f"expected_length={len(expected)} actual_length={len(actual)}; {diagnostic}"
            )

调用顺序必须是标题回读成功后再填写正文，正文使用 append_hashtags() 的最终文本参与校验。不得在异常消息或日志中输出标题/正文原文。

将 _click_publish_control() 改为返回几何元数据。对已确认的 252 像素宿主，使用右侧 120 像素按钮中心；对于其他宽度，使用右侧操作区域中心，并夹紧在边界内：

    right_button_width = min(120.0, width / 2.0)
    position = {
        "x": max(width / 2.0, width - right_button_width / 2.0),
        "y": height / 2.0,
    }

记录 host_box 与 position，但不记录正文。若宿主宽度小于等于零，继续抛出 BrowserPageChanged。

- [ ] **Step 4: 运行字段和点击回归测试**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py -q

Expected: PASS；字段未持久化时在点击前失败，正常图文和上传发布仍能填写、验证并点击右侧发布区域。

- [ ] **Step 5: 提交该独立改动**

    git add src/agentkit/connectors/xhs_publisher_playwright.py tests/unit/test_xhs_publication.py
    git commit -m "fix: verify xhs form values before publication"

### Task 3: 收集脱敏提交证据并保留人工观察窗口

**Files:**

- Modify: src/agentkit/config.py:235-255
- Modify: skills/xhs-growth-campaign/scripts/providers.py:197-266
- Modify: src/agentkit/connectors/xhs_publisher_playwright.py:1-55, 337-420, 730-790
- Modify: .env.example:101-129
- Modify: docs/XHS_WEB_SEARCH.md:77-137
- Test: tests/unit/test_config.py:1-90
- Test: tests/unit/test_xhs_publication.py:111-520
- Test: tests/unit/test_xhs_skill_providers.py

- [ ] **Step 1: 写出观察窗口和脱敏网络记录的失败测试**

在 _PublishPage 中加入 on(event, callback)、emit_response(response) 和 wait_calls。构造只含 URL、方法、资源类型、状态码的假 request/response，验证错误信息：

    assert "POST /api/sns/v1/note" in str(error.value)
    assert "200" in str(error.value)
    assert "secret-body" not in str(error.value)
    assert page.wait_calls == [90_000]

再增加 headless 等效测试：传入 observation_seconds=0 时，结果未知不等待，但账本仍保持 unknown。

在 tests/unit/test_config.py 中验证：

    assert s.browser_publish_observation_seconds == 90.0
    assert _fresh_settings(
        monkeypatch, AGENTKIT_BROWSER_PUBLISH_OBSERVATION_SECONDS="15"
    ).browser_publish_observation_seconds == 15.0

并把 AGENTKIT_BROWSER_PUBLISH_OBSERVATION_SECONDS 加入 _fresh_settings() 清理的环境变量列表，避免测试进程间泄漏设置。

- [ ] **Step 2: 运行测试并确认失败原因正确**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py tests/unit/test_config.py -q

Expected: FAIL，因为当前适配器没有 response listener、观察窗口设置或脱敏网络摘要。

- [ ] **Step 3: 实现设置、证据记录和未知结果观察**

在 Settings 中增加：

    browser_publish_observation_seconds: float = Field(default=90.0, ge=0.0, le=300.0)

在 XhsPublishAdapter.__init__() 增加 observation_seconds: float = 0.0，拒绝负值。创建 tests/unit/test_xhs_skill_providers.py，通过 declarative_catalog._load_entrypoint() 加载声明式 Skill 的 provider 工厂，并分别断言 headed 配置传入 15 秒、headless 配置传入 0 秒：

    build_provider = _load_entrypoint(
        REPO_ROOT,
        REPO_ROOT / "skills" / "xhs-growth-campaign",
        "scripts.providers:build_playwright_publishing_provider",
    )
    settings = Settings(_env_file=None, xhs_publishing_provider="playwright")
    headed = build_provider(
        settings,
        {"browser_headless": "false", "browser_publish_observation_seconds": 15},
    )
    headless = build_provider(
        settings,
        {"browser_headless": "true", "browser_publish_observation_seconds": 15},
    )

    assert headed.adapter.observation_seconds == 15.0
    assert headless.adapter.observation_seconds == 0.0

Skill provider 构造适配器时仅在 browser_config.headless is False 传递配置值。

增加 _PublishEvidenceRecorder，在点击前注册 page.on("response", callback)。回调只保留以下字段：

    {
        "method": request.method,
        "path": urlparse(request.url).path,
        "status": response.status,
        "resource_type": request.resource_type,
    }

只接受 https 且 hostname 为 xiaohongshu.com 或其子域名的非 GET 响应；最多保留最近 20 条，禁止保存 query、Cookie、请求体、响应体或鉴权头。

点击后继续使用页面成功状态确认发布。捕获确认超时时，先读取页面状态，再调用 _capture_diagnostics(..., evidence=recorder.summary(), click=click_metadata)；若 observation_seconds > 0，调用一次 page.wait_for_timeout(int(observation_seconds * 1000))，然后抛出 XhsPublishOutcomeUnknown。不得把任意 2xx 网络响应单独解释为发布成功，也不得再次点击发布。

- [ ] **Step 4: 更新环境示例和运行文档**

在 .env.example 的 Playwright 区域新增：

    # 仅在 headed 发布结果无法确认时保留浏览器供人工查看；0 表示立即关闭。
    AGENTKIT_BROWSER_PUBLISH_OBSERVATION_SECONDS=90

在 docs/XHS_WEB_SEARCH.md 的发布配置节写明：该窗口不会自动重试、不会绕过人机验证；失败日志只记录脱敏接口路径、HTTP 方法和状态码。

- [ ] **Step 5: 运行相关单元测试**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py tests/unit/test_config.py tests/unit/test_xhs_skill_providers.py -q

Expected: PASS；网络证据脱敏、headless 行为、观察窗口、provider 配置和既有小红书工作流均通过。

- [ ] **Step 6: 提交该独立改动**

    git add src/agentkit/config.py src/agentkit/connectors/xhs_publisher_playwright.py skills/xhs-growth-campaign/scripts/providers.py .env.example docs/XHS_WEB_SEARCH.md tests/unit/test_config.py tests/unit/test_xhs_publication.py tests/unit/test_xhs_skill_providers.py
    git commit -m "feat: add observable xhs publish confirmation"

### Task 4: 全链路静态检查与受控人工验证

**Files:**

- Verify: tests/integration/test_xhs_publish_approval.py
- Verify: tests/unit/test_declarative_catalog.py
- Verify: src/agentkit/connectors/xhs_publisher_playwright.py

- [ ] **Step 1: 运行批准链路集成测试**

Run:

    .\.venv\Scripts\python.exe -m pytest tests/integration/test_xhs_publish_approval.py tests/unit/test_declarative_catalog.py -q

Expected: PASS；审批门、工具白名单与声明式 Agent 注册不回归。

- [ ] **Step 2: 运行静态检查**

Run:

    .\.venv\Scripts\python.exe -m ruff check src skills tests
    .\.venv\Scripts\python.exe -m mypy src

Expected: 两个命令均以 0 退出；新增的 Playwright fake 类型或可选 page 参数没有类型错误。

- [ ] **Step 3: 运行完整测试套件**

Run:

    .\.venv\Scripts\python.exe -m pytest -q

Expected: PASS；若工具时间限制中断输出，则记录中断原因，不得把未完成的套件宣称为通过。

- [ ] **Step 4: 进行一次受控 headed 人工验证**

先用独立 AgentKit 浏览器档案完成登录：

    agentkit --tenant company_alpha browser-login xhs --target publish

然后运行一次经审批的测试发布。验证日志必须包含字段回读、发布宿主边界、点击点和脱敏网络摘要；若平台没有明确确认，窗口保持最多 90 秒且账本状态为 unknown。在创作中心人工确认结果前，不得用相同幂等键再次执行发布。

## 自检清单

- 任务 1 覆盖图文生成和已有媒体上传的秒开秒关根因。
- 任务 2 覆盖标题/正文实际值验证和 closed shadow root 宿主点击证据。
- 任务 3 覆盖网络脱敏、结果未知观察窗口、headless 限制、配置和文档。
- 任务 4 覆盖审批、声明式注册、静态检查和不重复发布的人工验证。
- 计划中没有要求绕过短信、验证码或平台审核，也没有把网络 2xx 单独认定为发布成功。
