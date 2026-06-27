"""Unit tests for the pluggable LLM rate limiter."""

from __future__ import annotations

from langchain_core.rate_limiters import InMemoryRateLimiter

from agentkit.config import Settings
from agentkit.llm.rate_limit import SqliteRateLimiter, build_rate_limiter


def test_sqlite_rate_limiter_grants_then_empties(tmp_path) -> None:
    rl = SqliteRateLimiter(
        requests_per_second=1.0,
        db_path=tmp_path / "rl.sqlite",
        max_bucket_size=1,
    )
    # Starts full: first non-blocking acquire consumes the only token.
    assert rl.acquire(blocking=False) is True
    # Immediately after, the bucket has refilled by far less than a token.
    assert rl.acquire(blocking=False) is False


def test_sqlite_rate_limiter_blocking_returns_true(tmp_path) -> None:
    rl = SqliteRateLimiter(
        requests_per_second=50.0,
        db_path=tmp_path / "rl.sqlite",
        max_bucket_size=1,
    )
    assert rl.acquire() is True
    assert rl.acquire() is True  # blocks briefly for refill, then grants


def test_sqlite_rate_limiter_is_shared_across_instances(tmp_path) -> None:
    path = tmp_path / "shared.sqlite"
    a = SqliteRateLimiter(requests_per_second=1.0, db_path=path, max_bucket_size=1)
    b = SqliteRateLimiter(requests_per_second=1.0, db_path=path, max_bucket_size=1)
    # Two "workers" share one bucket: once A drains it, B sees it empty.
    assert a.acquire(blocking=False) is True
    assert b.acquire(blocking=False) is False


def test_build_rate_limiter_process_default() -> None:
    rl = build_rate_limiter(Settings(_env_file=None))
    assert isinstance(rl, InMemoryRateLimiter)


def test_build_rate_limiter_sqlite(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        llm_rate_limiter_backend="sqlite",
        llm_rate_limiter_sqlite_path=str(tmp_path / "rl.sqlite"),
    )
    assert isinstance(build_rate_limiter(settings), SqliteRateLimiter)


def test_build_rate_limiter_disabled() -> None:
    assert build_rate_limiter(Settings(_env_file=None, llm_rate_limiter_enabled=False)) is None
