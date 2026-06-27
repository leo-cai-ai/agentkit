"""Unit tests for SSRF-safe outbound HTTP helpers."""

from __future__ import annotations

import pytest

from agentkit.core import net
from agentkit.core.net import (
    EgressPolicy,
    SsrfError,
    _host_allowed,
    _ip_is_public,
    validate_url,
)


def test_rejects_non_https_by_default() -> None:
    with pytest.raises(SsrfError):
        validate_url("http://example.com", EgressPolicy())


def test_allows_http_when_opted_in(monkeypatch) -> None:
    monkeypatch.setattr(
        net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    assert validate_url("http://example.com", EgressPolicy(allow_http=True)) == "example.com"


def test_rejects_missing_host() -> None:
    with pytest.raises(SsrfError):
        validate_url("https://", EgressPolicy())


def test_rejects_loopback() -> None:
    with pytest.raises(SsrfError):
        validate_url("https://127.0.0.1", EgressPolicy())


def test_rejects_private_ip() -> None:
    with pytest.raises(SsrfError):
        validate_url("https://10.0.0.5", EgressPolicy())


def test_allowlist_blocks_other_domains() -> None:
    with pytest.raises(SsrfError):
        validate_url("https://evil.com", EgressPolicy(allowed_domains=("example.com",)))


def test_host_allowed_matches_domain_and_subdomain() -> None:
    assert _host_allowed("example.com", ("example.com",))
    assert _host_allowed("api.example.com", ("example.com",))
    assert not _host_allowed("example.com.evil.com", ("example.com",))


def test_ip_is_public() -> None:
    assert _ip_is_public("8.8.8.8")
    assert not _ip_is_public("192.168.1.1")
    assert not _ip_is_public("::1")


def test_validate_url_allows_public_host(monkeypatch) -> None:
    monkeypatch.setattr(
        net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    assert validate_url("https://example.com/path?x=1", EgressPolicy()) == "example.com"
