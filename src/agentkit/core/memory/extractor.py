"""通过受治理 Context Pack 提取长期稳定事实。"""

from __future__ import annotations

from typing import Any

from agentkit.core.context.models import ContextRenderRequest


class MemoryExtractor:
    """尽力提取事实；模型或解析失败不会中断业务事务。"""

    def __init__(
        self,
        *,
        context_invoker: Any,
        tenant_selector: str,
        max_tokens: int = 8000,
    ) -> None:
        self._context_invoker = context_invoker
        self._tenant_selector = tenant_selector
        self._max_tokens = max(1, int(max_tokens))

    def extract(
        self,
        *,
        tenant_id: str,
        run_id: str,
        user_text: str,
        assistant_text: str,
    ) -> list[str]:
        try:
            value = self._context_invoker.invoke_json(
                ContextRenderRequest(
                    context_id="runtime.memory-extract",
                    tenant_id=tenant_id,
                    tenant_selector=self._tenant_selector,
                    run_id=run_id,
                    agent=None,
                    skill=None,
                    values={
                        "memory.exchange": {
                            "user": user_text.strip(),
                            "assistant": assistant_text.strip(),
                        }
                    },
                    global_token_limit=self._max_tokens,
                )
            ).value
        except Exception:
            return []
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]


__all__ = ["MemoryExtractor"]
