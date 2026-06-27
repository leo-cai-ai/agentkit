# Phase 2 设计：框架化 / fork 体验

- 日期: 2026-06-26
- 状态: 已与用户确认范围（hybrid 发现 / runtime+CLI 多租户 / per-tenant DB / new-pack+new-tenant 脚手架）
- 方法论: superpowers（brainstorming → writing-plans）
- 阶段定位: 把「架构扎实的 demo」变成「fork 后易扩展的框架」。

## 0. 总原则

**不改变默认行为**：`build_runtime()` 无参时仍加载 `company_alpha` 租户，输出与现状等价；现有集成测试保持绿。新增能力均为「可选、可发现、可生成」。

确认的范围取舍：
| 维度 | 决定 |
|------|------|
| Pack 发现 | 混合：扫描仓内 `domain_packs/*` + entry points 加载外部包 |
| 多租户 | runtime + CLI 按 `tenant_id` 加载；web 暂用默认/env 驱动 |
| 审计 DB | 每租户独立 `data/<tenant_id>.sqlite` |
| 脚手架 | `agentkit new-pack <name>` + `agentkit new-tenant <id>` |

## 1. Domain Pack 插件发现（hybrid）

### 1.1 现状
`bootstrap.DOMAIN_PACKS` 是硬编码 dict，顶部直接 `import` 两个 pack。加业务域必须改 core。

### 1.2 设计
新增 `src/agentkit/runtime/pack_registry.py`：

- Pack 约定：每个 pack 模块导出 `DOMAIN: str` 与 `register(*, agents, skills, tools, tenant_config) -> None`。
- 类型别名 `RegisterFn = Callable[..., None]`；可选 `Protocol` 文档化扩展契约。
- `discover_packs() -> dict[str, RegisterFn]`：
  1. **仓内扫描**：`pkgutil.iter_modules(agentkit.domain_packs.__path__)` 找子包，导入 `<pkg>.pack`，
     若同时具备 `DOMAIN` + `register` 则登记 `DOMAIN -> register`。
  2. **entry points**：`importlib.metadata.entry_points(group="agentkit.domain_packs")`，每个入口点加载到
     pack 模块（读取其 `DOMAIN` + `register`）。entry points 可补充/覆盖仓内同名域。
  - **健壮性**：单个 pack 导入失败只 `log.warning` 跳过，不影响其它 pack（遵循日志安全，不打印敏感信息）。
  - 结果顺序确定（按域名排序），便于测试与可复现。
- `bootstrap.build_runtime` 改用 `discover_packs()`，删除硬编码 dict 与顶部 pack import。
  保留薄的向后兼容：模块级 `DOMAIN_PACKS` 改为 `discover_packs()` 的惰性调用包装（或直接移除，仅内部使用）。

### 1.3 收益
fork 只需在 `domain_packs/` 丢一个带 `DOMAIN`+`register` 的文件夹即可被发现；外部团队可发布独立 pip 包经 entry points 注入。

## 2. 多租户按 tenant_id 加载

### 2.1 现状
`load_tenant_config()` 硬编码 `tenants/company_alpha.json`；`build_runtime()` 无 tenant 入参；web `get_runtime()` 为 `lru_cache(maxsize=1)`（单租户）。

### 2.2 设计（runtime + CLI 层）
- `load_tenant_config(tenant_id: str) -> dict`：读 `tenants/<tenant_id>.json`；缺失抛清晰错误（列出可用租户）。
- `list_tenants() -> list[str]`：扫描 `tenants/*.json` 返回文件名 stem（排序）。
- `resolve_tenant_id(explicit: str | None = None) -> str`：优先级 显式参数 → `AGENTKIT_TENANT_ID` → 默认 `company_alpha`。
- `build_runtime(*, tenant_id: str | None = None, db_path: Path | None = None) -> DemoRuntime`：
  - `tenant_id = resolve_tenant_id(tenant_id)`（文件选择器；与 `tenant_config["tenant_id"]` 逻辑 id 区分，后者仍供 gateway/audit 使用）。
  - `db_path` 缺省 = `data/<tenant_id>.sqlite`（**每租户独立**）；显式传入则尊重（保持现有测试行为）。
  - 其余装配不变；`DemoRuntime` 增加 `tenant_id` 字段（文件选择器）以便 web/CLI 显示。
- CLI：`--tenant <id>` 全局可选参数（run-demo / web）；未给则走 `resolve_tenant_id`（env）。
- web：`get_runtime()` 改为按 `resolve_tenant_id()` 选择，并把缓存键改为 tenant_id（`dict` 缓存而非 maxsize=1），UI 不加切换器（范围外）。

### 2.3 隔离
每租户独立 sqlite 文件即物理隔离审计数据；`data/` 目录按需创建。

## 3. 脚手架命令

新增 `src/agentkit/runtime/scaffold.py`（纯函数产出内容 + 写盘助手），CLI 接线：

- `agentkit new-tenant <id> [--force]`：在 `tenants/<id>.json` 写入最小可用租户模板（含 tenant_id、enabled_domains 空、role_permissions、ui 基本项）。已存在且无 `--force` 则报错。
- `agentkit new-pack <name> [--force]`：在 `src/agentkit/domain_packs/<name>/` 生成 `__init__.py` + `pack.py` 骨架
  （`DOMAIN`、`register`、一个示例 skill/tool/agent）。已存在则报错。生成物应能被 `discover_packs()` 发现。

模板以模块级字符串常量维护；生成的 JSON/py 必须合法（测试校验）。

## 4. 扩展 API 文档化
- 形式化 pack 契约（`RegisterFn` 别名 + `register` 签名 + `DOMAIN` 约定），README 增「添加业务域 pack」「添加租户」「多租户运行」章节。
- 说明发现优先级（仓内扫描 + entry points）与 entry points 声明示例。

## 5. 影响文件（预估）
- 新增：`runtime/pack_registry.py`、`runtime/scaffold.py`。
- 改：`runtime/bootstrap.py`、`cli.py`、`web/app.py`、`README.md`、`pyproject.toml`（entry points group 示例，可选）。
- 测试：`tests/unit/test_pack_registry.py`、`test_scaffold.py`、`test_multitenant.py`（或并入 bootstrap 测试）、整体集成回归。

## 6. 非目标（留给 Phase 3 或后续）
- web 控制台租户切换 UI。
- 热重载 / 动态注册（运行时增删 pack）。
- 多租户鉴权与数据加密（Phase 3 安全）。
- skill_tool.py → `agentkit skill` 迁移（本期不做）。

## 7. 验收标准
1. `ruff check`、`ruff format --check`、`pytest` 全绿；`mypy src/agentkit/core` 不新增错误。
2. `discover_packs()` 在零硬编码 dict 下发现 `hr.recruitment` 与 `marketing.social_growth`；新增文件夹即被发现；坏 pack 被跳过且记录 warning。
3. `build_runtime()` 无参等价于 `company_alpha`（集成测试通过）；`build_runtime(tenant_id=...)` 加载对应文件，缺失租户报清晰错误。
4. 每租户审计落到 `data/<tenant_id>.sqlite`。
5. `agentkit new-tenant`/`new-pack` 生成合法且可发现/可加载的产物（测试校验），含覆盖保护。
6. README 有扩展/多租户文档。

## 8. 风险与缓解
- **扫描导入副作用/坏 pack** → 每 pack try/except + warning；确定性排序。
- **entry_points API 版本差异** → 目标 py3.11+，用 `group=` 关键字稳定 API。
- **per-tenant db 路径** → 确保 `data/` 存在；显式 db_path 仍可覆盖（保测试）。
- **行为漂移** → 先跑现有集成测试基线，改动小步提交常绿。

## 9. 下一步
进入 `writing-plans`，拆成 TDD 任务（确切文件、完整代码、红绿验证）。建议切片：
1. pack_registry（发现）+ 单测 → 接入 bootstrap；
2. 多租户加载（load/list/resolve + build_runtime tenant_id + per-tenant db）+ 测试 + CLI `--tenant`；
3. scaffold（new-pack/new-tenant）+ CLI 接线 + 测试；
4. 收尾：README 扩展/多租户文档 + 全门禁 + 整支 review。
