"""Content safety guardrails: PII redaction, prompt-injection detection, moderation.

These are dependency-free, deterministic primitives that complement the LLM
governance layer (plan/output review, approval). They run at well-defined seams:

- **Input** (gateway / chat): detect prompt-injection attempts and PII before the
  request reaches the model. Injection can be *flagged* (annotated + audited) or
  *blocked* (refused without an LLM call, which also saves cost).
- **Output**: detect/redact PII in generated text (utility; callers decide whether
  to mutate, since streamed text has already been delivered to the client).

Detection is heuristic by design and tuned to minimise false positives on the
"block" path: only high-severity injection findings can block. A pluggable
``ModerationProvider`` seam lets deployments add an external moderation service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SafetyFinding:
    category: str  # "pii" | "prompt_injection" | "moderation"
    label: str  # e.g. "email", "credit_card", "instruction_override"
    severity: str = "medium"  # "low" | "medium" | "high"
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "label": self.label,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InputDecision:
    action: str  # "allow" | "flag" | "block"
    findings: tuple[SafetyFinding, ...] = ()
    reason: str = ""

    def to_audit(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# PII detection / redaction
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Candidate card-like runs of 13-19 digits with optional space/dash separators.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# Common credential/token shapes (overlaps the no-hardcoded-credentials rule).
_SECRET_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_pii(text: str) -> list[SafetyFinding]:
    """Return PII findings without mutating ``text`` (deterministic, ordered)."""
    findings: list[SafetyFinding] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, severity: str, sample: str) -> None:
        key = (label, sample)
        if key in seen:
            return
        seen.add(key)
        findings.append(SafetyFinding("pii", label, severity, detail=_mask(sample)))

    for label, pattern in _SECRET_RES:
        for m in pattern.finditer(text):
            add(label, "high", m.group(0))
    for m in _EMAIL_RE.finditer(text):
        add("email", "medium", m.group(0))
    for m in _SSN_RE.finditer(text):
        add("ssn", "high", m.group(0))
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            add("credit_card", "high", m.group(0))
    for m in _IPV4_RE.finditer(text):
        add("ip_address", "low", m.group(0))
    return findings


def _mask(sample: str) -> str:
    """Keep only the last 2 visible chars; never echo the full secret in audit."""
    s = sample.strip()
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 2) + s[-2:]


def redact_pii(text: str) -> tuple[str, list[SafetyFinding]]:
    """Replace detected PII with ``[REDACTED:<label>]`` placeholders."""
    findings = find_pii(text)
    redacted = text
    # Replace longer/structured matches first to avoid partial overlaps.
    for label, pattern in _SECRET_RES:
        redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    redacted = _EMAIL_RE.sub("[REDACTED:email]", redacted)
    redacted = _SSN_RE.sub("[REDACTED:ssn]", redacted)

    def _card_sub(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[REDACTED:credit_card]"
        return m.group(0)

    redacted = _CARD_RE.sub(_card_sub, redacted)
    redacted = _IPV4_RE.sub("[REDACTED:ip_address]", redacted)
    return redacted, findings


# --------------------------------------------------------------------------- #
# Prompt-injection detection
# --------------------------------------------------------------------------- #

# (label, severity, pattern). High-severity patterns are block-worthy.
_INJECTION_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        "high",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b"
            r"(?:previous|prior|above|earlier|all)\b[^.\n]{0,20}"
            r"\b(?:instruction|instructions|prompt|prompts|rules|context)\b"
        ),
    ),
    (
        "system_prompt_exfiltration",
        "high",
        re.compile(
            r"(?i)\b(?:reveal|show|print|repeat|expose|leak)\b[^.\n]{0,30}"
            r"\b(?:system\s*prompt|system\s*message|your\s*instructions|prompt)\b"
        ),
    ),
    (
        "role_override",
        "medium",
        re.compile(r"(?i)\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\b"),
    ),
    (
        "jailbreak",
        "high",
        re.compile(r"(?i)\b(?:jailbreak|developer\s*mode|do\s+anything\s+now|DAN)\b"),
    ),
    # Chinese variants.
    (
        "instruction_override",
        "high",
        re.compile(
            r"(忽略|无视|忘记)[^。\n]{0,20}(以上|之前|前面|所有)?[^。\n]{0,10}(指令|提示|规则|要求)"
        ),
    ),
    (
        "system_prompt_exfiltration",
        "high",
        re.compile(r"(泄露|显示|输出|打印|重复)[^。\n]{0,15}(系统)?(提示词|提示|指令)"),
    ),
    ("role_override", "medium", re.compile(r"你现在是|扮演|假装(你)?是")),
    ("jailbreak", "high", re.compile(r"越狱|开发者模式")),
)


def find_prompt_injection(text: str) -> list[SafetyFinding]:
    findings: list[SafetyFinding] = []
    seen: set[str] = set()
    for label, severity, pattern in _INJECTION_RULES:
        m = pattern.search(text)
        if m and label not in seen:
            seen.add(label)
            findings.append(
                SafetyFinding("prompt_injection", label, severity, detail=m.group(0)[:80])
            )
    return findings


# --------------------------------------------------------------------------- #
# Moderation seam
# --------------------------------------------------------------------------- #


class ModerationProvider(Protocol):
    def check(self, text: str) -> list[SafetyFinding]: ...


class NullModerationProvider:
    """Default no-op moderation; deployments may swap in an external service."""

    def check(self, text: str) -> list[SafetyFinding]:
        return []


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #


@dataclass
class ContentSafetyGuard:
    enabled: bool = True
    block_on_injection: bool = False
    detect_pii: bool = True
    moderation: ModerationProvider = field(default_factory=NullModerationProvider)

    def inspect_input(self, text: str) -> InputDecision:
        if not self.enabled or not text:
            return InputDecision("allow")
        findings: list[SafetyFinding] = list(find_prompt_injection(text))
        if self.detect_pii:
            findings.extend(find_pii(text))
        findings.extend(self.moderation.check(text))
        if not findings:
            return InputDecision("allow")
        high_injection = any(
            f.category == "prompt_injection" and f.severity == "high" for f in findings
        )
        high_moderation = any(f.category == "moderation" and f.severity == "high" for f in findings)
        if self.block_on_injection and (high_injection or high_moderation):
            return InputDecision(
                "block",
                tuple(findings),
                reason="High-severity prompt-injection or moderation signal detected.",
            )
        return InputDecision("flag", tuple(findings), reason="Content safety findings present.")

    def inspect_output(self, text: str) -> list[SafetyFinding]:
        if not self.enabled or not text or not self.detect_pii:
            return []
        return find_pii(text)

    def sanitize_output(self, text: str) -> tuple[str, list[SafetyFinding]]:
        if not self.enabled or not text or not self.detect_pii:
            return text, []
        return redact_pii(text)


def build_safety_guard(settings: Any = None) -> ContentSafetyGuard:
    """Build a guard from settings (lazily importing the global settings)."""
    if settings is None:
        try:
            from agentkit.config import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 - settings optional in lightweight tests
            settings = None
    return ContentSafetyGuard(
        enabled=bool(getattr(settings, "safety_enabled", True)),
        block_on_injection=bool(getattr(settings, "safety_block_on_injection", False)),
        detect_pii=bool(getattr(settings, "safety_detect_pii", True)),
    )


REFUSAL_MESSAGE = (
    "This request was blocked by the content-safety guard because it looks like an "
    "attempt to override the assistant's instructions. Please rephrase your request."
)


__all__ = [
    "SafetyFinding",
    "InputDecision",
    "ContentSafetyGuard",
    "ModerationProvider",
    "NullModerationProvider",
    "find_pii",
    "redact_pii",
    "find_prompt_injection",
    "build_safety_guard",
    "REFUSAL_MESSAGE",
]
