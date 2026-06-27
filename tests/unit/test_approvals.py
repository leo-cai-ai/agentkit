from agentkit.core.approvals import evaluate_approval, skill_requires_approval


def test_no_required_skills():
    view = evaluate_approval(
        planned_skills=["a", "b"],
        approval_required_skills=[],
        approved_skills=[],
        rejected_skills=[],
    )
    assert view.required == []
    assert view.pending == []
    assert view.rejected == []


def test_pending_when_required_and_not_approved():
    view = evaluate_approval(
        planned_skills=["candidate.rank"],
        approval_required_skills=["candidate.rank"],
        approved_skills=[],
        rejected_skills=[],
    )
    assert view.required == ["candidate.rank"]
    assert view.pending == ["candidate.rank"]
    assert view.rejected == []


def test_approved_clears_pending():
    view = evaluate_approval(
        planned_skills=["candidate.rank"],
        approval_required_skills=["candidate.rank"],
        approved_skills=["candidate.rank"],
        rejected_skills=[],
    )
    assert view.pending == []
    assert view.rejected == []


def test_rejected_takes_precedence_over_pending():
    view = evaluate_approval(
        planned_skills=["candidate.rank"],
        approval_required_skills=["candidate.rank"],
        approved_skills=[],
        rejected_skills=["candidate.rank"],
    )
    assert view.rejected == ["candidate.rank"]
    assert view.pending == []


def test_non_required_skills_are_ignored():
    view = evaluate_approval(
        planned_skills=["free.skill"],
        approval_required_skills=["candidate.rank"],
        approved_skills=[],
        rejected_skills=["free.skill"],
    )
    assert view.required == []
    assert view.rejected == []
    assert view.pending == []


def test_skill_requires_approval_ignores_rejected():
    assert skill_requires_approval(
        skill_name="candidate.rank",
        approval_required_skills=["candidate.rank"],
        approved_skills=[],
    )
    assert not skill_requires_approval(
        skill_name="candidate.rank",
        approval_required_skills=["candidate.rank"],
        approved_skills=["candidate.rank"],
    )
    assert not skill_requires_approval(
        skill_name="free.skill",
        approval_required_skills=["candidate.rank"],
        approved_skills=[],
    )
