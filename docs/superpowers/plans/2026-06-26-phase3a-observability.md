# Phase 3a 实现计划：可观测性 + 健壮性

- 关联设计: `docs/superpowers/specs/2026-06-26-phase3-production-hardening-design.md` §3
- 执行: worktree `.worktrees/phase3a-observability`（分支 `phase3a-observability`，基于 `main`），TDD 红绿，小步提交。
- 环境: `python -m uv ...`；PowerShell 用 `;`；worktree 内先 `python -m uv sync --all-extras`。
- 每任务验证: `ruff check` / `ruff format --check` / `pytest -q`；收尾再 `mypy src/agentkit/core`。
- 不改业务行为。

## Task 1 — run_id 贯穿日志
- 新增 `src/agentkit/core/log_context.py`：
  - `_run_id: ContextVar[str]`（默认 `"-"`）；`current_run_id() -> str`；`set_run_id(run_id) -> Token`；`reset_run_id(token)`；`bind_run_id(run_id)` 上下文管理器。
- 改 `logging_config._RunIdFilter`：无 `record.run_id` 时取 `current_run_id()`。
- 接入 `langgraph_agent`：`_start_run_node` 创建 run_id 后 `set_run_id(run_id)`；`_finalize_node` 末尾 `reset`（或简单覆盖）。
- 测试 `tests/unit/test_log_context.py`：
  - 默认 `-`；`bind_run_id` 内 `current_run_id()` 正确、退出后恢复；
  - 用 `caplog` + 一条无 extra 的日志验证 filter 填入当前 run_id。
- 提交: `feat: propagate run_id into logs via contextvar`

## Task 2 — 节点耗时 / 指标事件
- 新增 `src/agentkit/core/metrics.py`：
  - `@contextmanager timed_event(audit, run_id, event_type, **fields)`：记开始 `time.perf_counter()`，退出时 `audit.record(run_id, event_type, {**fields, "duration_ms": ..., "ok": <无异常>})`；异常也记后再抛。
- 接入 `langgraph_agent` 各节点：用 `timed_event(self._audit, run_id, "node_timing", node=...)` 包住核心调用（understand_intent / route / plan / execute / review_output）。保持既有 `*_completed`/`graph_node_finished` 事件不变（仅新增 `node_timing`）。
- 审计新增只读聚合 `SQLiteAuditLog.event_timing_summary()`：按 `event_type` 从 `payload_json` 提取 `duration_ms` 聚合（count/avg_ms），用 SQLite `json_extract`。
- 测试：
  - `tests/unit/test_metrics.py`：`timed_event` 用 `InMemoryAuditLog` 记录含 `duration_ms`/`ok`；异常路径 `ok=False` 且异常上抛。
  - `tests/integration/test_timing_events.py`：假 provider 跑一次 `build_runtime` + handle，断言审计含 `node_timing` 事件且 `event_timing_summary()` 可返回。
- 提交: `feat: record per-node timing events and timing summary query`

## Task 3 — 可配限流与超时
- `config.Settings` 增 `llm_requests_per_second: float = 0.8`、`llm_rate_limiter_enabled: bool = True`。
- `llm/cisco.py`：删除模块级 `rate_limiter`；`CiscoCircuitProvider.__init__` 增 `requests_per_second: float = 0.8`、`rate_limiter_enabled: bool = True`，按参数构造 `InMemoryRateLimiter`（禁用时传 `None`）。
- `llm/factory.py`：从 settings 注入 `requests_per_second` / `rate_limiter_enabled`（cisco 分支）。
- 测试 `tests/unit/test_config.py`（追加）：默认值；env 覆盖 `AGENTKIT_LLM_REQUESTS_PER_SECOND`。`tests/unit/test_factory.py`（追加）：fake 分支不受影响；cisco 构造参数传递（可 monkeypatch CiscoCircuitProvider 验证 kwargs）。
- 提交: `feat: make LLM rate limit and timeout configurable`

## Task 4 — 收尾
- README 增「可观测性」小节：run_id 关联日志、节点耗时事件、`AGENTKIT_LLM_REQUESTS_PER_SECOND` / `AGENTKIT_LLM_RATE_LIMITER_ENABLED` / `AGENTKIT_LLM_TIMEOUT_SECONDS` 配置。
- 全门禁：ruff + format + pytest + `mypy src/agentkit/core`（不新增错误）。
- 整支 review（diff 排除 `uv.lock`）→ 合并 `main` → 清理 worktree。
- 提交: `docs: document Phase 3a observability and tuning`

## 验收（完成定义）
日志带真实 run_id；审计含 `node_timing` 且 `event_timing_summary()` 可用；rps/timeout 可经 env 配置且默认行为等价；门禁全绿；业务行为不变。
