"""End-to-end semantic memory: extract on one turn, recall in a new conversation."""

from __future__ import annotations

from agentkit.core.memory.context_builder import ContextBuilder
from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.manager import ConversationManager
from agentkit.core.memory.retrieval import MemoryRetriever
from agentkit.core.memory.store import ConversationStore
from agentkit.core.memory.summarizer import Summarizer
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator


def _build(tmp_path, *, captured, extractor_facts):
    store = ConversationStore(tmp_path / "t.sqlite")
    tokenizer = HeuristicTokenEstimator()
    builder = ContextBuilder(tokenizer=tokenizer, budget_tokens=10_000, window_turns=6)
    retriever = MemoryRetriever(
        store=store,
        embeddings=FakeEmbeddingProvider(dim=128),
        min_score=0.05,
    )

    def chat_fn(system, user):
        captured["system"] = system
        return "assistant reply"

    extractor = MemoryExtractor(chat_fn=lambda s, u: extractor_facts)
    manager = ConversationManager(
        store=store,
        builder=builder,
        summarizer=Summarizer(chat_fn=lambda s, u: "SUM"),
        tokenizer=tokenizer,
        chat_fn=chat_fn,
        retriever=retriever,
        extractor=extractor,
        retrieval_k=3,
        extract_every_n_turns=1,  # extract every turn for the test
    )
    return manager, store


def test_extracted_fact_recalled_in_new_conversation(tmp_path):
    captured: dict = {}
    manager, _store = _build(
        tmp_path,
        captured=captured,
        extractor_facts='["the user\'s name is Sam"]',
    )

    # Turn 1 in conversation A -> extraction stores the fact.
    manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="Hi, my name is Sam")

    # New conversation, same user, asks about the remembered fact.
    reply = manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="what is my name?")
    assert reply.debug["retrieved_memories"] >= 1
    assert "the user's name is Sam" in captured["system"]


def test_retrieval_scoped_per_user(tmp_path):
    captured: dict = {}
    manager, _store = _build(
        tmp_path,
        captured=captured,
        extractor_facts='["the user\'s name is Sam"]',
    )
    manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="my name is Sam")

    # Different user must not see u1's memory.
    reply = manager.chat(tenant_id="t1", agent="cs", user_id="u2", text="what is my name?")
    assert reply.debug["retrieved_memories"] == 0


def test_extraction_failure_does_not_break_turn(tmp_path):
    store = ConversationStore(tmp_path / "t.sqlite")
    tokenizer = HeuristicTokenEstimator()
    builder = ContextBuilder(tokenizer=tokenizer, budget_tokens=10_000, window_turns=6)
    retriever = MemoryRetriever(store=store, embeddings=FakeEmbeddingProvider())

    def boom_extract(*, user_text, assistant_text):
        raise RuntimeError("extractor exploded")

    class BoomExtractor:
        extract = staticmethod(boom_extract)

    manager = ConversationManager(
        store=store,
        builder=builder,
        summarizer=Summarizer(chat_fn=lambda s, u: "SUM"),
        tokenizer=tokenizer,
        chat_fn=lambda s, u: "ok",
        retriever=retriever,
        extractor=BoomExtractor(),
        extract_every_n_turns=1,
    )
    reply = manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="hello")
    assert reply.reply == "ok"  # turn still succeeds despite extractor failure
