import pytest

from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.memory.retrieval import MemoryRetriever, cosine
from agentkit.core.memory.store import ConversationStore


@pytest.fixture()
def retriever(tmp_path):
    store = ConversationStore(tmp_path / "t.sqlite")
    return MemoryRetriever(
        store=store,
        embeddings=FakeEmbeddingProvider(dim=128),
        min_score=0.05,
        dedup_threshold=0.95,
    )


def test_cosine_zero_vector():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_retrieve_finds_relevant_memory(retriever):
    retriever.remember(
        tenant_id="t1",
        agent="cs",
        user_id="u1",
        texts=["the user's name is Sam", "the user lives in Tokyo"],
    )
    hits = retriever.retrieve(
        tenant_id="t1", agent="cs", user_id="u1", query="what is my name", k=1
    )
    assert hits == ["the user's name is Sam"]


def test_retrieve_empty_when_no_memories(retriever):
    assert retriever.retrieve(tenant_id="t1", agent="cs", user_id="u1", query="x", k=3) == []


def test_retrieve_zero_k(retriever):
    retriever.remember(tenant_id="t1", agent="cs", user_id="u1", texts=["fact"])
    assert retriever.retrieve(tenant_id="t1", agent="cs", user_id="u1", query="fact", k=0) == []


def test_remember_dedups_near_duplicates(retriever):
    first = retriever.remember(
        tenant_id="t1", agent="cs", user_id="u1", texts=["the user's name is Sam"]
    )
    assert len(first) == 1
    again = retriever.remember(
        tenant_id="t1", agent="cs", user_id="u1", texts=["the user's name is Sam"]
    )
    assert again == []  # exact duplicate skipped


def test_remember_skips_blank(retriever):
    assert retriever.remember(tenant_id="t1", agent="cs", user_id="u1", texts=["  ", ""]) == []
