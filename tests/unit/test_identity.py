"""Unit tests for the console RBAC core (agentkit.core.identity)."""

from __future__ import annotations

from agentkit.config import Settings
from agentkit.core.identity import (
    GOVERNANCE_VIEW,
    TASK_APPROVE,
    TASK_RUN,
    Principal,
    has_permission,
    load_role_permissions,
    parse_roles,
    permissions_for,
)


def test_parse_roles_handles_separators_and_dedup() -> None:
    assert parse_roles("admin, operator;viewer admin") == ["admin", "operator", "viewer"]
    assert parse_roles("") == []
    assert parse_roles(None) == []


def test_principal_authentication_and_public_dict() -> None:
    anon = Principal(subject="anonymous", auth_method="anonymous")
    assert anon.is_authenticated is False
    user = Principal(subject="u1", roles=("member",), auth_method="proxy")
    assert user.is_authenticated is True
    pub = user.to_public_dict()
    assert pub == {"subject": "u1", "roles": ["member"], "auth_method": "proxy"}


def test_admin_wildcard_grants_everything() -> None:
    admin = Principal(subject="a", roles=("admin",), auth_method="token")
    assert has_permission(admin, TASK_RUN)
    assert has_permission(admin, TASK_APPROVE)
    assert has_permission(admin, GOVERNANCE_VIEW)


def test_viewer_cannot_run_but_can_view() -> None:
    viewer = Principal(subject="v", roles=("viewer",), auth_method="proxy")
    assert has_permission(viewer, GOVERNANCE_VIEW)
    assert not has_permission(viewer, TASK_RUN)
    assert not has_permission(viewer, TASK_APPROVE)


def test_member_can_run_not_approve() -> None:
    member = Principal(subject="m", roles=("member",), auth_method="proxy")
    assert has_permission(member, TASK_RUN)
    assert not has_permission(member, TASK_APPROVE)


def test_permissions_for_unions_roles() -> None:
    perms = permissions_for(("viewer", "member"))
    assert TASK_RUN in perms
    assert GOVERNANCE_VIEW in perms


def test_load_role_permissions_override_merges() -> None:
    settings = Settings(
        _env_file=None,
        rbac_role_permissions='{"viewer": ["task:run", "governance:view"]}',
    )
    mapping = load_role_permissions(settings)
    viewer = Principal(subject="v", roles=("viewer",), auth_method="proxy")
    assert has_permission(viewer, TASK_RUN, mapping)
    # Built-in roles still present after merge.
    admin = Principal(subject="a", roles=("admin",), auth_method="token")
    assert has_permission(admin, TASK_APPROVE, mapping)


def test_load_role_permissions_bad_json_falls_back_to_defaults() -> None:
    settings = Settings(_env_file=None, rbac_role_permissions="not-json{")
    mapping = load_role_permissions(settings)
    member = Principal(subject="m", roles=("member",), auth_method="proxy")
    assert has_permission(member, TASK_RUN, mapping)
