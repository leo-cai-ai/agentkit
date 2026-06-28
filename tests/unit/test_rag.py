from __future__ import annotations

from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.rag import (
    HybridRetriever,
    InMemoryKnowledgeStore,
    KeywordRetriever,
    KnowledgeDocument,
    KnowledgeIngestionPipeline,
    RetrievalQuery,
    VectorRetriever,
)


def test_ingestion_chunks_embeds_and_keyword_retrieves_with_acl():
    store = InMemoryKnowledgeStore()
    pipeline = KnowledgeIngestionPipeline(
        store=store,
        embeddings=FakeEmbeddingProvider(dim=32),
    )
    pipeline.ingest(
        [
            KnowledgeDocument(
                id="policy-1",
                tenant_id="t1",
                title="Refund Policy",
                text="Refunds require manager approval.\n\nShipping delays use logistics playbook.",
                acl_roles=("support",),
            )
        ]
    )

    retriever = KeywordRetriever(store=store)
    allowed = retriever.retrieve(
        RetrievalQuery(tenant_id="t1", text="refund approval", roles=("support",), k=3)
    )
    denied = retriever.retrieve(
        RetrievalQuery(tenant_id="t1", text="refund approval", roles=("sales",), k=3)
    )

    assert allowed
    assert allowed[0].chunk.document_id == "policy-1"
    assert denied == []


def test_hybrid_retriever_fuses_keyword_and_vector_scores():
    store = InMemoryKnowledgeStore()
    pipeline = KnowledgeIngestionPipeline(
        store=store,
        embeddings=FakeEmbeddingProvider(dim=32),
    )
    pipeline.ingest(
        [
            KnowledgeDocument(
                id="kb-1",
                tenant_id="t1",
                title="ATS ranking",
                text="Candidate ranking uses skills, years of experience, and location.",
            ),
            KnowledgeDocument(
                id="kb-2",
                tenant_id="t1",
                title="Publishing workflow",
                text="XHS publishing creates a draft and waits for approval.",
            ),
        ]
    )

    query = RetrievalQuery(tenant_id="t1", text="candidate ranking skills", k=2)
    hybrid = HybridRetriever(
        retrievers=[
            (KeywordRetriever(store=store), 0.7),
            (VectorRetriever(store=store, embeddings=FakeEmbeddingProvider(dim=32)), 0.3),
        ]
    )

    hits = hybrid.retrieve(query)

    assert hits
    assert hits[0].chunk.document_id == "kb-1"
    assert hits[0].source == "hybrid"
    assert "keyword" in hits[0].diagnostics
