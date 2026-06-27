"""Pluggable LLM rate limiting.

The default ``process`` backend uses LangChain's in-memory token bucket. That is
correct for a single process but **not shared across gunicorn workers**: each
worker gets its own bucket, so the effective request rate becomes
``workers x requests_per_second`` and can blow past an endpoint's spike-arrest
limit.

The ``sqlite`` backend persists a single token bucket in a SQLite file, so every
worker on the host shares one budget — the right default for multi-worker
deployments behind a 1 rps endpoint. A future Redis backend can implement the
same ``build_rate_limiter`` seam for multi-host fan-out without touching callers.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.rate_limiters import BaseRateLimiter, InMemoryRateLimiter

if TYPE_CHECKING:
    from agentkit.config import Settings

_DEFAULT_SQLITE_PATH = "data/llm_ratelimit.sqlite"


class SqliteRateLimiter(BaseRateLimiter):
    """Cross-process token-bucket rate limiter backed by a shared SQLite file.

    All processes that open the same ``db_path`` share one bucket. Refill and
    consume happen inside a single ``BEGIN IMMEDIATE`` write transaction so the
    bucket update is atomic across workers. Wall-clock time is used (not a
    per-process monotonic clock) so refill is consistent across processes.
    """

    def __init__(
        self,
        *,
        requests_per_second: float,
        db_path: str | Path,
        max_bucket_size: float = 1.0,
        check_every_n_seconds: float = 0.1,
        busy_timeout: float = 5.0,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self._rps = float(requests_per_second)
        self._max = float(max_bucket_size)
        self._check_every_n_seconds = float(check_every_n_seconds)
        self._busy_timeout = float(busy_timeout)
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=self._busy_timeout)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS bucket ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), tokens REAL NOT NULL, last REAL NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO bucket (id, tokens, last) VALUES (1, ?, ?)",
                (self._max, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def _try_consume(self) -> bool:
        conn = sqlite3.connect(self._db_path, timeout=self._busy_timeout)
        try:
            conn.isolation_level = None  # explicit transaction control below
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT tokens, last FROM bucket WHERE id = 1").fetchone()
            now = time.time()
            tokens, last = (row[0], row[1]) if row else (self._max, now)
            tokens = min(self._max, tokens + max(0.0, now - last) * self._rps)
            granted = tokens >= 1.0
            if granted:
                tokens -= 1.0
            conn.execute("UPDATE bucket SET tokens = ?, last = ? WHERE id = 1", (tokens, now))
            conn.execute("COMMIT")
            return granted
        except sqlite3.OperationalError:
            # Another worker holds the write lock; treat as "not granted" and let
            # the caller retry after the usual check interval.
            return False
        finally:
            conn.close()

    def acquire(self, *, blocking: bool = True) -> bool:
        if not blocking:
            return self._try_consume()
        while not self._try_consume():
            time.sleep(self._check_every_n_seconds)
        return True

    async def aacquire(self, *, blocking: bool = True) -> bool:
        if not blocking:
            return self._try_consume()
        while not self._try_consume():
            await asyncio.sleep(self._check_every_n_seconds)
        return True


def build_rate_limiter(settings: Settings) -> Any:
    """Build the configured LLM rate limiter (or ``None`` when disabled)."""
    if not getattr(settings, "llm_rate_limiter_enabled", True):
        return None
    rps = float(getattr(settings, "llm_requests_per_second", 0.9))
    backend = getattr(settings, "llm_rate_limiter_backend", "process")
    if backend == "sqlite":
        path = getattr(settings, "llm_rate_limiter_sqlite_path", None) or _DEFAULT_SQLITE_PATH
        return SqliteRateLimiter(requests_per_second=rps, db_path=path)
    return InMemoryRateLimiter(
        requests_per_second=rps,
        check_every_n_seconds=0.1,
        max_bucket_size=1,
    )
