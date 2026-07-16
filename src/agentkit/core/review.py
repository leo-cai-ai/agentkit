"""与业务无关的有限审核门禁状态机。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, Protocol, TypeVar

from .safety import ContentSafetyGuard

ReviewStatus = Literal["passed", "revisable", "blocked"]
OutputReviewAction = Literal["pass", "flag", "block"]
_T = TypeVar("_T")


@dataclass(frozen=True)
class OutputReviewContext:
    """通用输出审查所需的最小、无业务耦合上下文。"""

    tenant_id: str
    run_id: str
    agent_id: str
    strategy: str
    status: str
    output: dict[str, Any]


@dataclass(frozen=True)
class OutputReviewFinding:
    """可安全写入审计日志的结构化审查发现。"""

    code: str
    severity: Literal["low", "medium", "high"]
    message: str
    path: str = ""


@dataclass(frozen=True)
class OutputReviewResult:
    """审查动作、处理后的输出和非敏感发现。"""

    action: OutputReviewAction
    output: dict[str, Any]
    findings: tuple[OutputReviewFinding, ...] = ()


class OutputReviewer(Protocol):
    """输出审查器扩展点；实现不得触发业务流程重跑。"""

    def review(self, context: OutputReviewContext) -> OutputReviewResult: ...


class OutputReviewChain:
    """按注册顺序执行输出审查，默认异常时关闭发布通道。"""

    def __init__(
        self,
        reviewers: list[OutputReviewer] | tuple[OutputReviewer, ...],
        *,
        fail_closed: bool = True,
    ) -> None:
        self._reviewers = tuple(reviewers)
        self._fail_closed = fail_closed

    def review(self, context: OutputReviewContext) -> OutputReviewResult:
        current = context
        action: OutputReviewAction = "pass"
        findings: list[OutputReviewFinding] = []
        for reviewer in self._reviewers:
            try:
                result = reviewer.review(current)
            except Exception:  # noqa: BLE001 - 审查器必须转换成稳定的治理结果
                findings.append(
                    OutputReviewFinding(
                        code="reviewer.error",
                        severity="high" if self._fail_closed else "medium",
                        message="输出审查器执行失败。",
                    )
                )
                return OutputReviewResult(
                    action="block" if self._fail_closed else "flag",
                    output=current.output,
                    findings=tuple(findings),
                )
            findings.extend(result.findings)
            current = replace(current, output=result.output)
            if result.action == "block":
                return OutputReviewResult("block", current.output, tuple(findings))
            if result.action == "flag":
                action = "flag"
        return OutputReviewResult(action, current.output, tuple(findings))


class OutputSafetyReviewer:
    """递归清理最终输出中的敏感信息，不改变输出结构。"""

    def __init__(self, guard: ContentSafetyGuard) -> None:
        self._guard = guard

    def review(self, context: OutputReviewContext) -> OutputReviewResult:
        findings: list[OutputReviewFinding] = []
        sanitized = _sanitize_output_value(context.output, "$", self._guard, findings)
        return OutputReviewResult(
            action="flag" if findings else "pass",
            output=dict(sanitized),
            findings=tuple(findings),
        )


def _sanitize_output_value(
    value: Any,
    path: str,
    guard: ContentSafetyGuard,
    findings: list[OutputReviewFinding],
) -> Any:
    if isinstance(value, str):
        sanitized, safety_findings = guard.sanitize_output(value)
        findings.extend(
            OutputReviewFinding(
                code=f"safety.{finding.category}.{finding.label}",
                severity=finding.severity,
                message="输出中包含已脱敏的敏感信息。",
                path=path,
            )
            for finding in safety_findings
        )
        return sanitized
    if isinstance(value, dict):
        return {
            key: _sanitize_output_value(item, f"{path}.{key}", guard, findings)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_output_value(item, f"{path}[{index}]", guard, findings)
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            _sanitize_output_value(item, f"{path}[{index}]", guard, findings)
            for index, item in enumerate(value)
        )
    return value


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
    "OutputReviewAction",
    "OutputReviewChain",
    "OutputReviewContext",
    "OutputReviewFinding",
    "OutputReviewer",
    "OutputReviewResult",
    "OutputSafetyReviewer",
    "ReviewDecision",
    "ReviewExecutionError",
    "ReviewLoop",
    "ReviewLoopResult",
    "ReviewPolicy",
    "ReviewStatus",
    "ReviewTransition",
]
