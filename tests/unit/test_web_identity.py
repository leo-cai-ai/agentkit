"""Unit tests for web-layer identity resolution (agentkit.web.identity)."""

from __future__ import annotations

from agentkit.config import Settings
from agentkit.web.identity import (
    proxy_header_principal,
    resolve_principal,
    session_principal,
)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_resolve_dev_admin_when_auth_disabled() -> None:
    p = resolve_principal(_settings(web_auth_disabled=True), headers={}, sess={})
    assert p.auth_method == "dev"
    assert "admin" in p.roles


def test_resolve_session_principal_after_token_login() -> None:
    s = _settings(
        web_auth_token="t",
        web_token_subject="ops",
        web_token_roles="operator",
        web_token_business_roles="recruiter",
    )
    p = resolve_principal(s, headers={}, sess={"authenticated": True})
    assert p.auth_method == "token"
    assert p.subject == "ops"
    assert p.roles == ("operator",)
    assert p.claims["business_roles"] == ["recruiter"]


def test_resolve_anonymous_when_not_logged_in() -> None:
    p = resolve_principal(_settings(web_auth_token="t"), headers={}, sess={})
    assert p.is_authenticated is False


def test_proxy_header_principal_reads_identity() -> None:
    s = _settings(auth_proxy_enabled=True)
    headers = {
        "X-Forwarded-User": "alice",
        "X-Forwarded-Email": "alice@example.com",
        "X-Forwarded-Roles": "operator, viewer",
        "X-Forwarded-Business-Roles": "recruiter, growth_manager",
    }
    p = proxy_header_principal(headers, s)
    assert p is not None
    assert p.subject == "alice"
    assert p.email == "alice@example.com"
    assert p.roles == ("operator", "viewer")
    assert p.claims["business_roles"] == ["recruiter", "growth_manager"]
    assert p.auth_method == "proxy"


def test_proxy_header_principal_default_roles_when_absent() -> None:
    s = _settings(auth_proxy_enabled=True, auth_proxy_default_roles="viewer")
    p = proxy_header_principal({"X-Forwarded-User": "bob"}, s)
    assert p is not None
    assert p.roles == ("viewer",)


def test_proxy_header_principal_none_without_user() -> None:
    s = _settings(auth_proxy_enabled=True)
    assert proxy_header_principal({}, s) is None


def test_resolve_prefers_proxy_then_session() -> None:
    s = _settings(auth_proxy_enabled=True, web_auth_token="t")
    headers = {"X-Forwarded-User": "carol", "X-Forwarded-Roles": "member"}
    p = resolve_principal(s, headers=headers, sess={"authenticated": True})
    assert p.subject == "carol"
    assert p.auth_method == "proxy"


def test_session_principal_defaults_to_admin() -> None:
    p = session_principal(_settings(web_auth_token="t"))
    assert p.roles == ("admin",)
