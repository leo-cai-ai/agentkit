"""Pluggable token estimation.

The estimate is deliberately approximate: it exists to keep prompts under a
budget (avoid blowing the context window), not to bill tokens. CJK text packs
far fewer characters per token than ASCII, so the heuristic weights CJK code
points separately.
"""

from __future__ import annotations

import math
from typing import Protocol


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # CJK Extension A
        or 0x3040 <= code <= 0x30FF  # Hiragana + Katakana
        or 0xAC00 <= code <= 0xD7A3  # Hangul syllables
        or 0xF900 <= code <= 0xFAFF  # CJK Compatibility Ideographs
    )


class HeuristicTokenEstimator:
    """Character-based estimator with a heavier weight for CJK code points."""

    def __init__(self, *, chars_per_token: float = 4.0, cjk_tokens_per_char: float = 1.5) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        if cjk_tokens_per_char <= 0:
            raise ValueError("cjk_tokens_per_char must be positive")
        self._chars_per_token = chars_per_token
        self._cjk_tokens_per_char = cjk_tokens_per_char

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        cjk = 0
        other = 0
        for char in text:
            if _is_cjk(char):
                cjk += 1
            else:
                other += 1
        tokens = cjk * self._cjk_tokens_per_char + other / self._chars_per_token
        return int(math.ceil(tokens))
