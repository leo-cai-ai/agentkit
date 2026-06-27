"""Mock ATS connector used by the HR recruitment skill pack."""

from __future__ import annotations


class MockAtsConnector:
    def __init__(self) -> None:
        self._jobs = {
            "JOB-001": {
                "job_id": "JOB-001",
                "title": "Senior AI Platform Engineer",
                "required_skills": ["python", "langgraph", "mcp", "distributed-systems"],
            }
        }
        self._candidates = {
            "C-100": {
                "candidate_id": "C-100",
                "name": "Alice Zhang",
                "skills": ["python", "langgraph", "mcp", "postgres"],
                "years_experience": 7,
            },
            "C-101": {
                "candidate_id": "C-101",
                "name": "Ben Liu",
                "skills": ["python", "java", "distributed-systems", "kafka"],
                "years_experience": 9,
            },
            "C-102": {
                "candidate_id": "C-102",
                "name": "Chloe Wang",
                "skills": ["python", "langgraph", "mcp", "distributed-systems"],
                "years_experience": 5,
            },
            "C-103": {
                "candidate_id": "C-103",
                "name": "David Chen",
                "skills": ["python", "fastapi", "postgres"],
                "years_experience": 6,
            },
            "C-104": {
                "candidate_id": "C-104",
                "name": "Eva Sun",
                "skills": ["langgraph", "mcp", "distributed-systems", "observability"],
                "years_experience": 8,
            },
        }

    def get_job(self, job_id: str) -> dict:
        if job_id not in self._jobs:
            raise KeyError(f"unknown job_id: {job_id}")
        return dict(self._jobs[job_id])

    def get_candidates(self, candidate_ids: list[str]) -> list[dict]:
        candidates = []
        for candidate_id in candidate_ids:
            if candidate_id in self._candidates:
                candidates.append(dict(self._candidates[candidate_id]))
        return candidates
