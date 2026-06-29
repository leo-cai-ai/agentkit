"""Budget-aware short-term context assembly.

Builds the system/user prompt for a conversational turn within a token budget.
Assembly order (highest-priority fixed parts first):

    persona -> tools/skills catalog -> retrieved memories -> rolling summary ->
    recent turns (sliding window) -> current user message

When the assembled context exceeds the budget, the oldest recent turns are
folded into the rolling summary (via ``summarize_fn``) and dropped from the
window, repeating until the context fits or only the current message remains.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentkit.core.llm_client import strip_reasoning_tags

from .tokenizer import TokenEstimator

SummarizeFn = Callable[[str, Sequence[dict[str, Any]]], str]

_FOLD_BATCH = 2  # messages folded per iteration (~one turn)


@dataclass
class BuildResult:
    system_text: str
    user_text: str
    summary_text: str
    summary_changed: bool
    covered_through_message_id: int
    included_message_ids: list[int] = field(default_factory=list)
    estimated_tokens: int = 0


def _render_turns(turns: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for turn in turns:
        role = str(turn.get("role", "user"))
        content = str(turn.get("content", "")).strip()
        if role == "assistant":
            content = strip_reasoning_tags(content)
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


class ContextBuilder:
    def __init__(
        self,
        *,
        tokenizer: TokenEstimator,
        budget_tokens: int,
        window_turns: int = 6,
        summary_cap_tokens: int = 600,
        memory_cap_tokens: int = 600,
        knowledge_cap_tokens: int = 1000,
    ) -> None:
        self._tokenizer = tokenizer
        self._budget = max(1, budget_tokens)
        self._window_turns = max(1, window_turns)
        self._summary_cap = summary_cap_tokens
        self._memory_cap = memory_cap_tokens
        self._knowledge_cap = knowledge_cap_tokens

    @property
    def window_turns(self) -> int:
        return self._window_turns

    def build(
        self,
        *,
        persona: str = "",
        tool_catalog: str = "",
        retrieved_memories: Sequence[str] = (),
        retrieved_knowledge: Sequence[str] = (),
        summary: str = "",
        recent_messages: Sequence[dict[str, Any]] = (),
        current_text: str,
        summarize_fn: SummarizeFn,
        summary_covered_through_message_id: int = 0,
    ) -> BuildResult:
        # Apply the sliding window first: never consider more than window_turns*2 messages.
        working: list[dict[str, Any]] = list(recent_messages)[-(self._window_turns * 2) :]
        summary_cur = (summary or "").strip()
        covered_through = summary_covered_through_message_id
        summary_changed = False

        memories_text = self._truncate(
            "\n".join(f"- {m}" for m in retrieved_memories), self._memory_cap
        )
        knowledge_text = self._truncate(
            "\n".join(f"- {m}" for m in retrieved_knowledge), self._knowledge_cap
        )
        user_text = current_text

        while True:
            summary_capped = self._truncate(summary_cur, self._summary_cap)
            system_text = self._render(
                persona, tool_catalog, memories_text, knowledge_text, summary_capped, working
            )
            total = self._tokenizer.estimate(system_text) + self._tokenizer.estimate(user_text)
            if total <= self._budget or not working:
                break
            batch = working[:_FOLD_BATCH]
            working = working[_FOLD_BATCH:]
            summary_cur = summarize_fn(summary_cur, batch)
            summary_changed = True
            batch_max_id = max((int(m.get("id", 0)) for m in batch), default=covered_through)
            covered_through = max(covered_through, batch_max_id)

        summary_capped = self._truncate(summary_cur, self._summary_cap)
        system_text = self._render(
            persona, tool_catalog, memories_text, knowledge_text, summary_capped, working
        )
        estimated = self._tokenizer.estimate(system_text) + self._tokenizer.estimate(user_text)
        return BuildResult(
            system_text=system_text,
            user_text=user_text,
            summary_text=summary_cur,
            summary_changed=summary_changed,
            covered_through_message_id=covered_through,
            included_message_ids=[int(m.get("id", 0)) for m in working],
            estimated_tokens=estimated,
        )

    def _render(
        self,
        persona: str,
        tool_catalog: str,
        memories_text: str,
        knowledge_text: str,
        summary: str,
        working: Sequence[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        if persona.strip():
            parts.append(persona.strip())
        if tool_catalog.strip():
            parts.append("## Available tools & skills\n" + tool_catalog.strip())
        if memories_text.strip():
            parts.append("## Relevant memory\n" + memories_text.strip())
        if knowledge_text.strip():
            parts.append(
                "## Relevant knowledge\n"
                + knowledge_text.strip()
                + "\n\nUse this knowledge only when it is relevant. "
                "Cite the bracketed KB source ids in the answer when you rely on it."
            )
        if summary.strip():
            parts.append("## Conversation summary so far\n" + summary.strip())
        if working:
            parts.append("## Recent conversation\n" + _render_turns(working))
        return "\n\n".join(parts)

    def _truncate(self, text: str, cap_tokens: int) -> str:
        if cap_tokens <= 0 or not text:
            return text
        if self._tokenizer.estimate(text) <= cap_tokens:
            return text
        # Proportional first cut, then fine-trim until under the cap.
        estimate = max(1, self._tokenizer.estimate(text))
        target_len = max(1, int(len(text) * cap_tokens / estimate))
        trimmed = text[:target_len]
        while trimmed and self._tokenizer.estimate(trimmed) > cap_tokens:
            trimmed = trimmed[: max(0, len(trimmed) - 16)]
        return trimmed.rstrip() + " …"
