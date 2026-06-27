"""Thin re-export of the canonical scorer.

The scoring logic lives in domain_packs.hr_recruitment.scoring; this module must
not duplicate it. Importable when the repo root is on sys.path (e.g. via the
package install added in a later phase, or running tests from the repo root).
"""

from __future__ import annotations

from agentkit.domain_packs.hr_recruitment.scoring import score_candidate

__all__ = ["score_candidate"]
