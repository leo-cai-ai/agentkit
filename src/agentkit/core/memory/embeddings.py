"""Pluggable text embeddings for semantic long-term memory.

The default ``FakeEmbeddingProvider`` is deterministic and offline-safe: it
hashes word tokens into a fixed-size bag-of-words vector and L2-normalizes,
so texts that share more words score higher cosine similarity. That is enough
for tests and a credible local fallback. ``OpenAICompatibleEmbeddingProvider``
delegates to an OpenAI-compatible ``/embeddings`` endpoint.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _stable_bucket(token: str, dim: int) -> int:
    # Process-stable hash (built-in hash() is randomized per process, which
    # would break retrieval against embeddings persisted in an earlier run).
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little") % dim


class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def dim(self) -> int: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vec))
    if norm == 0.0:
        return vec
    return [value / norm for value in vec]


class FakeEmbeddingProvider:
    """Deterministic bag-of-words hashing embedder (no network)."""

    name = "fake"

    def __init__(self, dim: int = 64) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for token in _tokens(text):
                vec[_stable_bucket(token, self._dim)] += 1.0
            vectors.append(_normalize(vec))
        return vectors


class OpenAICompatibleEmbeddingProvider:
    name = "openai"

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        dim: int = 0,
    ) -> None:
        from agentkit.llm.base import LLMRequiredError

        if not base_url or not api_key or not model:
            raise LLMRequiredError(
                "OpenAI-compatible embeddings need AGENTKIT_EMBEDDING_BASE_URL, "
                "AGENTKIT_EMBEDDING_API_KEY, and AGENTKIT_EMBEDDING_MODEL."
            )
        from langchain_openai import OpenAIEmbeddings
        from pydantic import SecretStr

        self._dim = dim
        self._client = OpenAIEmbeddings(
            base_url=base_url,
            api_key=SecretStr(api_key),
            model=model,
        )

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._client.embed_documents(list(texts))
        if self._dim == 0 and vectors:
            self._dim = len(vectors[0])
        return [list(vec) for vec in vectors]


def build_embedding_provider(settings: object) -> EmbeddingProvider:
    """Build the configured embedding provider (default: fake)."""
    provider = getattr(settings, "embedding_provider", "fake")
    if provider == "openai":
        api_key = getattr(settings, "embedding_api_key", None)
        return OpenAICompatibleEmbeddingProvider(
            base_url=getattr(settings, "embedding_base_url", None),
            api_key=api_key.get_secret_value() if api_key is not None else None,
            model=getattr(settings, "embedding_model", None),
        )
    return FakeEmbeddingProvider()
