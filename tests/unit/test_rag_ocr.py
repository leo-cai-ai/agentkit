from __future__ import annotations

import zipfile

import agentkit.core.rag.service as rag_service
from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.ocr import NoneOcrProvider, OcrResult
from agentkit.core.rag.ingest import AdaptiveTextChunker
from agentkit.core.rag.loaders import (
    DocumentFolderLoader,
    DocumentLoadOptions,
    FileLoadReport,
)
from agentkit.core.rag.retrieval import KeywordRetriever
from agentkit.core.rag.service import KnowledgeService
from agentkit.core.rag.store import InMemoryKnowledgeStore


class RecordingOcrProvider:
    name = "ollama"
    model = "glm-ocr:latest"
    enabled = True

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[bytes, str, str]] = []
        self.fail_first = fail_first

    def analyze(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        hint: str = "",
    ) -> OcrResult:
        self.calls.append((image_bytes, mime_type, hint))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("first image failed")
        return OcrResult(
            status="completed",
            text="识别文本",
            provider=self.name,
            model=self.model,
        )


def _knowledge_service(*, ocr_provider) -> KnowledgeService:
    store = InMemoryKnowledgeStore()
    return KnowledgeService(
        tenant_id="t1",
        tenant_selector="company_alpha",
        store=store,
        embeddings=FakeEmbeddingProvider(dim=8),
        retriever=KeywordRetriever(store=store),
        chunker=AdaptiveTextChunker(),
        ocr_provider=ocr_provider,
    )


def test_rag_none_provider_disables_ocr_without_tesseract(monkeypatch, tmp_path) -> None:
    service = _knowledge_service(ocr_provider=NoneOcrProvider())
    captured: dict[str, object] = {}

    class RecordingLoader:
        def __init__(self, *, options, ocr_provider):
            captured["enabled"] = options.ocr_enabled
            captured["provider"] = ocr_provider

        def load_path_with_report(self, *args, **kwargs):
            return FileLoadReport()

    monkeypatch.setattr(rag_service, "DocumentFolderLoader", RecordingLoader)

    service.ingest_path(tmp_path, ocr_enabled=True)

    assert captured == {"enabled": False, "provider": None}


def test_rag_ollama_provider_is_injected_when_ocr_is_enabled(monkeypatch, tmp_path) -> None:
    provider = RecordingOcrProvider()
    service = _knowledge_service(ocr_provider=provider)
    captured: dict[str, object] = {}

    class RecordingLoader:
        def __init__(self, *, options, ocr_provider):
            captured["enabled"] = options.ocr_enabled
            captured["provider"] = ocr_provider

        def load_path_with_report(self, *args, **kwargs):
            return FileLoadReport()

    monkeypatch.setattr(rag_service, "DocumentFolderLoader", RecordingLoader)

    service.ingest_path(tmp_path, ocr_enabled=True)

    assert captured == {"enabled": True, "provider": provider}


def test_docx_ocr_continues_after_one_embedded_image_fails(tmp_path) -> None:
    path = tmp_path / "images.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/media/one.png", b"one")
        archive.writestr("word/media/two.png", b"two")
    provider = RecordingOcrProvider(fail_first=True)
    loader = DocumentFolderLoader(
        options=DocumentLoadOptions(ocr_enabled=True),
        ocr_provider=provider,
    )
    warnings: list[str] = []

    blocks = loader._extract_docx_image_ocr(path, warnings)

    assert len(provider.calls) == 2
    assert len(blocks) == 1
    assert blocks[0]["text"] == "识别文本"
    assert warnings == [
        "OCR failed on embedded image word/media/one.png: first image failed"
    ]
