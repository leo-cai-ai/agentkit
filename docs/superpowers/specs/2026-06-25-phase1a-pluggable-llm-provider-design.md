# Phase 1a — 可插拔 LLM Provider + 类型化配置 设计

- 日期: 2026-06-25
- 状态: 已与用户确认（方案 A；自写轻量重试；全局 env 选 provider）
- 方法论: superpowers（brainstorming → writing-plans）
- 上游: `docs/superpowers/specs/2026-06-25-project-engineering-roadmap-design.md`（Phase 1）

## 1. 背景与目标

Phase 0 已把 `demoagent` 工程化为可安装包 `agentkit`。当前 LLM 层（`agentkit/llm/cisco.py`）把 Cisco Circuit/Gemini 写死，且 **import 时若缺凭证直接 `raise`**；所有图节点通过 `agentkit/core/llm_client.py` 的 `require_chat(system, user) -> str` / `require_chat_json(...)` 单轮调用。

**Phase 1a 目标**：把 LLM 后端做成**可插拔 provider 抽象**，由**类型化配置**驱动选择，**消除 import 即崩**，加入**重试/超时**，并提供 **FakeProvider** 解锁 Phase 0 欠下的整图集成测试。**对外签名与图节点零改动。**

### 已确认约束
| 维度 | 决定 |
|------|------|
| 范围 | 仅 Phase 1a：provider 抽象 + 类型化配置 + FakeProvider（prompt 注入/schema 校验/治理去重各自单独做） |
| Provider | Cisco（封装现有）+ 通用 OpenAI 兼容（base_url+api_key+model）+ Fake（测试） |
| 配置 | pydantic-settings（新增 `pydantic` / `pydantic-settings` 依赖） |
| 重试 | 自写轻量指数退避，**零新依赖**（不引 tenacity） |
| Provider 选择 | 全局 env（`AGENTKIT_LLM_PROVIDER`）这一层足够 |
| 接口 | 窄接口 `complete(system, user) -> str`（精确匹配现有用法） |

## 2. 设计

### 2.1 接口与文件（`src/agentkit/`）

- `llm/base.py`
  - `class LLMProvider(Protocol)`: `name: str`；`def complete(self, system: str, user: str) -> str`。
  - 复用 `agentkit.core.llm_client.LLMRequiredError`（保持单一异常类型；`base.py` 从 llm_client 引入或反之——见 2.4 以避免循环依赖）。

- `llm/cisco.py` → 重构为 `class CiscoCircuitProvider`
  - 保留 `SignatureAwareAzureChatOpenAI` 兼容层、`CircuitAuth`、限流器。
  - **import 时不读 env、不 raise、不建 model**。构造 `CiscoCircuitProvider(config)` 时校验三件套凭证，缺失抛 `LLMRequiredError`（清晰报错）。
  - `complete(system, user)`：用 `SystemMessage/HumanMessage` 调 `model.invoke`，抽取文本（沿用现有 list-parts 处理）。
  - 移除模块级 `model` 与注释掉的 DeepSeek 块（OpenAI 兼容 provider 已覆盖 DeepSeek 场景）。

- `llm/openai_compatible.py` → `class OpenAICompatibleProvider`
  - 用 `langchain_openai.ChatOpenAI(base_url=..., api_key=..., model=...)`（OpenAI/DeepSeek/本地 vLLM）。
  - `complete(system, user)` 同样的文本抽取。

- `llm/fake.py` → `class FakeProvider`
  - 确定性、可脚本化：构造可传 `responder: Callable[[str, str], str]`（按 system/user 内容派发）或 `responses: list[str]`（队列）。默认返回最简合法响应。
  - 用于单测与整图集成测试，无网络/无凭证。

- `llm/factory.py`
  - `def build_provider(settings: Settings) -> LLMProvider`：按 `settings.llm_provider` 选择并构造对应 provider。

- `config.py`（包根 `agentkit/config.py`）
  - pydantic-settings `Settings`：
    - `llm_provider: Literal["cisco", "openai", "fake"] = "cisco"`
    - `llm_max_retries: int = 2`，`llm_timeout_seconds: float = 30.0`，`llm_retry_base_delay: float = 0.5`
    - Cisco：`cisco_client_id/secret/app_key: str | None`
    - OpenAI 兼容：`openai_base_url: str | None`，`openai_api_key: str | None`，`openai_model: str | None`，`openai_api_version: str | None`
    - `model_config = SettingsConfigDict(env_prefix="AGENTKIT_", env_file=".env", extra="ignore")`
    - 兼容现有 `.env`：Cisco 字段用 `validation_alias` 接受裸 `CISCO_CLIENT_ID` 等（不强制改用户现有 .env）。
  - `@lru_cache def get_settings() -> Settings`。**实例化时不因缺某 provider 的凭证而失败**——只校验被选中 provider 的必需项，且校验发生在 `build_provider`/首次 `complete`，不是 import。

### 2.2 `core/llm_client.py` 改动（对外 API 不变）
- 用 `_get_provider()`（`@lru_cache`，`build_provider(get_settings())`）替换 `_load_model()`/`require_model()` 的模型加载。
- `require_chat(system, user)`：调用 `_with_retry(lambda: provider.complete(system, user))`；失败包成 `LLMRequiredError`。
- `_with_retry`：自写循环，按 `settings.llm_max_retries` 次重试、指数退避（`base_delay * 2**attempt`）；超时由 provider 内部 client 控制（Cisco httpx timeout / ChatOpenAI request_timeout 取自 settings）。
- 保留 `require_chat`/`require_chat_json`/`chat`/`chat_json`/`LLMRequiredError` 的签名与语义；`llm_available()` 改为「能否构造选中 provider」。
- 结果：`intent/router/planner/governance/executor/conversation/hr pack` **零改动**。

### 2.3 行为与兼容
- 默认 `AGENTKIT_LLM_PROVIDER` 未设 → `cisco`，配合现有 `.env` → 行为与今天一致。
- `agentkit run-demo` / `web` 行为不变（除新增日志已在 Phase 0）。
- import `agentkit.llm.*` 不再触发网络或凭证校验。

### 2.4 循环依赖与放置
- `LLMRequiredError` 当前在 `core/llm_client.py`。为避免 `llm/*` 依赖 `core`，将 `LLMRequiredError` 下沉到 `agentkit/llm/base.py`，`core/llm_client.py` 改为 `from agentkit.llm.base import LLMRequiredError` 并 re-export（保持 `from agentkit.core.llm_client import LLMRequiredError` 的现有引用不破）。

## 3. 测试

### 单元（`tests/unit/`）
- `test_config.py`：env 解析、默认值、Cisco 别名（裸 `CISCO_*`）、`get_settings` 缓存；选中 provider 缺凭证时在 `build_provider`/`complete` 报 `LLMRequiredError`（不在 import）。
- `test_factory.py`：`llm_provider` → 对应 provider 类型；未知值校验失败。
- `test_fake_provider.py`：队列/responder 行为。
- `test_llm_client_retry.py`：用 Fake（前 N 次抛、随后成功）验证重试成功；超出次数抛 `LLMRequiredError`；`require_chat_json` 解析。

### 集成（`tests/integration/`）——补 Phase 0 欠的整图测试
- `test_graph_with_fake_provider.py`：设 `AGENTKIT_LLM_PROVIDER=fake`（用 monkeypatch 环境或直接注入 settings/provider），脚本化 Fake 按节点 system prompt 关键字返回合法 JSON/文本：
  - intent 节点 → 合法 IntentFrame JSON
  - route 节点 → `{"skill_name": "candidate.rank", ...}`
  - plan / plan-review / approval-assessment / output-review → 各自合法 JSON
  - rank 摘要（require_chat）→ 任意文本
  - 断言 `build_runtime` + `gateway.handle(HR 请求, approved_skills=[candidate.rank])` 跑到 finalize、`output.governance` 完整、`ranked_candidates` 正确；以及一条 chit-chat 请求走对话回退分支。
- 无网络/无真实凭证。

## 4. 验收标准
1. `import agentkit.llm.cisco` / `agentkit.config` 不读 env、不 raise、不建 model。
2. `AGENTKIT_LLM_PROVIDER` 在 cisco/openai/fake 间切换；缺凭证只在使用时清晰报错。
3. 所有图节点未改动；`require_chat/require_chat_json` 签名语义不变。
4. 重试/超时按 settings 生效（有测试覆盖）。
5. 整图集成测试用 FakeProvider 跑通，无网络/凭证。
6. `ruff check`/`ruff format --check`/`pytest` 全绿；`mypy src/agentkit`（informational）。
7. `agentkit run-demo` 默认仍走 Cisco，行为不变（有凭证时手动验证）。

## 5. 风险与缓解
- **现有 .env 字段名**（裸 `CISCO_*`）与 `AGENTKIT_` 前缀冲突 → 用 pydantic `validation_alias` 显式接受裸名，避免要求用户改 .env。
- **循环依赖** → `LLMRequiredError` 下沉到 `llm/base.py`，core 侧 re-export。
- **Fake 整图测试脆弱**（依赖各节点 prompt 文案派发）→ 按稳定关键字（如 "intent decomposition"/"routing node"/"plan-review"）派发，并在测试内集中定义，文案变更时一处维护。
- **行为回归** → provider 重构保持 `complete` 的文本抽取逻辑与原 `require_chat` 一致；默认路径仍是 Cisco。

## 6. 非目标
prompt 文件注入、skill schema 运行时校验、治理审批去重、按 agent/tenant 选模型、异步/流式、tool-calling 经 LLM。各自后续单独 spec。

## 7. 下一步
进入 superpowers `writing-plans`，将 Phase 1a 拆成 2–5 分钟粒度的 TDD 任务（含确切文件路径、完整代码、红绿验证）。实现阶段在隔离 worktree（`using-git-worktrees`）进行，基于当前 `main`（已含 Phase 0）。
