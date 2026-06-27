"""SSRF-safe outbound HTTP for tools/connectors.

Tools that reach external systems should use :func:`safe_request` instead of
calling ``httpx``/``requests`` directly. It enforces:

- scheme allow-list (``https`` by default; ``http`` only if explicitly allowed),
- host resolution + blocking of private / loopback / link-local / reserved IPs
  (so a user-supplied URL cannot pivot into internal infrastructure),
- an optional egress domain allow-list,
- redirects disabled by default (a redirect can otherwise bypass the IP checks),
- a bounded timeout and response size cap.

This is defense-in-depth at the application layer; pair it with network egress
controls in production.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class SsrfError(ValueError):
    """Raised when an outbound request targets a disallowed scheme/host/IP."""


@dataclass(frozen=True)
class EgressPolicy:
    allow_http: bool = False
    allowed_domains: tuple[str, ...] = ()
    max_response_bytes: int = 5_000_000
    timeout_seconds: float = 10.0

    def schemes(self) -> tuple[str, ...]:
        return ("http", "https") if self.allow_http else ("https",)


def _host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    if not allowed_domains:
        return True
    host = host.lower().rstrip(".")
    for domain in allowed_domains:
        domain = domain.lower().lstrip(".").rstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve_public_ips(host: str) -> list[str]:
    """Resolve ``host`` and require every resolved address to be public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SsrfError(f"could not resolve host: {host}") from exc
    ips = sorted({str(info[4][0]) for info in infos})
    if not ips:
        raise SsrfError(f"no addresses for host: {host}")
    for ip in ips:
        if not _ip_is_public(ip):
            raise SsrfError(f"host {host} resolves to non-public address {ip}")
    return ips


def validate_url(url: str, policy: EgressPolicy) -> str:
    """Validate ``url`` against ``policy``; return the host (raises SsrfError)."""
    parts = urlsplit(url)
    if parts.scheme not in policy.schemes():
        raise SsrfError(f"scheme not allowed: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise SsrfError("URL has no host")
    if not _host_allowed(host, policy.allowed_domains):
        raise SsrfError(f"host not in egress allow-list: {host}")
    _resolve_public_ips(host)
    return host


def safe_request(
    method: str,
    url: str,
    *,
    policy: EgressPolicy | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an SSRF-checked HTTP request (redirects disabled, size-capped)."""
    policy = policy or build_egress_policy()
    validate_url(url, policy)
    timeout = kwargs.pop("timeout", policy.timeout_seconds)
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        response = client.request(method, url, **kwargs)
    content_length = response.headers.get("content-length")
    if content_length is not None and int(content_length) > policy.max_response_bytes:
        raise SsrfError(f"response too large: {content_length} > {policy.max_response_bytes} bytes")
    return response


def build_egress_policy() -> EgressPolicy:
    """Build the egress policy from settings (safe defaults when unavailable)."""
    try:
        from agentkit.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - settings optional in lightweight contexts
        return EgressPolicy()
    domains = getattr(settings, "egress_allowed_domains", "") or ""
    allowed = tuple(d.strip() for d in domains.split(",") if d.strip())
    return EgressPolicy(
        allow_http=bool(getattr(settings, "egress_allow_http", False)),
        allowed_domains=allowed,
        max_response_bytes=int(getattr(settings, "egress_max_response_bytes", 5_000_000)),
        timeout_seconds=float(getattr(settings, "egress_timeout_seconds", 10.0)),
    )
