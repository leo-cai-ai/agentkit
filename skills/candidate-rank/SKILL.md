---
name: candidate-rank
description: Rank candidates for a job requisition using ATS tools, required skills, experience, tenant policy, and batch execution.
---

# Candidate Rank

Use this skill when the user asks to rank, shortlist, screen, or compare candidates for a job.

Required context:
- `job_id`
- `candidate_ids`
- `top_n`

Workflow:
1. Fetch the job requisition through `ats.get_job`.
2. Fetch candidate profiles through `ats.get_candidates`.
3. Score candidates against required skills and experience.
4. Return a ranked shortlist with matched skills, missing skills, score, and reason.

References:
- Read `references/scoring.md` for the current demo scoring rule.

Scripts:
- `scripts/score_candidates.py` contains the deterministic demo scoring helper.
