# Phase 3b 实现计划：安全硬化

- 关联设计: `docs/superpowers/specs/2026-06-26-phase3-production-hardening-design.md` §4
- 执行: worktree `.worktrees/phase3b-security`（分支 `phase3b-security`，基于 `main`），TDD。
- 环境: `python -m uv ...`；worktree 内先 `python -m uv sync --all-extras`。
- 每任务验证: `ruff check` / `ruff format --check` / `pytest -q`；收尾 `mypy src/agentkit/core`。

## Task 1 — 密钥不外泄（SecretStr）
- `config.Settings`：`cisco_client_secret`、`cisco_app_key`、`openai_api_key` 改为 `SecretStr | None`；新增 Web 安全配置：
  - `web_auth_token: SecretStr | None = None`
  - `web_secret_key: SecretStr | None = None`
  - `web_cookie_secure: bool = True`
  - `web_auth_disabled: bool = False`
- `llm/factory.py`：读取处 `.get_secret_value()`（None 安全）。
- 测试 `tests/unit/test_config.py`（追加）：secret 字段为 `SecretStr`；`repr(settings)` 不含明文；env 注入后 `.get_secret_value()` 正确。
- 提交: `feat: store secrets as SecretStr and add web security settings`

## Task 2 — Web 鉴权 + cookie/安全头 + CSRF
- 新增 `src/agentkit/web/security.py`：
  - 纯函数：`token_matches(provided, expected)`（`hmac.compare_digest`，空安全）、`ensure_csrf_token(session)`、`csrf_matches(session, sent)`、`security_headers()`（返回头 dict）、`auth_required(endpoint, method, settings, session)` → `("ok"|"login"|"forbidden"|"csrf"|"unconfigured")`（纯决策，便于单测）。
  - `configure_security(app)`：设 `secret_key`（来自 `web_secret_key`，缺省随机并告警）、cookie 配置（HTTPONLY/SAMESITE=Strict/SECURE 可配）；注册 `before_request`（鉴权 + 改状态请求 CSRF 校验）、`after_request`（安全头 + 敏感页 `Cache-Control: no-store`）、`/login`、`/logout`、`csrf_token` context processor。幂等（`app._agentkit_security` 标志，路由只注册一次；钩子内实时读 `get_settings()`）。
  - Fail-closed：未配 `web_auth_token` 且未 `web_auth_disabled` → 受保护路由 503（提示配置）；本地开发设 `AGENTKIT_WEB_AUTH_DISABLED=true` 放行。
  - 公共端点：`login`、`logout`、`healthz`、`static`。
- 新增模板 `templates/login.html`（极简，POST token 表单 + 错误提示）。
- `web/app.py`：import 后调用 `configure_security(app)`。
- `templates/base.html`：`<head>` 注入 `<meta name="csrf-token" content="{{ csrf_token }}">`。
- `static/js/app.js`：`postTask` 带 `X-CSRF-Token` 头（读 meta）。
- 测试：
  - `tests/unit/test_web_security.py`：`token_matches`、`csrf_matches`、`security_headers`、`auth_required` 各分支（含 fail-closed、public 放行、POST 缺 CSRF）。
  - `tests/integration/test_web_auth.py`（Flask test client，env 配 token + secret_key + `web_cookie_secure=false`，`get_settings.cache_clear()` 后 `configure_security`）：
    - 未登录 GET `/` → 302 到 `/login`；
    - 错误 token POST `/login` → 401；正确 → 302 且后续 `/` 200；
    - 响应含安全头（`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、CSP）；
    - 登录后 POST `/api/tasks` 缺 `X-CSRF-Token` → 400；带正确 → 不为 400（可能 200/4xx 业务码，断言 `!=400`）；
    - `AGENTKIT_WEB_AUTH_DISABLED=true` 时 `/` 直接 200。
- 提交: `feat: add web console token auth, CSRF, and security headers`

## Task 3 — 收尾
- README「安全」小节：Web 令牌鉴权（`AGENTKIT_WEB_AUTH_TOKEN`）、`AGENTKIT_WEB_SECRET_KEY`、`AGENTKIT_WEB_COOKIE_SECURE`、`AGENTKIT_WEB_AUTH_DISABLED`、cookie/安全头/CSRF 说明、密钥经 env 注入且不入日志。
- 全门禁：ruff + format + pytest + `mypy src/agentkit/core`（不新增错误）。
- 整支 review（排除 `uv.lock`）→ 合并 `main` → 清理 worktree。
- 提交: `docs: document Phase 3b security hardening`

## 验收（完成定义）
未带凭证访问受保护路由被挡（302/503）；正确令牌登录可用；密钥 `repr` 脱敏；安全响应头齐备；改状态 POST 无 CSRF 令牌被拒；门禁全绿；GET 路由业务行为不变。
