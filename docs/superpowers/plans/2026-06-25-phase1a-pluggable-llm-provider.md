# Phase 1a — 可插拔 LLM Provider + 类型化配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LLM 后端做成由类型化配置驱动的可插拔 provider（Cisco / OpenAI 兼容 / Fake），消除 import 即崩，加重试/超时，并用 FakeProvider 跑通整图集成测试——图节点与 `llm_client` 对外 API 零改动。

**Architecture:** 新增 `LLMProvider` 协议（窄接口 `complete(system, user) -> str`）+ pydantic-settings 配置 + 工厂选择；`core/llm_client.py` 内部改走配置选中的 provider 并包一层自写重试。默认仍为 Cisco，行为不变。

**Tech Stack:** Python 3.11+、pydantic / pydantic-settings、langchain-openai、uv、pytest。

## Global Constraints
- 行为不变：图节点与 `require_chat/require_chat_json/chat/chat_json/LLMRequiredError` 的签名与语义保持不变；默认 provider=cisco。
- import 安全：`import agentkit.llm.*` 与 `agentkit.config` 不得读 env、不得 raise、不得建 model 或发网络。
- 凭证缺失只在「构造选中 provider / 首次 complete」时以 `LLMRequiredError` 清晰报错。
- 所有测试无需真实 LLM/网络/凭证。
- 接口窄：`complete(system: str, user: str) -> str`。
- 重试自写（零新增运行时依赖，除 pydantic/pydantic-settings）。
- provider 选择走全局 env `AGENTKIT_LLM_PROVIDER`（cisco|openai|fake，默认 cisco）。
- 现有 `.env` 的裸 `CISCO_CLIENT_ID/SECRET/APP_KEY` 必须仍被识别（pydantic `validation_alias`）。
- 工具链门禁：`ruff check .`、`ruff format --check .`、`pytest` 全绿。

---

### Task 1: 依赖 + 类型化配置 `agentkit/config.py`

**Files:**
- Modify: `pyproject.toml`（加 `pydantic`、`pydantic-settings` 运行时依赖）
- Create: `src/agentkit/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `agentkit.config.Settings`（pydantic-settings）；`agentkit.config.get_settings() -> Settings`（lru_cache）。字段见下。

- [ ] **Step 1: 加依赖**
在 `pyproject.toml` 的 `[project].dependencies` 末尾追加：
```toml
    "pydantic>=2.7.0,<3.0.0",
    "pydantic-settings>=2.3.0,<3.0.0",
```
Run: `python -m uv sync --extra dev`  → Expected: 解析安装成功，`uv.lock` 更新。

- [ ] **Step 2: 写失败测试 `tests/unit/test_config.py`**
```python
import importlib

import agentkit.config as config_mod


def _fresh_settings(monkeypatch, **env):
    # Ensure a clean env so .env / process env don't leak into assertions.
    for var in [
        "AGENTKIT_LLM_PROVIDER",
        "AGENTKIT_LLM_MAX_RETRIES",
        "CISCO_CLIENT_ID",
        "CISCO_CLIENT_SECRET",
        "CISCO_APP_KEY",
        "AGENTKIT_OPENAI_BASE_URL",
        "AGENTKIT_OPENAI_API_KEY",
        "AGENTKIT_OPENAI_MODEL",
    ]:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Build a Settings that ignores any on-disk .env for deterministic tests.
    return config_mod.Settings(_env_file=None)


def test_defaults(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.llm_provider == "cisco"
    assert s.llm_max_retries == 2
    assert s.cisco_client_id is None


def test_cisco_bare_env_aliases(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        CISCO_CLIENT_ID="cid",
        CISCO_CLIENT_SECRET="sec",
        CISCO_APP_KEY="ak",
    )
    assert s.cisco_client_id == "cid"
    assert s.cisco_client_secret == "sec"
    assert s.cisco_app_key == "ak"


def test_provider_selection_and_openai_fields(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AGENTKIT_LLM_PROVIDER="openai",
        AGENTKIT_OPENAI_BASE_URL="http://localhost:8000/v1",
        AGENTKIT_OPENAI_API_KEY="k",
        AGENTKIT_OPENAI_MODEL="m",
    )
    assert s.llm_provider == "openai"
    assert s.openai_base_url == "http://localhost:8000/v1"
    assert s.openai_model == "m"


def test_invalid_provider_rejected(monkeypatch):
    import pydantic

    try:
        _fresh_settings(monkeypatch, AGENTKIT_LLM_PROVIDER="bogus")
        raised = False
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_get_settings_cached(monkeypatch):
    config_mod.get_settings.cache_clear()
    a = config_mod.get_settings()
    b = config_mod.get_settings()
    assert a is b
```

- [ ] **Step 3: 运行，确认失败**
Run: `python -m uv run pytest tests/unit/test_config.py -v` → Expected: FAIL（`agentkit.config` 不存在）。

- [ ] **Step 4: 实现 `src/agentkit/config.py`**
```python
"""Typed runtime configuration (env / .env driven, import-safe)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTKIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["cisco", "openai", "fake"] = "cisco"
    llm_max_retries: int = 2
    llm_timeout_seconds: float = 30.0
    llm_retry_base_delay: float = 0.5

    # Cisco Circuit — accept the bare CISCO_* names from existing .env files.
    cisco_client_id: str | None = Field(default=None, validation_alias="CISCO_CLIENT_ID")
    cisco_client_secret: str | None = Field(default=None, validation_alias="CISCO_CLIENT_SECRET")
    cisco_app_key: str | None = Field(default=None, validation_alias="CISCO_APP_KEY")

    # OpenAI-compatible (read as AGENTKIT_OPENAI_*).
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_api_version: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: 运行，确认通过**
Run: `python -m uv run pytest tests/unit/test_config.py -v` → Expected: PASS。

- [ ] **Step 6: 提交**
```bash
git add pyproject.toml uv.lock src/agentkit/config.py tests/unit/test_config.py
git commit -m "feat: add typed pydantic-settings configuration"
```

---

### Task 2: Provider 协议 + 文本抽取 + FakeProvider

**Files:**
- Create: `src/agentkit/llm/base.py`
- Create: `src/agentkit/llm/fake.py`
- Test: `tests/unit/test_fake_provider.py`

**Interfaces:**
- Produces: `agentkit.llm.base.LLMProvider`（Protocol，`name: str`，`complete(system, user) -> str`）；`agentkit.llm.base.LLMRequiredError`；`agentkit.llm.base.extract_text(response) -> str`；`agentkit.llm.fake.FakeProvider(responder=None, responses=None)`。

- [ ] **Step 1: 写失败测试 `tests/unit/test_fake_provider.py`**
```python
import pytest

from agentkit.llm.base import LLMProvider, LLMRequiredError, extract_text
from agentkit.llm.fake import FakeProvider


def test_fake_is_llmprovider():
    fp = FakeProvider(responses=["x"])
    assert isinstance(fp, LLMProvider)
    assert fp.name == "fake"


def test_fake_queue():
    fp = FakeProvider(responses=["a", "b"])
    assert fp.complete("s", "u") == "a"
    assert fp.complete("s", "u") == "b"
    with pytest.raises(LLMRequiredError):
        fp.complete("s", "u")


def test_fake_responder_dispatches_on_inputs():
    fp = FakeProvider(responder=lambda system, user: f"{system}|{user}")
    assert fp.complete("S", "U") == "S|U"


def test_extract_text_handles_list_parts():
    class R:
        content = [{"text": "he"}, "llo"]

    assert extract_text(R()) == "hello"

    class R2:
        content = "plain"

    assert extract_text(R2()) == "plain"
```

- [ ] **Step 2: 运行，确认失败**
Run: `python -m uv run pytest tests/unit/test_fake_provider.py -v` → Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 `src/agentkit/llm/base.py`**
```python
"""LLM provider abstraction, shared error, and response text extraction."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class LLMRequiredError(RuntimeError):
    """Raised when an LLM-required runtime step cannot complete."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str:
        """Single-shot completion: system + user prompt -> text reply."""
        ...


def extract_text(response: Any) -> str:
    """Pull plain text out of a LangChain-style response message."""
    content = getattr(response, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        content = "".join(parts)
    return str(content or "")
```

- [ ] **Step 4: 实现 `src/agentkit/llm/fake.py`**
```python
"""Deterministic, scriptable provider for tests (no network/credentials)."""

from __future__ import annotations

from collections.abc import Callable

from agentkit.llm.base import LLMRequiredError


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        responder: Callable[[str, str], str] | None = None,
        responses: list[str] | None = None,
    ) -> None:
        self._responder = responder
        self._responses = list(responses) if responses is not None else None

    def complete(self, system: str, user: str) -> str:
        if self._responder is not None:
            return self._responder(system, user)
        if self._responses is not None:
            if not self._responses:
                raise LLMRequiredError("FakeProvider response queue exhausted.")
            return self._responses.pop(0)
        return "ok"
```

- [ ] **Step 5: 运行，确认通过**
Run: `python -m uv run pytest tests/unit/test_fake_provider.py -v` → Expected: PASS。

- [ ] **Step 6: 提交**
```bash
git add src/agentkit/llm/base.py src/agentkit/llm/fake.py tests/unit/test_fake_provider.py
git commit -m "feat: add LLMProvider protocol and FakeProvider"
```

---

### Task 3: Cisco/OpenAI provider 重构 + 工厂

**Files:**
- Modify: `src/agentkit/llm/cisco.py`（重构为 `CiscoCircuitProvider`，移除 import 时读 env/raise/建 model）
- Create: `src/agentkit/llm/openai_compatible.py`
- Create: `src/agentkit/llm/factory.py`
- Test: `tests/unit/test_factory.py`

**Interfaces:**
- Consumes: `agentkit.config.Settings`；`agentkit.llm.base.{LLMRequiredError, extract_text}`.
- Produces: `agentkit.llm.cisco.CiscoCircuitProvider(*, client_id, client_secret, app_key, timeout_seconds=30.0)`；`agentkit.llm.openai_compatible.OpenAICompatibleProvider(*, base_url, api_key, model, api_version=None, timeout_seconds=30.0)`；`agentkit.llm.factory.build_provider(settings) -> LLMProvider`.

- [ ] **Step 1: 写失败测试 `tests/unit/test_factory.py`**
```python
import pytest

from agentkit.config import Settings
from agentkit.llm.base import LLMRequiredError
from agentkit.llm.factory import build_provider


def test_build_fake():
    s = Settings(_env_file=None, llm_provider="fake")
    p = build_provider(s)
    assert p.name == "fake"


def test_build_cisco_missing_creds_raises():
    s = Settings(_env_file=None, llm_provider="cisco")  # no creds
    with pytest.raises(LLMRequiredError):
        build_provider(s)


def test_build_openai_missing_fields_raises():
    s = Settings(_env_file=None, llm_provider="openai")  # no base_url/key/model
    with pytest.raises(LLMRequiredError):
        build_provider(s)


def test_import_cisco_module_is_side_effect_free():
    # Importing the module must NOT read env, raise, or build a model.
    import importlib

    import agentkit.llm.cisco as cisco_mod

    importlib.reload(cisco_mod)
    assert hasattr(cisco_mod, "CiscoCircuitProvider")
    assert not hasattr(cisco_mod, "model")  # module-level eager model removed
```

- [ ] **Step 2: 运行，确认失败**
Run: `python -m uv run pytest tests/unit/test_factory.py -v` → Expected: FAIL。

- [ ] **Step 3: 重构 `src/agentkit/llm/cisco.py`**
保留 `_TokenProvider`、`_ApiKeyAuth`、`CircuitAuth`、`SignatureAwareAzureChatOpenAI`、模块级常量（`CIRCUIT_ENDPOINT`/`API_VERSION`/`DEPLOYMENT`/`TOKEN_URL`）与 `rate_limiter`（这些是纯定义，import 安全）。**删除**：模块级 `load_dotenv()`、`CISCO_* = os.getenv(...)`、缺失即 `raise` 的块、模块级 `auth`、模块级 `model`、注释掉的 DeepSeek 块。然后在文件末尾加：
```python
class CiscoCircuitProvider:
    name = "cisco"

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        app_key: str | None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not all([client_id, client_secret, app_key]):
            raise LLMRequiredError(
                "Cisco Circuit credentials missing: set CISCO_CLIENT_ID, "
                "CISCO_CLIENT_SECRET, and CISCO_APP_KEY."
            )
        auth = CircuitAuth(client_id, client_secret)
        self._model = SignatureAwareAzureChatOpenAI(
            azure_endpoint=CIRCUIT_ENDPOINT,
            api_version=API_VERSION,
            api_key="x",  # placeholder; real auth injected via http_client api-key header
            http_client=auth.httpx_client(timeout=timeout_seconds),
            deployment_name=DEPLOYMENT,
            extra_body={"user": json.dumps({"appkey": app_key})},
            rate_limiter=rate_limiter,
        ).bind(parallel_tool_calls=False)

    def complete(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = self._model.invoke([SystemMessage(system), HumanMessage(user)])
        return extract_text(response)
```
并把顶部 import 调整为：保留 `base64, json, threading, time, httpx`，`from langchain_core.messages import AIMessage`，`from langchain_core.rate_limiters import InMemoryRateLimiter`，`from langchain_openai import AzureChatOpenAI`；新增 `from agentkit.llm.base import LLMRequiredError, extract_text`；删除 `import os`、`from dotenv import load_dotenv`（若不再使用）。更新模块 docstring（去掉「import model 即用」「缺凭证 raise」描述，改为「构造 CiscoCircuitProvider 使用」）。

- [ ] **Step 4: 实现 `src/agentkit/llm/openai_compatible.py`**
```python
"""Generic OpenAI-compatible provider (OpenAI / Azure-OpenAI-compatible / DeepSeek / local vLLM)."""

from __future__ import annotations

from agentkit.llm.base import LLMRequiredError, extract_text


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        api_version: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not all([base_url, api_key, model]):
            raise LLMRequiredError(
                "OpenAI-compatible provider needs AGENTKIT_OPENAI_BASE_URL, "
                "AGENTKIT_OPENAI_API_KEY, and AGENTKIT_OPENAI_MODEL."
            )
        from langchain_openai import ChatOpenAI

        self._model = ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout_seconds,
        )

    def complete(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = self._model.invoke([SystemMessage(system), HumanMessage(user)])
        return extract_text(response)
```

- [ ] **Step 5: 实现 `src/agentkit/llm/factory.py`**
```python
"""Build the configured LLM provider."""

from __future__ import annotations

from agentkit.config import Settings
from agentkit.llm.base import LLMProvider, LLMRequiredError


def build_provider(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider
    if provider == "cisco":
        from agentkit.llm.cisco import CiscoCircuitProvider

        return CiscoCircuitProvider(
            client_id=settings.cisco_client_id,
            client_secret=settings.cisco_client_secret,
            app_key=settings.cisco_app_key,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if provider == "openai":
        from agentkit.llm.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            api_version=settings.openai_api_version,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if provider == "fake":
        from agentkit.llm.fake import FakeProvider

        return FakeProvider()
    raise LLMRequiredError(f"Unknown llm_provider: {provider}")
```

- [ ] **Step 6: 运行，确认通过**
Run: `python -m uv run pytest tests/unit/test_factory.py -v` → Expected: PASS（含 import-side-effect-free 测试）。

- [ ] **Step 7: 提交**
```bash
git add src/agentkit/llm/cisco.py src/agentkit/llm/openai_compatible.py src/agentkit/llm/factory.py tests/unit/test_factory.py
git commit -m "refactor: provider classes (cisco/openai) + factory; cisco import side-effect free"
```

---

### Task 4: `core/llm_client.py` 走 provider + 重试（对外 API 不变）

**Files:**
- Modify: `src/agentkit/core/llm_client.py`
- Test: `tests/unit/test_llm_client_retry.py`

**Interfaces:**
- Consumes: `agentkit.config.get_settings`；`agentkit.llm.factory.build_provider`；`agentkit.llm.base.LLMRequiredError`.
- Produces (unchanged signatures): `require_chat(system, user) -> str`、`require_chat_json(system, user) -> dict`、`chat`、`chat_json`、`LLMRequiredError`、`llm_available() -> bool`、`require_model`（保留兼容：返回 provider）。新增 `_get_provider()`（@lru_cache）。

- [ ] **Step 1: 写失败测试 `tests/unit/test_llm_client_retry.py`**
```python
import pytest

import agentkit.core.llm_client as llm_client
from agentkit.llm.base import LLMRequiredError


class _FlakyProvider:
    name = "flaky"

    def __init__(self, fail_times, then="hi"):
        self._fail_times = fail_times
        self._then = then
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("boom")
        return self._then


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    # No real sleeping during retry tests.
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)
    # Small deterministic retry budget.
    from agentkit.config import Settings

    monkeypatch.setattr(
        llm_client, "_get_settings_for_retry", lambda: Settings(_env_file=None, llm_max_retries=2)
    ) if hasattr(llm_client, "_get_settings_for_retry") else None
    yield


def test_retry_succeeds_after_failures(monkeypatch):
    prov = _FlakyProvider(fail_times=2)
    monkeypatch.setattr(llm_client, "_get_provider", lambda: prov)
    from agentkit.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("agentkit.config.get_settings", lambda: Settings(_env_file=None, llm_max_retries=2))
    assert llm_client.require_chat("s", "u") == "hi"
    assert prov.calls == 3


def test_retry_exhausted_raises(monkeypatch):
    prov = _FlakyProvider(fail_times=5)
    monkeypatch.setattr(llm_client, "_get_provider", lambda: prov)
    from agentkit.config import Settings

    monkeypatch.setattr("agentkit.config.get_settings", lambda: Settings(_env_file=None, llm_max_retries=1))
    with pytest.raises(LLMRequiredError):
        llm_client.require_chat("s", "u")


def test_require_chat_json_parses(monkeypatch):
    prov = _FlakyProvider(fail_times=0, then='{"a": 1}')
    monkeypatch.setattr(llm_client, "_get_provider", lambda: prov)
    from agentkit.config import Settings

    monkeypatch.setattr("agentkit.config.get_settings", lambda: Settings(_env_file=None, llm_max_retries=0))
    assert llm_client.require_chat_json("s", "u") == {"a": 1}
```

- [ ] **Step 2: 运行，确认失败**
Run: `python -m uv run pytest tests/unit/test_llm_client_retry.py -v` → Expected: FAIL（当前 llm_client 还在用 `_load_model`）。

- [ ] **Step 3: 重写 `src/agentkit/core/llm_client.py`**
```python
"""LLM client helpers around the configured provider (see agentkit.llm.factory).

``chat`` / ``chat_json`` keep the older optional behavior (return None on failure).
The runtime's agent path uses ``require_chat`` / ``require_chat_json``: they fail
loudly (LLMRequiredError) when the provider is unavailable, the call keeps failing
after retries, or the response cannot be parsed.
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any

from agentkit.llm.base import LLMRequiredError

from .logging_config import get_logger

_log = get_logger("agentkit.llm")

__all__ = [
    "LLMRequiredError",
    "llm_available",
    "require_model",
    "require_chat",
    "require_chat_json",
    "chat",
    "chat_json",
]


@lru_cache(maxsize=1)
def _get_provider():
    from agentkit.config import get_settings
    from agentkit.llm.factory import build_provider

    return build_provider(get_settings())


def llm_available() -> bool:
    try:
        _get_provider()
        return True
    except Exception:
        return False


def require_model():
    """Back-compat shim: return the configured provider (raises if unavailable)."""
    try:
        return _get_provider()
    except LLMRequiredError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to the runtime's error type
        raise LLMRequiredError(f"LLM provider unavailable: {exc}") from exc


def chat(system: str, user: str) -> str | None:
    try:
        return require_chat(system, user)
    except LLMRequiredError:
        return None


def require_chat(system: str, user: str) -> str:
    from agentkit.config import get_settings

    settings = get_settings()
    provider = require_model()
    last_exc: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            text = provider.complete(system, user)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _log.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)
            if attempt < settings.llm_max_retries:
                time.sleep(settings.llm_retry_base_delay * (2**attempt))
                continue
            raise LLMRequiredError(f"LLM call failed after retries: {exc}") from exc
        if not text:
            raise LLMRequiredError("LLM returned an empty response.")
        return str(text).strip()
    raise LLMRequiredError(f"LLM call failed: {last_exc}")


def chat_json(system: str, user: str) -> dict[str, Any] | None:
    try:
        return require_chat_json(system, user)
    except LLMRequiredError:
        return None


def require_chat_json(system: str, user: str) -> dict[str, Any]:
    raw = require_chat(system, user)
    data = _extract_json(raw)
    if data is None:
        raise LLMRequiredError(f"LLM did not return a valid JSON object: {raw[:500]}")
    return data


def _extract_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
```
注意：测试用 `monkeypatch.setattr(llm_client, "_get_provider", ...)`；`require_chat` 通过 `require_model()`→`_get_provider()` 全局名查找，monkeypatch 生效。`time` 模块被 import，测试可 patch `llm_client.time.sleep`。

- [ ] **Step 4: 运行目标测试 + 既有全套**
Run: `python -m uv run pytest tests/unit/test_llm_client_retry.py -v` → Expected: PASS。
Run: `python -m uv run pytest -q` → Expected: 既有 18 + 新增全绿。
Run: `python -m uv run python -c "import agentkit.core.llm_client, agentkit.config, agentkit.llm.cisco; print('import ok')"` → Expected: `import ok`（无凭证、无网络、无 raise）。

- [ ] **Step 5: 提交**
```bash
git add src/agentkit/core/llm_client.py tests/unit/test_llm_client_retry.py
git commit -m "refactor: route llm_client through configured provider with retry"
```

---

### Task 5: 整图集成测试（FakeProvider，补 Phase 0 欠的）

**Files:**
- Test: `tests/integration/test_graph_with_fake_provider.py`

**Interfaces:**
- Consumes: `agentkit.runtime.bootstrap.build_runtime`；`agentkit.core.contracts.TaskRequest`；通过 monkeypatch `agentkit.core.llm_client._get_provider` 注入脚本化 `FakeProvider`。

- [ ] **Step 1: 写测试 `tests/integration/test_graph_with_fake_provider.py`**
```python
import json

import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


def _hr_responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "rank candidates",
                "target": {"kind": "none", "name": ""},
                "entities": {},
                "confidence": "high",
                "signals": [],
            }
        )
    if "routing node" in s:
        return json.dumps({"skill_name": "candidate.rank", "reason": "match", "confidence": "high"})
    if "planning node" in s:
        return json.dumps(
            {
                "steps": [
                    {"step_id": 1, "skill_name": "candidate.rank", "mode": "plan_execute", "depends_on": []}
                ],
                "warnings": [],
            }
        )
    if "plan-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "approval-governance node" in s:
        return json.dumps(
            {"risk_level": "low", "approval_summary": "ok", "concerns": [], "recommended_status": "approved"}
        )
    if "output-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "recruiting assistant" in s:
        return "Recommended hire: top candidate."
    return "ok"


def test_full_graph_hr_execute_with_fake(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_hr_responder))
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
    request = TaskRequest(
        user_id="u-1",
        roles=["recruiter"],
        text="Rank the top candidate for JOB-001.",
        context={
            "agent": "hr_recruiter",
            "job_id": "JOB-001",
            "candidate_ids": ["C-100"],
            "top_n": 1,
            "approved_skills": ["candidate.rank"],
        },
    )
    response = runtime.gateway.handle(request)
    out = response.to_dict()

    assert "governance" in out["output"]
    gov = out["output"]["governance"]
    assert gov["approval"]["status"] == "approved"
    assert gov["output_review"]["status"] in {"approved", "approved_with_warnings"}
    final = out["output"].get("final", {})
    ranked = final.get("ranked_candidates") or out["output"].get("ranked_candidates")
    assert ranked and ranked[0]["candidate_id"] == "C-100"


def _chitchat_responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return json.dumps(
            {
                "intent_type": "chit_chat",
                "goal": "respond conversationally",
                "target": {"kind": "platform_handler", "name": "default"},
                "entities": {},
                "confidence": "low",
                "signals": [],
            }
        )
    if "routing node" in s:
        return json.dumps({"skill_name": None, "reason": "chit-chat", "confidence": "low"})
    if "plan-review node" in s:
        return json.dumps({"status": "skipped", "reason": "no skill", "findings": []})
    if "approval-governance node" in s:
        return json.dumps(
            {"risk_level": "low", "approval_summary": "n/a", "concerns": [], "recommended_status": "approved"}
        )
    if "output-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    # conversation fallback _llm_reply
    return "Hello! How can I help?"


def test_full_graph_chitchat_with_fake(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_chitchat_responder))
    runtime = build_runtime(db_path=tmp_path / "audit2.sqlite")
    request = TaskRequest(user_id="u-2", roles=[], text="hello there")
    response = runtime.gateway.handle(request)
    out = response.to_dict()
    assert out["output"]["final"]["conversation"] is True
```

- [ ] **Step 2: 运行**
Run: `python -m uv run pytest tests/integration/test_graph_with_fake_provider.py -v`
Expected: PASS。若某节点断言因 prompt 文案不含预期子串而失败，读取对应 `core/*.py` 的 `_llm_system_prompt`，把 `_hr_responder`/`_chitchat_responder` 里的匹配子串调成该节点 system prompt 中稳定存在的片段（如 intent 用 "intent decomposition module"、router 用 "routing node"、planner 用 "planning node"、governance 三个分别用 "plan-review node"/"approval-governance node"/"output-review node"、hr 摘要用 "recruiting assistant"）。这些子串来自各节点现有 system prompt，应直接命中。

- [ ] **Step 3: 提交**
```bash
git add tests/integration/test_graph_with_fake_provider.py
git commit -m "test: full LangGraph integration test via FakeProvider (no network)"
```

---

### Task 6: 最终校验 + 文档

**Files:**
- Modify: `README.md`（新增 provider/配置说明）
- Modify: `docs/superpowers/...`（无需）

**Interfaces:** 无新接口。

- [ ] **Step 1: 全门禁**
Run（全部需通过）:
```
python -m uv run ruff check .
python -m uv run ruff format --check .
python -m uv run pytest -q
python -m uv run python -c "import agentkit.config, agentkit.llm.cisco, agentkit.llm.factory; print('import ok')"
```
如 `ruff check` 报问题，`python -m uv run ruff check . --fix` 后再 `ruff format .`，确保行为不变（测试仍绿）。

- [ ] **Step 2: README 增补配置段**
在 README 安装/运行段之后追加一节（中文）：
```markdown
## LLM Provider 配置

通过环境变量选择后端（默认 `cisco`）：

\`\`\`bash
# Cisco Circuit（默认；用现有 .env 的 CISCO_CLIENT_ID/SECRET/APP_KEY）
AGENTKIT_LLM_PROVIDER=cisco

# OpenAI 兼容（OpenAI / DeepSeek / 本地 vLLM 等）
AGENTKIT_LLM_PROVIDER=openai
AGENTKIT_OPENAI_BASE_URL=https://api.openai.com/v1
AGENTKIT_OPENAI_API_KEY=sk-...
AGENTKIT_OPENAI_MODEL=gpt-4o-mini

# 测试用假后端（不发网络）
AGENTKIT_LLM_PROVIDER=fake
\`\`\`

其它可调项：`AGENTKIT_LLM_MAX_RETRIES`（默认 2）、`AGENTKIT_LLM_TIMEOUT_SECONDS`（默认 30）、`AGENTKIT_LLM_RETRY_BASE_DELAY`（默认 0.5）。
\`\`\`
```

- [ ] **Step 3: 提交**
```bash
git add README.md
git commit -m "docs: document LLM provider/config env vars"
```

---

## Self-Review

**Spec coverage（对照 Phase 1a spec §2/§4）：**
- provider 协议/窄接口 → Task 2。
- 类型化配置（pydantic-settings、CISCO_* 别名、import 安全）→ Task 1。
- Cisco 重构（去 import 崩）+ OpenAI 兼容 + 工厂 → Task 3。
- llm_client 走 provider + 重试 + API 不变 → Task 4。
- FakeProvider + 整图集成测试 → Task 2/Task 5。
- 验收门禁 + 文档 → Task 6。

**Placeholder 扫描：** 无 TBD；每步含完整代码与命令/预期。Task 5 Step 2 含「文案不命中则调子串」的兜底说明（不是占位，而是对照现有 prompt 的对齐指引）。

**类型一致性：** `LLMProvider.complete(system,user)->str`、`build_provider(settings)->LLMProvider`、`get_settings()->Settings`、`_get_provider()`、`require_chat/require_chat_json` 在各任务间一致；`LLMRequiredError` 统一来自 `agentkit.llm.base` 并由 `core.llm_client` re-export。

**已知风险：** Task 4 测试里对 `get_settings` 的 monkeypatch 需 patch 到 `agentkit.config.get_settings`（require_chat 内部 `from agentkit.config import get_settings` 局部导入，按模块属性解析，patch 模块属性生效）；测试已用 `monkeypatch.setattr("agentkit.config.get_settings", ...)`。

## Execution Handoff

完成后用 superpowers:finishing-a-development-branch 收尾。实现前用 superpowers:using-git-worktrees 建隔离 worktree（基于当前 `main`）。两种执行方式：
1. **Subagent-Driven（推荐）**：每任务派新 subagent + 两阶段 review。
2. **Inline**：executing-plans 批量执行 + 检查点。
