"""Web-layer identity resolution and RBAC enforcement.

Resolves the current :class:`Principal` from one of three sources (in priority
order, depending on settings):

1. **Auth disabled** (dev): a synthetic admin principal.
2. **Reverse-proxy identity headers**: the recommended SSO path — terminate
   OIDC/SAML at a proxy (oauth2-proxy, an API gateway, etc.) and forward
   ``X-Forwarded-User`` / ``-Email`` / ``-Roles``. Header names are configurable.
3. **Shared-token session login**: maps to a configurable admin principal.

``require_permission`` is a view decorator that enforces console RBAC using the
resolved principal stored on ``flask.g``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any

from flask import g

from agentkit.config import Settings, get_settings
from agentkit.core.identity import (
    Principal,
    has_permission,
    load_role_permissions,
    parse_roles,
)

ANONYMOUS = Principal(subject="anonymous", auth_method="anonymous")


def _dev_admin() -> Principal:
    return Principal(
        subject="dev",
        display_name="Dev (auth disabled)",
        roles=("admin",),
        auth_method="dev",
        claims={"business_roles": []},
    )


def session_principal(settings: Settings) -> Principal:
    roles = tuple(parse_roles(getattr(settings, "web_token_roles", "admin"))) or ("admin",)
    business_roles = parse_roles(getattr(settings, "web_token_business_roles", ""))
    return Principal(
        subject=getattr(settings, "web_token_subject", "console-admin") or "console-admin",
        display_name="Console Admin",
        roles=roles,
        auth_method="token",
        claims={"business_roles": business_roles},
    )


def proxy_header_principal(headers: Any, settings: Settings) -> Principal | None:
    """Build a principal from trusted reverse-proxy identity headers (or None)."""
    user = (headers.get(settings.auth_proxy_user_header) or "").strip()
    if not user:
        return None
    roles = tuple(parse_roles(headers.get(settings.auth_proxy_roles_header, "")))
    if not roles:
        roles = tuple(parse_roles(settings.auth_proxy_default_roles))
    business_roles = parse_roles(headers.get(settings.auth_proxy_business_roles_header, ""))
    if not business_roles:
        business_roles = parse_roles(settings.auth_proxy_default_business_roles)
    email = (headers.get(settings.auth_proxy_email_header) or "").strip()
    return Principal(
        subject=user,
        display_name=user,
        email=email,
        roles=roles,
        auth_method="proxy",
        claims={"business_roles": business_roles},
    )


def resolve_principal(
    settings: Settings,
    *,
    headers: Any,
    sess: Mapping[str, Any],
) -> Principal:
    """Resolve the caller's principal from the configured identity source."""
    if settings.web_auth_disabled:
        return _dev_admin()
    if getattr(settings, "auth_proxy_enabled", False):
        principal = proxy_header_principal(headers, settings)
        if principal is not None:
            return principal
    if sess.get("authenticated"):
        return session_principal(settings)
    return ANONYMOUS


def current_principal() -> Principal:
    """The principal resolved for this request (anonymous if none)."""
    return getattr(g, "principal", ANONYMOUS)


def require_permission(permission: str) -> Callable[[Callable], Callable]:
    """View decorator enforcing a console permission on the current principal."""

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            principal = current_principal()
            mapping = load_role_permissions(get_settings())
            if not has_permission(principal, permission, mapping):
                return (f"Forbidden: requires permission '{permission}'.", 403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "ANONYMOUS",
    "session_principal",
    "proxy_header_principal",
    "resolve_principal",
    "current_principal",
    "require_permission",
]
