"""Security hardening for the Flask management console.

Provides shared-token authentication, CSRF protection for state-changing
requests, secure cookies, and security response headers. The decision logic is
factored into pure functions so it can be unit-tested without a request
context; ``configure_security`` wires everything onto a Flask app.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any

from flask import Flask, g, redirect, render_template, request, session, url_for

from agentkit.config import Settings, get_settings
from agentkit.web.identity import resolve_principal

logger = logging.getLogger("agentkit.web.security")

# Endpoints reachable without authentication.
PUBLIC_ENDPOINTS = frozenset({"login", "logout", "healthz", "static"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def token_matches(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison that is safe for missing values."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def ensure_csrf_token(sess: Any) -> str:
    token = sess.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        sess["csrf_token"] = token
    return token


def csrf_matches(sess: Any, sent: str | None) -> bool:
    return token_matches(sent, sess.get("csrf_token"))


def security_headers() -> dict[str, str]:
    return {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
        "Cache-Control": "no-store",
    }


def auth_required(
    *,
    endpoint: str | None,
    method: str,
    is_authenticated: bool,
    csrf_ok: bool,
    settings: Settings,
) -> str:
    """Pure access decision.

    Returns one of: ``ok``, ``login``, ``unconfigured``, ``csrf``.
    """
    if endpoint in PUBLIC_ENDPOINTS or endpoint is None:
        return "ok"
    if settings.web_auth_disabled:
        return "ok"
    if is_authenticated:
        # Identity already established (proxy headers or session login); a shared
        # token need not be configured in that case.
        if method in STATE_CHANGING_METHODS and not csrf_ok:
            return "csrf"
        return "ok"
    if settings.web_auth_token is None:
        # Fail closed: refuse protected routes until an identity source exists.
        return "unconfigured"
    return "login"


def configure_security(app: Flask) -> None:
    """Apply secret key, cookie flags, hooks, and auth routes to ``app``.

    Idempotent: secret key / cookie config refresh on every call (so tests can
    re-run after changing settings), but routes and hooks register only once.
    """
    settings = get_settings()

    secret = (
        settings.web_secret_key.get_secret_value()
        if settings.web_secret_key
        else secrets.token_urlsafe(32)
    )
    if settings.web_secret_key is None and not settings.web_auth_disabled:
        logger.warning(
            "AGENTKIT_WEB_SECRET_KEY not set; using an ephemeral key. "
            "Sessions will not survive a restart."
        )
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=settings.web_cookie_secure,
    )

    if getattr(app, "_agentkit_security", False):
        return
    app._agentkit_security = True  # type: ignore[attr-defined]

    @app.before_request
    def _enforce_auth():  # pragma: no cover - exercised via test client
        current = get_settings()
        # Resolve the caller's identity once per request so views can enforce
        # RBAC and attribute actions to a real principal.
        principal = resolve_principal(current, headers=request.headers, sess=session)
        g.principal = principal
        sent_csrf = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        # Proxy-header (and dev) auth has no ambient session cookie to forge, so
        # the session-CSRF check does not apply; the upstream proxy owns CSRF.
        csrf_ok = principal.auth_method in {"proxy", "dev"} or csrf_matches(session, sent_csrf)
        decision = auth_required(
            endpoint=request.endpoint,
            method=request.method,
            is_authenticated=principal.is_authenticated,
            csrf_ok=csrf_ok,
            settings=current,
        )
        if decision == "ok":
            return None
        if decision == "login":
            return redirect(url_for("login", next=request.path))
        if decision == "csrf":
            return ("CSRF token missing or invalid.", 400)
        # unconfigured
        return (
            "Web console authentication is not configured. "
            "Set AGENTKIT_WEB_AUTH_TOKEN (or AGENTKIT_WEB_AUTH_DISABLED=true for local dev).",
            503,
        )

    @app.after_request
    def _apply_headers(response):  # pragma: no cover - exercised via test client
        for key, value in security_headers().items():
            response.headers.setdefault(key, value)
        return response

    @app.context_processor
    def _inject_csrf() -> dict[str, str]:
        return {"csrf_token": session.get("csrf_token", "")}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        current = get_settings()
        if current.web_auth_token is None and not current.web_auth_disabled:
            return render_template(
                "login.html", error="Auth token not configured on the server."
            ), 503
        if request.method == "POST":
            expected = current.web_auth_token.get_secret_value() if current.web_auth_token else None
            if token_matches(request.form.get("token"), expected):
                session.clear()
                session["authenticated"] = True
                ensure_csrf_token(session)
                target = request.args.get("next")
                if not target or not target.startswith("/"):
                    target = url_for("overview")
                return redirect(target)
            # Generic message; avoid leaking whether a token exists.
            return render_template("login.html", error="Invalid credentials."), 401
        return render_template("login.html", error=None)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))


__all__ = [
    "token_matches",
    "ensure_csrf_token",
    "csrf_matches",
    "security_headers",
    "auth_required",
    "configure_security",
    "PUBLIC_ENDPOINTS",
]
