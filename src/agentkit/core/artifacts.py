"""Run-scoped artifacts for workflow handoff.

Artifacts keep large step outputs out of downstream LLM context. A step returns
small summaries and references; callers can fetch full payloads only when a
later step actually needs them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ArtifactPayloadTooLargeError(ValueError):
    """Raised when a serialized artifact payload exceeds its storage limit."""


def canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically for storage, sizing, and hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    payload: Any
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: round(time.time(), 3))
    payload_sha256: str = ""
    payload_bytes: int = 0

    def ref(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "summary": self.summary,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "payload_sha256": self.payload_sha256,
            "payload_bytes": self.payload_bytes,
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


class SqliteArtifactStore:
    """SQLite-backed artifacts, strictly scoped to one tenant and run."""

    def __init__(
        self,
        *,
        tenant_id: str,
        run_id: str,
        sqlite_path: str | Path,
        max_payload_bytes: int = 1_048_576,
        on_write: Any = None,
    ) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be greater than zero")
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._db_path = Path(sqlite_path)
        self._max_payload_bytes = max_payload_bytes
        self._on_write = on_write
        self._init_schema()

    def put(
        self,
        *,
        kind: str,
        payload: Any,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        payload_json = canonical_json(payload)
        payload_bytes = len(payload_json)
        if payload_bytes > self._max_payload_bytes:
            raise ArtifactPayloadTooLargeError(
                "Artifact payload is "
                f"{payload_bytes} bytes; limit is {self._max_payload_bytes} bytes"
            )
        metadata_value = dict(metadata or {})
        metadata_json = canonical_json(metadata_value).decode("utf-8")
        record = ArtifactRecord(
            artifact_id=f"artifact_{uuid.uuid4().hex[:12]}",
            kind=kind,
            payload=payload,
            summary=summary,
            metadata=metadata_value,
            payload_sha256=hashlib.sha256(payload_json).hexdigest(),
            payload_bytes=payload_bytes,
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO workflow_artifacts (
                        artifact_id, tenant_id, run_id, kind, payload_json,
                        payload_sha256, payload_bytes, summary, metadata_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.artifact_id,
                        self._tenant_id,
                        self._run_id,
                        record.kind,
                        payload_json.decode("utf-8"),
                        record.payload_sha256,
                        record.payload_bytes,
                        record.summary,
                        metadata_json,
                        record.created_at,
                    ),
                )
        finally:
            conn.close()
        if callable(self._on_write):
            self._on_write(record)
        return record

    def get(self, artifact_id: str) -> ArtifactRecord:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT artifact_id, kind, payload_json, payload_sha256, payload_bytes,
                           summary, metadata_json, created_at
                    FROM workflow_artifacts
                    WHERE artifact_id = ? AND tenant_id = ? AND run_id = ?
                    """,
                    (artifact_id, self._tenant_id, self._run_id),
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(artifact_id)
        return _sqlite_record(row)

    def list(self) -> list[ArtifactRecord]:
        conn = self._connect()
        try:
            with conn:
                rows = conn.execute(
                    """
                    SELECT artifact_id, kind, payload_json, payload_sha256, payload_bytes,
                           summary, metadata_json, created_at
                    FROM workflow_artifacts
                    WHERE tenant_id = ? AND run_id = ?
                    ORDER BY created_at ASC, artifact_id ASC
                    """,
                    (self._tenant_id, self._run_id),
                ).fetchall()
        finally:
            conn.close()
        return [_sqlite_record(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        from .migrations import run_sqlite_migrations

        run_sqlite_migrations(self._db_path)


class PostgresArtifactStore:
    """PostgreSQL-backed artifacts, strictly scoped to one tenant and run."""

    def __init__(
        self,
        *,
        tenant_id: str,
        run_id: str,
        settings: Any,
        max_payload_bytes: int = 1_048_576,
        on_write: Any = None,
    ) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be greater than zero")
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._settings = settings
        self._max_payload_bytes = max_payload_bytes
        self._on_write = on_write
        self._init_schema()

    def put(
        self,
        *,
        kind: str,
        payload: Any,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        payload_json = canonical_json(payload)
        payload_bytes = len(payload_json)
        if payload_bytes > self._max_payload_bytes:
            raise ArtifactPayloadTooLargeError(
                "Artifact payload is "
                f"{payload_bytes} bytes; limit is {self._max_payload_bytes} bytes"
            )
        metadata_value = dict(metadata or {})
        record = ArtifactRecord(
            artifact_id=f"artifact_{uuid.uuid4().hex[:12]}",
            kind=kind,
            payload=payload,
            summary=summary,
            metadata=metadata_value,
            payload_sha256=hashlib.sha256(payload_json).hexdigest(),
            payload_bytes=payload_bytes,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_artifacts (
                    artifact_id, tenant_id, run_id, kind, payload_json,
                    payload_sha256, payload_bytes, summary, metadata_json, created_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    record.artifact_id,
                    self._tenant_id,
                    self._run_id,
                    record.kind,
                    payload_json.decode("utf-8"),
                    record.payload_sha256,
                    record.payload_bytes,
                    record.summary,
                    canonical_json(metadata_value).decode("utf-8"),
                    record.created_at,
                ),
            )
        if callable(self._on_write):
            self._on_write(record)
        return record

    def get(self, artifact_id: str) -> ArtifactRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT artifact_id, kind, payload_json::text, payload_sha256, payload_bytes,
                       summary, metadata_json::text, created_at
                FROM workflow_artifacts
                WHERE artifact_id = %s AND tenant_id = %s AND run_id = %s
                """,
                (artifact_id, self._tenant_id, self._run_id),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return _postgres_record(row)

    def list(self) -> list[ArtifactRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT artifact_id, kind, payload_json::text, payload_sha256, payload_bytes,
                       summary, metadata_json::text, created_at
                FROM workflow_artifacts
                WHERE tenant_id = %s AND run_id = %s
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (self._tenant_id, self._run_id),
            ).fetchall()
        return [_postgres_record(row) for row in rows]

    def _connect(self) -> Any:
        from .pg import connection

        return connection(self._settings)

    def _init_schema(self) -> None:
        from .migrations import run_postgres_migrations

        run_postgres_migrations(self._settings)


def build_artifact_store(
    *,
    backend: str,
    tenant_id: str,
    run_id: str,
    sqlite_path: str | Path | None = None,
    settings: Any = None,
    max_payload_bytes: int = 1_048_576,
    on_write: Any = None,
) -> ArtifactStore:
    """Build an artifact store for one tenant/run persistence scope."""
    normalized_backend = backend.lower()
    if normalized_backend == "memory":
        return InMemoryArtifactStore(on_write=on_write)
    if normalized_backend == "sqlite":
        if sqlite_path is None:
            raise ValueError("sqlite_path is required for the SQLite artifact store")
        return SqliteArtifactStore(
            tenant_id=tenant_id,
            run_id=run_id,
            sqlite_path=sqlite_path,
            max_payload_bytes=max_payload_bytes,
            on_write=on_write,
        )
    if normalized_backend == "postgres":
        if settings is None:
            raise ValueError("settings is required for the PostgreSQL artifact store")
        return PostgresArtifactStore(
            tenant_id=tenant_id,
            run_id=run_id,
            settings=settings,
            max_payload_bytes=max_payload_bytes,
            on_write=on_write,
        )
    raise ValueError(f"Unsupported artifact backend: {backend!r}")


def _sqlite_record(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=str(row["artifact_id"]),
        kind=str(row["kind"]),
        payload=json.loads(row["payload_json"]),
        summary=str(row["summary"]),
        metadata=json.loads(row["metadata_json"]),
        created_at=float(row["created_at"]),
        payload_sha256=str(row["payload_sha256"]),
        payload_bytes=int(row["payload_bytes"]),
    )


def _postgres_record(row: Any) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=str(row[0]),
        kind=str(row[1]),
        payload=json.loads(row[2]),
        summary=str(row[5]),
        metadata=json.loads(row[6]),
        created_at=float(row[7]),
        payload_sha256=str(row[3]),
        payload_bytes=int(row[4]),
    )


__all__ = [
    "ArtifactPayloadTooLargeError",
    "ArtifactRecord",
    "ArtifactStore",
    "InMemoryArtifactStore",
    "PostgresArtifactStore",
    "SqliteArtifactStore",
    "build_artifact_store",
    "canonical_json",
]
