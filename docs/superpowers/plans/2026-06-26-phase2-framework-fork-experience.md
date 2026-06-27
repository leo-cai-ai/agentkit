# Phase 2 实现计划：框架化 / fork 体验

- 关联设计: `docs/superpowers/specs/2026-06-26-phase2-framework-fork-experience-design.md`
- 执行: worktree `.worktrees/phase2-framework`（分支 `phase2-framework`，基于 `main`），TDD 红绿，小步提交。
- 环境: `uv` 不在 PATH → `python -m uv ...`；Windows PowerShell 无 `&&`，用 `;`。worktree 内先 `python -m uv sync --all-extras`。
- 每任务验证: `python -m uv run ruff check .`；`ruff format --check .`；`pytest -q`。

## Task 1 — Pack 插件发现（pack_registry）
- 新增 `src/agentkit/runtime/pack_registry.py`：
  - `RegisterFn` 类型别名；`discover_packs() -> dict[str, RegisterFn]`。
  - 仓内扫描：`pkgutil.iter_modules(agentkit.domain_packs.__path__, prefix)`，导入 `<pkg>.pack`，取 `DOMAIN`+`register`。
  - entry points：`importlib.metadata.entry_points(group="agentkit.domain_packs")`，加载模块取 `DOMAIN`+`register`（可覆盖仓内）。
  - 单 pack 失败 → `logging.getLogger("agentkit.packs").warning(...)` 跳过；按域名排序返回。
- 新增 `tests/unit/test_pack_registry.py`：
  - 发现内置 `hr.recruitment` + `marketing.social_growth`；
  - 注入临时坏模块验证被跳过（或 monkeypatch iter_modules）；
  - monkeypatch `entry_points` 注入一个假 pack，验证被登记/可覆盖。
- 接入 `bootstrap.build_runtime`：用 `discover_packs()` 取代硬编码 dict，删除顶部 pack import 与 `DOMAIN_PACKS` 常量（仅内部使用）。
- 提交: `feat: discover domain packs via in-repo scan + entry points`

## Task 2 — 多租户按 tenant_id 加载
- `bootstrap` 增：`list_tenants() -> list[str]`、`resolve_tenant_id(explicit=None) -> str`（显式→env `AGENTKIT_TENANT_ID`→`company_alpha`）。
- 改 `load_tenant_config(tenant_id: str) -> dict`（读 `tenants/<id>.json`，缺失抛含可用列表的清晰错误）。
- 改 `build_runtime(*, tenant_id: str | None = None, db_path: Path | None = None)`：
  - 解析 tenant_id；`db_path` 缺省 `DEMO_ROOT/data/<tenant_id>.sqlite`（确保 `data/` 存在）；显式仍尊重。
  - `DemoRuntime` 增 `tenant_id` 字段。
- `cli.py`：加 `--tenant` 全局可选参数，传入 `build_runtime(tenant_id=...)`。
- `web/app.py`：`get_runtime()` 改 dict 缓存（键=`resolve_tenant_id()`）。
- 新增 `tests/unit/test_multitenant.py`：load 指定/缺失租户；list_tenants；resolve 优先级（monkeypatch env）；build_runtime per-tenant db 路径。
- 回归：现有 `test_build_runtime` / fake-provider 集成测试（显式 db_path）保持绿。
- 提交: `feat: load tenants by id with per-tenant audit db`

## Task 3 — 脚手架 new-pack / new-tenant
- 新增 `src/agentkit/runtime/scaffold.py`：
  - `render_tenant_config(tenant_id) -> str`（最小合法 JSON）；`render_pack_module(domain) -> tuple[init, pack]`。
  - `create_tenant(tenant_id, *, root, force=False) -> Path`；`create_pack(name, *, src_root, force=False) -> Path`（覆盖保护 → FileExistsError）。
- `cli.py`：`new-tenant <id> [--force]`、`new-pack <name> [--force]` 子命令。
- 新增 `tests/unit/test_scaffold.py`：
  - 生成的 tenant JSON 可 `json.loads` 且含 tenant_id；
  - 生成的 pack 模块写入临时包后能被 `discover_packs()`（或直接 import 校验 `DOMAIN`+`register`）发现；
  - 已存在且无 force → 抛错。
- 提交: `feat: add new-pack and new-tenant scaffolding commands`

## Task 4 — 收尾
- `README.md`：新增「添加业务域 pack（含 entry points 声明示例）」「添加租户」「多租户运行（--tenant / AGENTKIT_TENANT_ID / per-tenant db）」。
- 全门禁：ruff + format + pytest + `mypy src/agentkit/core`（不新增错误）。
- 折叠累计小修；整支 review（diff 排除 `uv.lock`）。
- 提交: `docs: document Phase 2 extension and multi-tenant usage`

## 验收（完成定义）
对齐设计第 7 节：门禁全绿；零硬编码 dict 下发现两内置 pack 且坏 pack 跳过；`build_runtime()` 无参等价 company_alpha、按 id 加载、缺失报错；每租户独立 db；脚手架产物合法可发现且有覆盖保护；README 文档齐备。
