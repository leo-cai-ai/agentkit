"""Rolling-summary summarizer.

Folds the oldest conversation turns into a running summary so the short-term
context stays under budget while older context is not lost outright (the full
transcript remains in the store).

The LLM call is injected as ``chat_fn`` (default: ``llm_client.require_chat``)
so the summarizer is trivially unit-testable with a deterministic stub.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

ChatFn = Callable[[str, str], str]

_SYSTEM_PROMPT = (
    "You maintain a concise running summary of a conversation between a user and "
    "an assistant. Merge the EXISTING SUMMARY with the NEW TURNS into a single "
    "updated summary. Preserve durable facts, user preferences, decisions, open "
    "questions, and identifiers. Be concise; drop pleasantries. Reply with the "
    "updated summary text only."
)


def _render_turns(turns: Sequence[dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        role = str(turn.get("role", "user"))
        content = str(turn.get("content", "")).strip()
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


class Summarizer:
    def __init__(self, *, chat_fn: ChatFn | None = None) -> None:
        self._chat_fn = chat_fn

    def _chat(self) -> ChatFn:
        if self._chat_fn is not None:
            return self._chat_fn
        from agentkit.core import llm_client

        return llm_client.require_chat

    def fold(self, *, existing_summary: str, turns: Sequence[dict[str, Any]]) -> str:
        if not turns:
            return existing_summary
        existing = (existing_summary or "").strip()
        user = (
            f"EXISTING SUMMARY:\n{existing or '(none)'}\n\n" f"NEW TURNS:\n{_render_turns(turns)}"
        )
        return self._chat()(_SYSTEM_PROMPT, user).strip()
