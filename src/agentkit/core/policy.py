"""Permission and approval checks for the generic runtime."""

from __future__ import annotations

from dataclasses import dataclass

from .approvals import skill_requires_approval
from .contracts import SkillDefinition, TaskRequest


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_approval: bool = False


class PolicyGuard:
    def __init__(self, tenant_config: dict) -> None:
        self._tenant_config = tenant_config

    def check_skill(self, *, request: TaskRequest, skill: SkillDefinition) -> PolicyDecision:
        role_permissions = self._tenant_config.get("role_permissions", {})
        granted: set[str] = set()
        for role in request.roles:
            granted.update(role_permissions.get(role, []))

        missing = [permission for permission in skill.permissions if permission not in granted]
        if missing:
            return PolicyDecision(
                allowed=False,
                reason=f"missing permissions: {', '.join(missing)}",
            )

        return PolicyDecision(
            allowed=True,
            reason="allowed",
            requires_approval=skill_requires_approval(
                skill_name=skill.name,
                approval_required_skills=self._tenant_config.get("approval_required_skills", []),
                approved_skills=request.context.get("approved_skills", []),
            ),
        )
