"""Durable idempotency ledger for side-effecting tool calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .artifacts import canonical_json


class IdempotencyError(RuntimeError):
    """Raised when an idempotency ledger operation cannot be completed safely."""


class IdempotencyConflictError(IdempotencyError):
    """Raised when a key is reused with a different request payload."""


class IdempotencyInProgressError(IdempotencyError):
    """Raised when another caller has already claimed a key."""


class IdempotencyOutcomeUnknownError(IdempotencyError):
    """Raised when a prior side effect may have completed without confirmation."""


@dataclass(frozen=True)
class IdempotencyClaim:
    """A newly claimed call or a durable successful result."""

    tenant_id: str
    tool_name: str
    idempotency_key: str
    args_sha256: str
    status: Literal["claimed", "succeeded"]
    result: dict[str, Any] | None = None

    @property
    def claimed(self) -> bool:
        """Whether this caller owns the right to execute the tool."""
        return self.status == "claimed"


class IdempotencyStore(Protocol):
    """Persistence contract for one tenant's tool idempotency records."""

    def begin(
        self,
        *,
        tool_name: str,
        idempotency_key: str,
        args: Mapping[str, Any],
    ) -> IdempotencyClaim: ...

    def finish_success(self, claim: IdempotencyClaim, result: dict[str, Any]) -> None: ...

    def finish_failure(self, claim: IdempotencyClaim, error_message: str) -> None: ...

    def finish_unknown(self, claim: IdempotencyClaim, error_message: str) -> None: ...


def canonical_args_hash(args: Mapping[str, Any]) -> str:
    """Hash call arguments after removing their idempotency transport key."""
    business_args = {key: value for key, value in args.items() if key != "_idempotency_key"}
    return hashlib.sha256(canonical_json(business_args)).hexdigest()


def key_digest(idempotency_key: str) -> str:
    """Return a stable audit-safe digest for an idempotency key."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


class SqliteIdempotencyStore:
    """SQLite-backed idempotency ledger scoped to a single tenant."""

    def __init__(self, *, tenant_id: str, sqlite_path: str | Path) -> None:
        self._tenant_id = tenant_id
        self._db_path = Path(sqlite_path)
        self._init_schema()

    def begin(
        self,
        *,
        tool_name: str,
        idempotency_key: str,
        args: Mapping[str, Any],
    ) -> IdempotencyClaim:
        args_sha256 = canonical_args_hash(args)
        now = _timestamp()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute(
                """
                INSERT INTO tool_idempotency_records (
                    tenant_id, tool_name, idempotency_key, args_sha256, status,
                    result_json, error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'running', NULL, '', ?, ?)
                ON CONFLICT(tenant_id, tool_name, idempotency_key) DO NOTHING
                """,
                (
                    self._tenant_id,
                    tool_name,
                    idempotency_key,
                    args_sha256,
                    now,
                    now,
                ),
            )
            if inserted.rowcount == 1:
                conn.commit()
                return IdempotencyClaim(
                    tenant_id=self._tenant_id,
                    tool_name=tool_name,
                    idempotency_key=idempotency_key,
                    args_sha256=args_sha256,
                    status="claimed",
                )

            row = conn.execute(
                """
                SELECT args_sha256, status, result_json
                FROM tool_idempotency_records
                WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                """,
                (self._tenant_id, tool_name, idempotency_key),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if row is None:  # pragma: no cover - protected by the primary key insert above
            raise IdempotencyError("Idempotency record could not be loaded")
        return _existing_claim(
            tenant_id=self._tenant_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            args_sha256=args_sha256,
            stored_args_sha256=str(row["args_sha256"]),
            status=str(row["status"]),
            result_json=row["result_json"],
        )

    def finish_success(self, claim: IdempotencyClaim, result: dict[str, Any]) -> None:
        self._finish(
            claim,
            status="succeeded",
            result_json=canonical_json(result).decode("utf-8"),
            error_message="",
        )

    def finish_failure(self, claim: IdempotencyClaim, error_message: str) -> None:
        self._finish(
            claim,
            status="failed",
            result_json=None,
            error_message=str(error_message),
        )

    def finish_unknown(self, claim: IdempotencyClaim, error_message: str) -> None:
        self._finish(
            claim,
            status="outcome_unknown",
            result_json=None,
            error_message=str(error_message),
        )

    def _finish(
        self,
        claim: IdempotencyClaim,
        *,
        status: Literal["succeeded", "failed", "outcome_unknown"],
        result_json: str | None,
        error_message: str,
    ) -> None:
        _validate_claim_tenant(claim, self._tenant_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE tool_idempotency_records
                SET status = ?, result_json = ?, error_message = ?, updated_at = ?
                WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
                  AND args_sha256 = ? AND status = 'running'
                """,
                (
                    status,
                    result_json,
                    error_message,
                    _timestamp(),
                    self._tenant_id,
                    claim.tool_name,
                    claim.idempotency_key,
                    claim.args_sha256,
                ),
            )
            if updated.rowcount != 1:
                raise IdempotencyError("Idempotency record is not an active claim")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        from .migrations import run_sqlite_migrations

        run_sqlite_migrations(self._db_path)


class PostgresIdempotencyStore:
    """PostgreSQL-backed idempotency ledger scoped to a single tenant."""

    def __init__(self, *, tenant_id: str, settings: Any) -> None:
        self._tenant_id = tenant_id
        self._settings = settings
        self._init_schema()

    def begin(
        self,
        *,
        tool_name: str,
        idempotency_key: str,
        args: Mapping[str, Any],
    ) -> IdempotencyClaim:
        args_sha256 = canonical_args_hash(args)
        now = _timestamp()
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO tool_idempotency_records (
                    tenant_id, tool_name, idempotency_key, args_sha256, status,
                    result_json, error_message, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, 'running', NULL, '', %s, %s)
                ON CONFLICT(tenant_id, tool_name, idempotency_key) DO NOTHING
                """,
                (
                    self._tenant_id,
                    tool_name,
                    idempotency_key,
                    args_sha256,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT args_sha256, status, result_json::text
                FROM tool_idempotency_records
                WHERE tenant_id = %s AND tool_name = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (self._tenant_id, tool_name, idempotency_key),
            ).fetchone()
            if inserted.rowcount == 1:
                return IdempotencyClaim(
                    tenant_id=self._tenant_id,
                    tool_name=tool_name,
                    idempotency_key=idempotency_key,
                    args_sha256=args_sha256,
                    status="claimed",
                )
            if row is None:  # pragma: no cover - protected by the primary key insert above
                raise IdempotencyError("Idempotency record could not be loaded")
            return _existing_claim(
                tenant_id=self._tenant_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                args_sha256=args_sha256,
                stored_args_sha256=str(row[0]),
                status=str(row[1]),
                result_json=row[2],
            )

    def finish_success(self, claim: IdempotencyClaim, result: dict[str, Any]) -> None:
        self._finish(
            claim,
            status="succeeded",
            result_json=canonical_json(result).decode("utf-8"),
            error_message="",
        )

    def finish_failure(self, claim: IdempotencyClaim, error_message: str) -> None:
        self._finish(
            claim,
            status="failed",
            result_json=None,
            error_message=str(error_message),
        )

    def finish_unknown(self, claim: IdempotencyClaim, error_message: str) -> None:
        self._finish(
            claim,
            status="outcome_unknown",
            result_json=None,
            error_message=str(error_message),
        )

    def _finish(
        self,
        claim: IdempotencyClaim,
        *,
        status: Literal["succeeded", "failed", "outcome_unknown"],
        result_json: str | None,
        error_message: str,
    ) -> None:
        _validate_claim_tenant(claim, self._tenant_id)
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE tool_idempotency_records
                SET status = %s, result_json = %s::jsonb, error_message = %s, updated_at = %s
                WHERE tenant_id = %s AND tool_name = %s AND idempotency_key = %s
                  AND args_sha256 = %s AND status = 'running'
                """,
                (
                    status,
                    result_json,
                    error_message,
                    _timestamp(),
                    self._tenant_id,
                    claim.tool_name,
                    claim.idempotency_key,
                    claim.args_sha256,
                ),
            )
            if updated.rowcount != 1:
                raise IdempotencyError("Idempotency record is not an active claim")

    def _connect(self) -> Any:
        from .pg import connection

        return connection(self._settings)

    def _init_schema(self) -> None:
        from .migrations import run_postgres_migrations

        run_postgres_migrations(self._settings)


def build_idempotency_store(
    *,
    backend: str,
    tenant_id: str,
    sqlite_path: str | Path | None = None,
    settings: Any = None,
) -> IdempotencyStore:
    """Build a durable idempotency ledger for one tenant."""
    normalized_backend = backend.lower()
    if normalized_backend == "sqlite":
        if sqlite_path is None:
            raise ValueError("sqlite_path is required for the SQLite idempotency store")
        return SqliteIdempotencyStore(tenant_id=tenant_id, sqlite_path=sqlite_path)
    if normalized_backend == "postgres":
        if settings is None:
            raise ValueError("settings is required for the PostgreSQL idempotency store")
        return PostgresIdempotencyStore(tenant_id=tenant_id, settings=settings)
    raise ValueError(f"Unsupported idempotency backend: {backend!r}")


def _existing_claim(
    *,
    tenant_id: str,
    tool_name: str,
    idempotency_key: str,
    args_sha256: str,
    stored_args_sha256: str,
    status: str,
    result_json: Any,
) -> IdempotencyClaim:
    if stored_args_sha256 != args_sha256:
        raise IdempotencyConflictError("Idempotency key conflicts with a prior request")
    if status == "succeeded":
        return IdempotencyClaim(
            tenant_id=tenant_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            args_sha256=args_sha256,
            status="succeeded",
            result=_decode_result(result_json),
        )
    if status == "running":
        raise IdempotencyInProgressError("Idempotency request is already in progress")
    if status == "outcome_unknown":
        raise IdempotencyOutcomeUnknownError("Idempotency outcome is unknown")
    if status == "failed":
        raise IdempotencyError("Idempotency request previously failed")
    raise IdempotencyError("Idempotency record has an unsupported status")


def _decode_result(result_json: Any) -> dict[str, Any]:
    if result_json is None:
        raise IdempotencyError("Idempotency success record has no result")
    try:
        result = json.loads(str(result_json))
    except (TypeError, ValueError) as exc:
        raise IdempotencyError("Idempotency success record has an invalid result") from exc
    if not isinstance(result, dict):
        raise IdempotencyError("Idempotency success record has an invalid result")
    return result


def _validate_claim_tenant(claim: IdempotencyClaim, tenant_id: str) -> None:
    if claim.tenant_id != tenant_id:
        raise IdempotencyError("Idempotency claim belongs to a different tenant")


def _timestamp() -> float:
    return round(time.time(), 3)


__all__ = [
    "IdempotencyClaim",
    "IdempotencyConflictError",
    "IdempotencyError",
    "IdempotencyInProgressError",
    "IdempotencyOutcomeUnknownError",
    "IdempotencyStore",
    "PostgresIdempotencyStore",
    "SqliteIdempotencyStore",
    "build_idempotency_store",
    "canonical_args_hash",
    "key_digest",
]
