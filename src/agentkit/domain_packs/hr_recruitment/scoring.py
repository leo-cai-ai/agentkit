"""Canonical deterministic scoring for the candidate-rank skill."""

from __future__ import annotations

from typing import Any


def score_candidate(*, required_skills: list[str], candidate: dict[str, Any]) -> dict[str, Any]:
    required = set(required_skills)
    skills = set(candidate.get("skills", []))
    matched = sorted(required & skills)
    missing = sorted(required - skills)
    score = len(matched) * 20 + int(candidate.get("years_experience", 0)) * 2
    return {
        "candidate_id": candidate["candidate_id"],
        "name": candidate["name"],
        "score": score,
        "matched_skills": matched,
        "missing_skills": missing,
        "reason": (
            f"Matched {len(matched)}/{len(required)} required skills; "
            f"{candidate.get('years_experience', 0)} years experience."
        ),
    }
