from __future__ import annotations

from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.rag import (
    AdaptiveTextChunker,
    ChunkingOptions,
    DocumentFolderLoader,
    HybridRetriever,
    InMemoryKnowledgeStore,
    KeywordRetriever,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionPipeline,
    RAGEvalCase,
    RetrievalHit,
    RetrievalQuery,
    VectorRetriever,
    evaluate_retriever,
)
from agentkit.core.rag.retrieval import LLMQueryRewriter, LLMReranker
from tests.context_support import SpyContextInvoker


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
        RetrievalQuery(
            tenant_id="t1",
            tenant_selector="company_alpha",
            run_id="r1",
            text="refund approval",
            roles=("support",),
            k=3,
        )
    )
    denied = retriever.retrieve(
        RetrievalQuery(
            tenant_id="t1",
            tenant_selector="company_alpha",
            run_id="r1",
            text="refund approval",
            roles=("sales",),
            k=3,
        )
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

    query = RetrievalQuery(
        tenant_id="t1",
        tenant_selector="company_alpha",
        run_id="r1",
        text="candidate ranking skills",
        k=2,
    )
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


def test_adaptive_chunker_preserves_block_provenance():
    doc = KnowledgeDocument(
        id="doc-1",
        tenant_id="t1",
        title="Handbook",
        text="",
        metadata={
            "blocks": [
                {"text": "Refund workflow overview", "kind": "page_text", "page": 1},
                {"text": "SLA | Owner\n24h | Support", "kind": "table", "page": 2},
            ],
            "source_path": "handbook.pdf",
        },
        acl_roles=("support",),
    )
    chunks = AdaptiveTextChunker(ChunkingOptions(max_chars=120)).chunk(doc)

    assert len(chunks) == 2
    assert chunks[0].metadata["pages"] == [1]
    assert chunks[1].metadata["content_kinds"] == ["table"]
    assert "blocks" not in chunks[0].metadata
    assert chunks[1].acl_roles == ("support",)


def test_folder_loader_loads_plain_text(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.txt").write_text("Refunds need approval.", encoding="utf-8")

    docs = DocumentFolderLoader().load_path(docs_dir, tenant_id="t1")

    assert len(docs) == 1
    assert docs[0].title == "policy"
    assert docs[0].text == "Refunds need approval."
    assert docs[0].metadata["blocks"][0]["source"] == "txt"


def test_rag_eval_reports_hit_rate_and_mrr():
    store = InMemoryKnowledgeStore()
    pipeline = KnowledgeIngestionPipeline(
        store=store,
        embeddings=FakeEmbeddingProvider(dim=32),
    )
    pipeline.ingest(
        [
            KnowledgeDocument(
                id="refund-policy",
                tenant_id="t1",
                title="Refund Policy",
                text="Refund approval requires the support manager.",
            )
        ]
    )
    retriever = KeywordRetriever(store=store)

    report = evaluate_retriever(
        [
            RAGEvalCase(
                tenant_id="t1",
                query="refund approval",
                relevant_document_ids=("refund-policy",),
                k=3,
            )
        ],
        retriever=retriever,
        default_tenant_id="t1",
    )

    assert report.hit_rate == 1.0
    assert report.mrr == 1.0


def test_query_rewriter_uses_context_pack_and_falls_back() -> None:
    spy = SpyContextInvoker({"queries": ["退款期限", "退款政策时限"]})
    query = RetrievalQuery(
        tenant_id="t1",
        tenant_selector="company_alpha",
        run_id="r1",
        text="退款期限",
        k=3,
    )

    variants = LLMQueryRewriter(
        context_invoker=spy,
        tenant_selector="company_alpha",
    ).rewrite(query)

    assert variants == ["退款期限", "退款政策时限"]
    assert spy.requests[-1].context_id == "runtime.rag-query-rewrite"


def test_reranker_marks_candidates_untrusted() -> None:
    spy = SpyContextInvoker({"ranked_ids": ["C-1"]})
    query = RetrievalQuery(
        tenant_id="t1",
        tenant_selector="company_alpha",
        run_id="r1",
        text="退款期限",
        k=1,
    )
    hits = [
        RetrievalHit(
            chunk=KnowledgeChunk(
                id="C-1",
                document_id="D-1",
                tenant_id="t1",
                text="七天",
            ),
            score=0.5,
        )
    ]

    result = LLMReranker(
        context_invoker=spy,
        tenant_selector="company_alpha",
    ).rerank(query=query, hits=hits)

    assert result[0].chunk.id == "C-1"
    request = spy.requests[-1]
    assert request.context_id == "runtime.rag-rerank"
    assert "rag.candidates" in request.values
