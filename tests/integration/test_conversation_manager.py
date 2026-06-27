import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.memory.context_builder import ContextBuilder
from agentkit.core.memory.manager import ConversationManager
from agentkit.core.memory.store import ConversationStore
from agentkit.core.memory.summarizer import Summarizer
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator


def _manager(tmp_path, *, budget=10_000, window=6, replies=None):
    store = ConversationStore(tmp_path / "tenant.sqlite")
    tokenizer = HeuristicTokenEstimator()
    builder = ContextBuilder(
        tokenizer=tokenizer,
        budget_tokens=budget,
        window_turns=window,
        summary_cap_tokens=10_000,
    )

    seq = list(replies or [])

    def chat_fn(system, user):
        if seq:
            return seq.pop(0)
        return f"reply-to:{user}"

    summarizer = Summarizer(chat_fn=lambda system, user: "SUMMARY")
    audit = InMemoryAuditLog()
    manager = ConversationManager(
        store=store,
        builder=builder,
        summarizer=summarizer,
        tokenizer=tokenizer,
        chat_fn=chat_fn,
        audit=audit,
    )
    return manager, store, audit


def test_first_turn_persists_user_and_assistant(tmp_path):
    manager, store, audit = _manager(tmp_path)
    reply = manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="hello there")
    assert reply.reply == "reply-to:hello there"
    assert reply.conversation_id
    msgs = store.all_messages(reply.conversation_id)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello there"
    assert msgs[1]["content"] == "reply-to:hello there"
    # auto-created conversation got a title from the text
    conv = store.get_conversation(reply.conversation_id)
    assert conv["title"] == "hello there"
    # audit recorded the turn
    events = audit.events_for(reply.run_id)
    assert any(e["type"] == "conversation_message" for e in events)


def test_second_turn_includes_prior_history(tmp_path):
    captured = {}

    store = ConversationStore(tmp_path / "tenant.sqlite")
    tokenizer = HeuristicTokenEstimator()
    builder = ContextBuilder(tokenizer=tokenizer, budget_tokens=10_000, window_turns=6)

    def chat_fn(system, user):
        captured["system"] = system
        return "ok"

    manager = ConversationManager(
        store=store,
        builder=builder,
        summarizer=Summarizer(chat_fn=lambda s, u: "SUM"),
        tokenizer=tokenizer,
        chat_fn=chat_fn,
    )

    first = manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="my name is Sam")
    manager.chat(
        tenant_id="t1",
        agent="cs",
        user_id="u1",
        text="what is my name?",
        conversation_id=first.conversation_id,
    )
    # The second turn's system prompt must contain the earlier exchange.
    assert "my name is Sam" in captured["system"]


def test_unknown_conversation_id_raises(tmp_path):
    manager, _store, _audit = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.chat(tenant_id="t1", agent="cs", user_id="u1", text="x", conversation_id="missing")


def test_tiny_budget_triggers_summary_persisted(tmp_path):
    # Force folding by using a very small budget over several turns.
    manager, store, _audit = _manager(tmp_path, budget=40, window=6)
    cid = None
    for i in range(6):
        reply = manager.chat(
            tenant_id="t1",
            agent="cs",
            user_id="u1",
            text=f"this is a reasonably long message number {i} with content",
            conversation_id=cid,
        )
        cid = reply.conversation_id
    summary = store.get_summary(cid)
    assert summary is not None
    assert summary["summary_text"] == "SUMMARY"
    # full transcript is still retained (all turns persisted)
    assert store.count_messages(cid) == 12
