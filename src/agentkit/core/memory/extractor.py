"""Durable-fact extraction for long-term memory.

After a turn, the extractor asks the LLM to distill durable facts/preferences
about the user as a JSON array of short strings. Extraction is best-effort:
any parsing/LLM failure yields an empty list so a bad extraction never breaks
the chat turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

ChatFn = Callable[[str, str], str]

_SYSTEM_PROMPT = (
    "You extract durable, reusable facts about the USER from a single exchange "
    "(stable preferences, identity details, long-lived constraints or goals). "
    "Ignore transient chit-chat and assistant content. Reply with ONLY a JSON "
    'array of short strings, e.g. ["the user prefers email", "the user is in Tokyo"]. '
    "If there is nothing durable, reply with []."
)


def _parse_array(raw: str) -> list[str]:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\[.*\]", text, flags=re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    return [item.strip() for item in data if isinstance(item, str) and item.strip()]


class MemoryExtractor:
    def __init__(self, *, chat_fn: ChatFn | None = None) -> None:
        self._chat_fn = chat_fn

    def _chat(self) -> ChatFn:
        if self._chat_fn is not None:
            return self._chat_fn
        from agentkit.core import llm_client

        return llm_client.require_chat

    def extract(self, *, user_text: str, assistant_text: str) -> list[str]:
        user = f"USER:\n{user_text.strip()}\n\nASSISTANT:\n{assistant_text.strip()}"
        try:
            raw = self._chat()(_SYSTEM_PROMPT, user)
        except Exception:
            return []
        return _parse_array(raw)
