"""Provider streaming + llm_client streaming sink."""

from __future__ import annotations

import agentkit.core.llm_client as llm_client
from agentkit.llm.fake import FakeProvider


def test_fake_provider_stream_chunks_concatenate_to_complete():
    provider = FakeProvider(responses=["Recommended hire: the strongest candidate."])
    chunks = list(provider.stream("sys", "user"))
    assert len(chunks) > 1  # actually chunked, not one shot
    assert "".join(chunks) == "Recommended hire: the strongest candidate."


def test_require_chat_streaming_forwards_chunks_to_sink(monkeypatch):
    provider = FakeProvider(responses=["streaming tokens flow live to the client"])
    monkeypatch.setattr(llm_client, "require_model", lambda: provider)

    collected: list[str] = []
    with llm_client.stream_sink(collected.append):
        text = llm_client.require_chat_streaming("sys", "user")

    assert text == "streaming tokens flow live to the client"
    assert len(collected) > 1
    assert "".join(collected) == text


def test_require_chat_streaming_without_sink_returns_full_text(monkeypatch):
    provider = FakeProvider(responses=["complete answer with no sink bound"])
    monkeypatch.setattr(llm_client, "require_model", lambda: provider)

    text = llm_client.require_chat_streaming("sys", "user")
    assert text == "complete answer with no sink bound"


def test_require_chat_streaming_falls_back_when_provider_cannot_stream(monkeypatch):
    class NoStreamProvider:
        name = "nostream"

        def complete(self, system: str, user: str) -> str:
            return "blocking only reply"

    monkeypatch.setattr(llm_client, "require_model", lambda: NoStreamProvider())

    collected: list[str] = []
    with llm_client.stream_sink(collected.append):
        text = llm_client.require_chat_streaming("sys", "user")

    assert text == "blocking only reply"
    # Fallback emits the whole reply as a single chunk.
    assert collected == ["blocking only reply"]


def test_stream_sink_resets_after_context(monkeypatch):
    provider = FakeProvider(responses=["a", "b"])
    monkeypatch.setattr(llm_client, "require_model", lambda: provider)

    seen: list[str] = []
    with llm_client.stream_sink(seen.append):
        llm_client.require_chat_streaming("s", "u")
    assert seen  # sink received chunks inside the context

    # Outside the context the sink is cleared; no further chunks captured.
    seen.clear()
    llm_client.require_chat_streaming("s", "u")
    assert seen == []
