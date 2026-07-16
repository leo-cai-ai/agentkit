"""Identity and console RBAC (role-based access control).

This is the *console action* authorization layer: it answers "may this caller
run a task / approve / view governance?" based on a :class:`Principal`'s roles.
It is deliberately separate from the *tenant business* authorization in
``PolicyGuard`` (which maps a request's business roles to skill permissions).
Keeping the two layers distinct lets an IdP/proxy own who the user is and what
console actions they may take, while tenants keep owning which business skills a
role may execute.

The model is dependency-free and pure so it is trivially testable. Wire it to an
identity source (a reverse proxy that terminates OIDC and forwards identity
headers, or the built-in shared-token login) in the web layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Console permissions (actions on the management console / API).
TASK_RUN = "task:run"
TASK_APPROVE = "task:approve"
CHAT_USE = "chat:use"
GOVERNANCE_VIEW = "governance:view"
RUNS_VIEW = "runs:view"
OPERATIONS_VIEW = "operations:view"
RUNTIME_ADMIN = "runtime:admin"
WILDCARD = "*"  # grants every permission

ALL_PERMISSIONS = frozenset(
    {
        TASK_RUN,
        TASK_APPROVE,
        CHAT_USE,
        GOVERNANCE_VIEW,
        RUNS_VIEW,
        OPERATIONS_VIEW,
        RUNTIME_ADMIN,
    }
)

# Built-in role -> permission bindings. Override/extend via settings
# (AGENTKIT_RBAC_ROLE_PERMISSIONS as a JSON object of role -> [permissions]).
DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {WILDCARD},
    "operator": {
        TASK_RUN,
        TASK_APPROVE,
        CHAT_USE,
        GOVERNANCE_VIEW,
        RUNS_VIEW,
        OPERATIONS_VIEW,
    },
    "member": {TASK_RUN, CHAT_USE, RUNS_VIEW},
    "viewer": {GOVERNANCE_VIEW, RUNS_VIEW, OPERATIONS_VIEW},
}


@dataclass(frozen=True)
class Principal:
    """An authenticated (or anonymous) caller.

    ``auth_method`` records how the identity was established: ``token`` (shared
    console token), ``proxy`` (trusted reverse-proxy identity headers, i.e.
    OIDC terminated upstream), ``dev`` (auth disabled), or ``anonymous``.
    """

    subject: str
    display_name: str = ""
    email: str = ""
    roles: tuple[str, ...] = ()
    auth_method: str = "anonymous"
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_authenticated(self) -> bool:
        return self.auth_method != "anonymous"

    def to_public_dict(self) -> dict[str, Any]:
        """Audit/log-safe view (no raw claims)."""
        return {
            "subject": self.subject,
            "roles": list(self.roles),
            "auth_method": self.auth_method,
        }


def parse_roles(raw: str | None) -> list[str]:
    """Parse a comma/space/semicolon-separated role string into a clean list."""
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        role = chunk.strip()
        if role and role not in out:
            out.append(role)
    return out


def load_role_permissions(settings: Any = None) -> dict[str, set[str]]:
    """Return role->permissions, merging any settings override over the defaults."""
    mapping: dict[str, set[str]] = {
        role: set(perms) for role, perms in DEFAULT_ROLE_PERMISSIONS.items()
    }
    raw = getattr(settings, "rbac_role_permissions", "") if settings is not None else ""
    if raw:
        try:
            override = json.loads(raw)
        except (ValueError, TypeError):
            return mapping
        if isinstance(override, dict):
            for role, perms in override.items():
                if isinstance(perms, list):
                    mapping[str(role)] = {str(p) for p in perms}
    return mapping


def permissions_for(
    roles: tuple[str, ...] | list[str],
    role_permissions: dict[str, set[str]] | None = None,
) -> set[str]:
    mapping = role_permissions or DEFAULT_ROLE_PERMISSIONS
    granted: set[str] = set()
    for role in roles:
        granted.update(mapping.get(role, set()))
    return granted


def has_permission(
    principal: Principal,
    permission: str,
    role_permissions: dict[str, set[str]] | None = None,
) -> bool:
    granted = permissions_for(principal.roles, role_permissions)
    return WILDCARD in granted or permission in granted


__all__ = [
    "Principal",
    "TASK_RUN",
    "TASK_APPROVE",
    "CHAT_USE",
    "GOVERNANCE_VIEW",
    "RUNS_VIEW",
    "OPERATIONS_VIEW",
    "RUNTIME_ADMIN",
    "WILDCARD",
    "ALL_PERMISSIONS",
    "DEFAULT_ROLE_PERMISSIONS",
    "parse_roles",
    "load_role_permissions",
    "permissions_for",
    "has_permission",
]
