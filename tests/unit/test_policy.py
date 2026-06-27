from agentkit.core.contracts import SkillDefinition, TaskRequest
from agentkit.core.policy import PolicyGuard


def _skill(name="candidate.rank", permissions=("hr.job.read",)):
    return SkillDefinition(
        name=name,
        domain="hr.recruitment",
        description="",
        input_schema={},
        output_schema={},
        permissions=list(permissions),
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {},
    )


def test_denied_when_role_missing_permission():
    guard = PolicyGuard({"role_permissions": {"recruiter": []}})
    req = TaskRequest(user_id="u", roles=["recruiter"], text="x")
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.allowed is False
    assert "missing permissions" in decision.reason


def test_allowed_without_approval():
    guard = PolicyGuard({"role_permissions": {"recruiter": ["hr.job.read"]}})
    req = TaskRequest(user_id="u", roles=["recruiter"], text="x")
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_requires_approval_when_skill_listed_and_not_preapproved():
    guard = PolicyGuard(
        {
            "role_permissions": {"recruiter": ["hr.job.read"]},
            "approval_required_skills": ["candidate.rank"],
        }
    )
    req = TaskRequest(user_id="u", roles=["recruiter"], text="x")
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.allowed is True
    assert decision.requires_approval is True


def test_preapproved_skill_skips_approval():
    guard = PolicyGuard(
        {
            "role_permissions": {"recruiter": ["hr.job.read"]},
            "approval_required_skills": ["candidate.rank"],
        }
    )
    req = TaskRequest(
        user_id="u",
        roles=["recruiter"],
        text="x",
        context={"approved_skills": ["candidate.rank"]},
    )
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.requires_approval is False
