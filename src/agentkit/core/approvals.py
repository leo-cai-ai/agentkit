"""Single source of truth for human-approval determination.

Both the graph-level ``HumanApprovalGate`` and the executor-level ``PolicyGuard``
need to decide which planned skills require human approval. This module owns that
membership logic so the two call sites cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalView:
    required: list[str]
    pending: list[str]
    rejected: list[str]


def evaluate_approval(
    *,
    planned_skills: Iterable[str],
    approval_required_skills: Iterable[str],
    approved_skills: Iterable[str],
    rejected_skills: Iterable[str],
) -> ApprovalView:
    required_set = set(approval_required_skills)
    approved_set = set(approved_skills)
    rejected_set = set(rejected_skills)

    required = [skill for skill in planned_skills if skill in required_set]
    rejected = [skill for skill in required if skill in rejected_set]
    pending = [
        skill for skill in required if skill not in approved_set and skill not in rejected_set
    ]
    return ApprovalView(required=required, pending=pending, rejected=rejected)


def skill_requires_approval(
    *,
    skill_name: str,
    approval_required_skills: Iterable[str],
    approved_skills: Iterable[str],
) -> bool:
    return skill_name in set(approval_required_skills) and skill_name not in set(approved_skills)
