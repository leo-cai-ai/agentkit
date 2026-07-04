"""与业务无关的有限审核门禁状态机。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, TypeVar

ReviewStatus = Literal["passed", "revisable", "blocked"]
_T = TypeVar("_T")


@dataclass(frozen=True)
class ReviewPolicy:
    """Skill 可选的审核次数与失败关闭策略。"""

    enabled: bool = False
    max_revisions: int = 0
    exhausted_status: Literal["blocked"] = "blocked"

    def __post_init__(self) -> None:
        if self.max_revisions < 0:
            raise ValueError("max_revisions 不能小于 0")


@dataclass(frozen=True)
class ReviewDecision:
    """一次业务审核的结构化决定。"""

    status: ReviewStatus
    reason: str = ""
    findings: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passed(cls, **kwargs: Any) -> ReviewDecision:
        return cls(status="passed", **kwargs)

    @classmethod
    def revisable(cls, **kwargs: Any) -> ReviewDecision:
        return cls(status="revisable", **kwargs)

    @classmethod
    def blocked(cls, **kwargs: Any) -> ReviewDecision:
        return cls(status="blocked", **kwargs)


@dataclass(frozen=True)
class ReviewLoopResult(Generic[_T]):
    candidate: _T
    decision: ReviewDecision
    revision_count: int
    history: tuple[ReviewDecision, ...]


@dataclass(frozen=True)
class ReviewTransition:
    """供审计与 Artifact 记录使用的状态转换事件。"""

    stage: Literal["review", "revise"]
    attempt: int
    decision: ReviewDecision


class ReviewExecutionError(RuntimeError):
    """Reviewer 或 Reviser 失败，并保留可审计的阶段信息。"""

    def __init__(self, stage: str, attempt: int, cause: Exception) -> None:
        super().__init__(f"审核门禁在 {stage} 阶段失败（attempt={attempt}）: {cause}")
        self.stage = stage
        self.attempt = attempt
        self.cause = cause


class ReviewLoop:
    """在固定改写预算内执行 Reviewer/Reviser。"""

    def __init__(self, policy: ReviewPolicy) -> None:
        self.policy = policy

    def run(
        self,
        candidate: _T,
        *,
        review: Callable[[_T, int], ReviewDecision],
        revise: Callable[[_T, ReviewDecision, int], _T],
        on_transition: Callable[[ReviewTransition], None] | None = None,
    ) -> ReviewLoopResult[_T]:
        history: list[ReviewDecision] = []
        revisions = 0
        while True:
            try:
                decision = review(candidate, revisions)
            except Exception as exc:
                raise ReviewExecutionError("review", revisions, exc) from exc
            if decision.status == "revisable" and revisions >= self.policy.max_revisions:
                decision = replace(decision, status=self.policy.exhausted_status)
            history.append(decision)
            if on_transition is not None:
                on_transition(ReviewTransition("review", revisions, decision))
            if decision.status in {"passed", "blocked"}:
                return ReviewLoopResult(candidate, decision, revisions, tuple(history))
            try:
                candidate = revise(candidate, decision, revisions + 1)
            except Exception as exc:
                raise ReviewExecutionError("revise", revisions + 1, exc) from exc
            revisions += 1
            if on_transition is not None:
                on_transition(ReviewTransition("revise", revisions, decision))


__all__ = [
    "ReviewDecision",
    "ReviewExecutionError",
    "ReviewLoop",
    "ReviewLoopResult",
    "ReviewPolicy",
    "ReviewStatus",
    "ReviewTransition",
]
