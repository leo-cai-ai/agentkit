"""Context Pack 的稳定错误类型。"""

from __future__ import annotations


class ContextError(RuntimeError):
    """所有 Context Pack 运行时错误的基类。"""

    code = "context_error"

    def __init__(self, message: str, *, context_id: str = "") -> None:
        super().__init__(message)
        self.context_id = context_id


class ContextInputMissingError(ContextError):
    code = "context_input_missing"

    def __init__(self, context_id: str, input_name: str) -> None:
        super().__init__(f"{context_id}: 缺少必需输入 {input_name}", context_id=context_id)


class ContextTooLargeError(ContextError):
    code = "context_too_large"


class ContextRenderError(ContextError):
    code = "context_render_failed"


class ContextOutputInvalidError(ContextError):
    code = "model_output_invalid"


class ContextHashMismatchError(ContextError):
    code = "context_hash_mismatch"


__all__ = [
    "ContextError",
    "ContextHashMismatchError",
    "ContextInputMissingError",
    "ContextOutputInvalidError",
    "ContextRenderError",
    "ContextTooLargeError",
]
