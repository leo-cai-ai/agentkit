import pytest

import agentkit.core.llm_client as llm_client
from agentkit.config import Settings
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
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)


def _use(monkeypatch, provider, *, max_retries):
    monkeypatch.setattr(llm_client, "_get_provider", lambda: provider)
    monkeypatch.setattr(
        "agentkit.config.get_settings",
        lambda: Settings(_env_file=None, llm_max_retries=max_retries),
    )


def test_retry_succeeds_after_failures(monkeypatch):
    prov = _FlakyProvider(fail_times=2)
    _use(monkeypatch, prov, max_retries=2)
    assert llm_client.require_chat("s", "u") == "hi"
    assert prov.calls == 3


def test_retry_exhausted_raises(monkeypatch):
    prov = _FlakyProvider(fail_times=5)
    _use(monkeypatch, prov, max_retries=1)
    with pytest.raises(LLMRequiredError):
        llm_client.require_chat("s", "u")
    assert prov.calls == 2


def test_require_chat_json_parses(monkeypatch):
    prov = _FlakyProvider(fail_times=0, then='{"a": 1}')
    _use(monkeypatch, prov, max_retries=0)
    assert llm_client.require_chat_json("s", "u") == {"a": 1}


def test_require_chat_json_ignores_think_block(monkeypatch):
    # Reasoning models prepend a <think> block whose prose contains braces;
    # it must be stripped before the JSON is parsed.
    reply = (
        "<think>The user wants a status. I'll return {\"status\": \"ok\"}.</think>\n"
        '{"status": "approved"}'
    )
    prov = _FlakyProvider(fail_times=0, then=reply)
    _use(monkeypatch, prov, max_retries=0)
    assert llm_client.require_chat_json("s", "u") == {"status": "approved"}


def test_extract_json_handles_think_and_fence():
    raw = "<think>reasoning with {braces}</think>\n```json\n{\"a\": 2}\n```"
    assert llm_client._extract_json(raw) == {"a": 2}


def test_extract_json_scans_first_valid_object_without_greedy_capture():
    raw = 'prefix {"a": 1} trailing {"b": 2}'
    assert llm_client._extract_json(raw) == {"a": 1}


def test_truncated_think_yields_friendly_message(monkeypatch):
    # Reasoning ran out of budget mid-thought: unclosed <think>, no JSON.
    prov = _FlakyProvider(fail_times=0, then="<think>let me work through {the data")
    _use(monkeypatch, prov, max_retries=0)
    with pytest.raises(LLMRequiredError) as exc:
        llm_client.require_chat_json("s", "u")
    assert str(exc.value) == llm_client.TRUNCATED_RESPONSE_MESSAGE


def test_truncated_json_object_yields_friendly_message(monkeypatch):
    # JSON started but was cut off (unbalanced braces), no think tags.
    prov = _FlakyProvider(fail_times=0, then='{"status": "appr')
    _use(monkeypatch, prov, max_retries=0)
    with pytest.raises(LLMRequiredError) as exc:
        llm_client.require_chat_json("s", "u")
    assert str(exc.value) == llm_client.TRUNCATED_RESPONSE_MESSAGE


def test_non_truncated_garbage_keeps_diagnostic(monkeypatch):
    # Not truncated, just not JSON -> keep the diagnostic (not the fallback).
    prov = _FlakyProvider(fail_times=0, then="sorry, I cannot do that")
    _use(monkeypatch, prov, max_retries=0)
    with pytest.raises(LLMRequiredError) as exc:
        llm_client.require_chat_json("s", "u")
    assert "did not return a valid JSON object" in str(exc.value)


def test_empty_response_raises_without_retry(monkeypatch):
    prov = _FlakyProvider(fail_times=0, then="")
    _use(monkeypatch, prov, max_retries=3)
    with pytest.raises(LLMRequiredError):
        llm_client.require_chat("s", "u")
    assert prov.calls == 1  # empty is not retried


def test_chat_returns_none_on_failure(monkeypatch):
    prov = _FlakyProvider(fail_times=5)
    _use(monkeypatch, prov, max_retries=0)
    assert llm_client.chat("s", "u") is None
