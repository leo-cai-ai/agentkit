"""通过 Context Pack 维护会话滚动摘要。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentkit.core.context.models import ContextRenderRequest


class Summarizer:
    """把超出短期窗口的历史折叠为简洁摘要。"""

    def __init__(
        self,
        *,
        context_invoker: Any,
        tenant_selector: str,
        max_tokens: int = 10_000,
    ) -> None:
        self._context_invoker = context_invoker
        self._tenant_selector = tenant_selector
        self._max_tokens = max(1, int(max_tokens))

    def fold(
        self,
        *,
        tenant_id: str,
        run_id: str,
        existing_summary: str,
        turns: Sequence[dict[str, Any]],
    ) -> str:
        if not turns:
            return existing_summary
        result = self._context_invoker.invoke_text(
            ContextRenderRequest(
                context_id="runtime.memory-summary",
                tenant_id=tenant_id,
                tenant_selector=self._tenant_selector,
                run_id=run_id,
                agent=None,
                skill=None,
                values={
                    "memory.summary_window": {
                        "existing_summary": (existing_summary or "").strip(),
                        "turns": list(turns),
                    }
                },
                global_token_limit=self._max_tokens,
            )
        )
        return str(result.value).strip()


__all__ = ["Summarizer"]
