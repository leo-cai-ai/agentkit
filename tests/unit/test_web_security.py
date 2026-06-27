"""Unit tests for web security decision helpers."""

from __future__ import annotations

from agentkit.config import Settings
from agentkit.web import security


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_token_matches() -> None:
    assert security.token_matches("abc", "abc") is True
    assert security.token_matches("abc", "abd") is False
    assert security.token_matches("", "abc") is False
    assert security.token_matches("abc", None) is False
    assert security.token_matches(None, None) is False


def test_csrf_helpers() -> None:
    sess: dict = {}
    token = security.ensure_csrf_token(sess)
    assert sess["csrf_token"] == token
    # Idempotent.
    assert security.ensure_csrf_token(sess) == token
    assert security.csrf_matches(sess, token) is True
    assert security.csrf_matches(sess, "wrong") is False
    assert security.csrf_matches(sess, None) is False


def test_security_headers_present() -> None:
    headers = security.security_headers()
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in headers
    assert headers["Referrer-Policy"] == "no-referrer"


def test_auth_required_public_endpoint_ok() -> None:
    s = _settings(web_auth_token="t")
    assert (
        security.auth_required(
            endpoint="login",
            method="GET",
            is_authenticated=False,
            csrf_ok=False,
            settings=s,
        )
        == "ok"
    )


def test_auth_required_unconfigured_fail_closed() -> None:
    s = _settings()  # no token, not disabled
    assert (
        security.auth_required(
            endpoint="overview",
            method="GET",
            is_authenticated=False,
            csrf_ok=False,
            settings=s,
        )
        == "unconfigured"
    )


def test_auth_required_disabled_allows() -> None:
    s = _settings(web_auth_disabled=True)
    assert (
        security.auth_required(
            endpoint="overview",
            method="GET",
            is_authenticated=False,
            csrf_ok=False,
            settings=s,
        )
        == "ok"
    )


def test_auth_required_redirects_when_unauthenticated() -> None:
    s = _settings(web_auth_token="t")
    assert (
        security.auth_required(
            endpoint="overview",
            method="GET",
            is_authenticated=False,
            csrf_ok=False,
            settings=s,
        )
        == "login"
    )


def test_auth_required_csrf_on_post() -> None:
    s = _settings(web_auth_token="t")
    assert (
        security.auth_required(
            endpoint="create_task",
            method="POST",
            is_authenticated=True,
            csrf_ok=False,
            settings=s,
        )
        == "csrf"
    )
    assert (
        security.auth_required(
            endpoint="create_task",
            method="POST",
            is_authenticated=True,
            csrf_ok=True,
            settings=s,
        )
        == "ok"
    )
