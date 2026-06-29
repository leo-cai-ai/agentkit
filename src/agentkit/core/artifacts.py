"""Run-scoped artifacts for workflow handoff.

Artifacts keep large step outputs out of downstream LLM context. A step returns
small summaries and references; callers can fetch full payloads only when a
later step actually needs them.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    payload: Any
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: round(time.time(), 3))

    def ref(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "summary": self.summary,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class ArtifactStore(Protocol):
    def put(
        self,
        *,
        kind: str,
        payload: Any,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord: ...

    def get(self, artifact_id: str) -> ArtifactRecord: ...

    def list(self) -> list[ArtifactRecord]: ...


class InMemoryArtifactStore:
    """Default run-local artifact store.

    The callback lets the executor mirror writes into audit events. Production
    deployments can replace this with SQLite/Postgres/object storage without
    changing skill handlers.
    """

    def __init__(self, *, on_write: Any = None) -> None:
        self._records: dict[str, ArtifactRecord] = {}
        self._on_write = on_write

    def put(
        self,
        *,
        kind: str,
        payload: Any,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        artifact_id = f"artifact_{uuid.uuid4().hex[:12]}"
        record = ArtifactRecord(
            artifact_id=artifact_id,
            kind=kind,
            payload=payload,
            summary=summary,
            metadata=dict(metadata or {}),
        )
        self._records[artifact_id] = record
        if callable(self._on_write):
            self._on_write(record)
        return record

    def get(self, artifact_id: str) -> ArtifactRecord:
        return self._records[artifact_id]

    def list(self) -> list[ArtifactRecord]:
        return list(self._records.values())


__all__ = ["ArtifactRecord", "ArtifactStore", "InMemoryArtifactStore"]
