# Phase 3c 实现计划：打包部署

- 关联设计: `docs/superpowers/specs/2026-06-26-phase3-production-hardening-design.md` §5
- 执行: worktree `.worktrees/phase3c-packaging`（分支 `phase3c-packaging`，基于 `main`），TDD（可测部分）。
- 环境: `python -m uv ...`；worktree 内先 `python -m uv sync --all-extras`。
- 注意: 本机可能无 docker daemon，镜像构建无法在此验证；Dockerfile/compose 按 IaC + 容器安全规则编写并人工核对，`/healthz` 用 Flask test client 测。

## Task 1 — /healthz 健康检查端点
- `web/app.py`：新增 `@app.get("/healthz")` → `jsonify({"status": "ok"})`，公共端点（已在 `security.PUBLIC_ENDPOINTS`）。不触发 LLM、不强制 build_runtime（轻量存活探针）。
- 测试 `tests/integration/test_healthz.py`：鉴权开启且未登录时 `/healthz` 仍 200 且 `{"status":"ok"}`。
- 提交: `feat: add /healthz liveness endpoint`

## Task 2 — 容器化运行入口（gunicorn）
- `pyproject.toml`：新增可选依赖组 `serve = ["gunicorn>=22.0.0,<24.0.0"]`（仅容器用，不入默认依赖）。
- 不改 `cli._run_web`（本地仍用 Flask dev server）；容器用 gunicorn 直接挂载 `agentkit.web.app:app`。
- 提交: `chore: add gunicorn serve extra for container deployment`

## Task 3 — Dockerfile / compose / dockerignore
- `Dockerfile`（多阶段，遵循容器安全规则）：
  - builder 阶段：`python:3.11-slim`，装 `uv`，`uv sync --extra serve`（或 `pip install . gunicorn`）到 venv。
  - runtime 阶段：`python:3.11-slim`，复制 venv 与源码；`ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`；创建非 root 用户并 `USER`；`EXPOSE 8501`；`HEALTHCHECK` 用 python urllib 探 `/healthz`；`CMD ["gunicorn","-b","0.0.0.0:8501","agentkit.web.app:app"]`。
- `docker-compose.yml`：
  - service `web`：`build: .`，`ports: ["8501:8501"]`，`env_file: .env`（注入 `AGENTKIT_*` 密钥/令牌），
  - volumes：`./tenants:/app/tenants:ro`、`./prompts:/app/prompts:ro`、`./skills:/app/skills:ro`、命名卷 `agentkit_data:/app/data`（rw，持久审计库），
  - `security_opt: ["no-new-privileges:true"]`、`cap_drop: ["ALL"]`、`read_only: true` + `tmpfs: ["/tmp"]`，
  - `healthcheck` 走 `/healthz`，`restart: unless-stopped`。
- `.dockerignore`：`.git`、`.venv`、`data/`、`**/__pycache__`、`.ruff_cache`、`.mypy_cache`、`.worktrees`、`*.sqlite`、`docs/`、`tests/`。
- 提交: `feat: add Dockerfile, docker-compose, and dockerignore`

## Task 4 — 收尾
- README「容器化部署」小节：`docker compose up --build`、必填 env（`AGENTKIT_WEB_AUTH_TOKEN`/`AGENTKIT_WEB_SECRET_KEY`、LLM 凭据）、`/healthz` 说明、数据卷持久化、内网 http 可设 `AGENTKIT_WEB_COOKIE_SECURE=false`。
- 全门禁：ruff + format + pytest + `mypy src/agentkit/core`（不新增错误）。
- 整支 review（排除 `uv.lock`）→ 合并 `main` → 清理 worktree。
- 提交: `docs: document container deployment`

## 验收（完成定义）
`/healthz` 公共可达返回 ok；Dockerfile 非 root + healthcheck + 最小基础镜像 + 无 docker.sock；compose 注入 env、持久化 data 卷、no-new-privileges；`.dockerignore` 排除敏感/无关文件；README 文档齐备；门禁全绿；既有行为不变。
