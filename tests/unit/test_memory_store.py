import time

import pytest

from agentkit.core.memory.store import ConversationStore


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(tmp_path / "tenant.sqlite")


def test_create_and_get_conversation(store):
    cid = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1", title="hi")
    conv = store.get_conversation(cid)
    assert conv is not None
    assert conv["tenant_id"] == "t1"
    assert conv["agent"] == "cs"
    assert conv["user_id"] == "u1"
    assert conv["title"] == "hi"
    assert conv["status"] == "active"


def test_get_missing_conversation_returns_none(store):
    assert store.get_conversation("nope") is None


def test_add_message_updates_conversation_updated_at(store):
    cid = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1")
    before = store.get_conversation(cid)["updated_at"]
    time.sleep(0.01)
    store.add_message(conversation_id=cid, role="user", content="hello", token_estimate=2)
    after = store.get_conversation(cid)["updated_at"]
    assert after >= before
    assert store.count_messages(cid) == 1


def test_recent_messages_returns_last_n_chronological(store):
    cid = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1")
    for i in range(5):
        store.add_message(conversation_id=cid, role="user", content=f"m{i}")
    recent = store.recent_messages(conversation_id=cid, limit=3)
    assert [m["content"] for m in recent] == ["m2", "m3", "m4"]


def test_recent_messages_zero_limit(store):
    cid = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1")
    store.add_message(conversation_id=cid, role="user", content="x")
    assert store.recent_messages(conversation_id=cid, limit=0) == []


def test_list_conversations_scoped_and_ordered(store):
    a = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1")
    time.sleep(0.01)
    b = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1")
    # different agent / user / tenant must not leak
    store.create_conversation(tenant_id="t1", agent="other", user_id="u1")
    store.create_conversation(tenant_id="t1", agent="cs", user_id="u2")
    store.create_conversation(tenant_id="t2", agent="cs", user_id="u1")

    convs = store.list_conversations(tenant_id="t1", agent="cs", user_id="u1")
    ids = [c["id"] for c in convs]
    assert ids == [b, a]  # most recently updated first


def test_summary_upsert_overwrites(store):
    cid = store.create_conversation(tenant_id="t1", agent="cs", user_id="u1")
    assert store.get_summary(cid) is None
    store.upsert_summary(
        conversation_id=cid, summary_text="v1", covered_through_message_id=3, token_estimate=10
    )
    store.upsert_summary(
        conversation_id=cid, summary_text="v2", covered_through_message_id=7, token_estimate=20
    )
    summary = store.get_summary(cid)
    assert summary["summary_text"] == "v2"
    assert summary["covered_through_message_id"] == 7
    assert summary["token_estimate"] == 20


def test_add_and_iter_memories_roundtrip(store):
    mid = store.add_memory(
        tenant_id="t1",
        agent="cs",
        user_id="u1",
        text="user prefers email",
        embedding=[0.1, 0.2, 0.3],
        kind="preference",
    )
    rows = store.iter_memories(tenant_id="t1", agent="cs", user_id="u1")
    assert len(rows) == 1
    assert rows[0]["id"] == mid
    assert rows[0]["text"] == "user prefers email"
    assert rows[0]["kind"] == "preference"
    assert rows[0]["dim"] == 3
    # float32 round-trip is approximate
    for got, want in zip(rows[0]["embedding"], [0.1, 0.2, 0.3], strict=True):
        assert abs(got - want) < 1e-6


def test_memories_scoped_isolation(store):
    store.add_memory(tenant_id="t1", agent="cs", user_id="u1", text="a", embedding=[1.0, 0.0])
    store.add_memory(tenant_id="t1", agent="cs", user_id="u2", text="b", embedding=[0.0, 1.0])
    store.add_memory(tenant_id="t1", agent="other", user_id="u1", text="c", embedding=[0.0, 1.0])
    rows = store.iter_memories(tenant_id="t1", agent="cs", user_id="u1")
    assert [r["text"] for r in rows] == ["a"]
