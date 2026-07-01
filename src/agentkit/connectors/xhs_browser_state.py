"""Shared browser-state signals for Xiaohongshu adapters."""

from __future__ import annotations

XHS_PHONE_VERIFICATION_PATTERN = (
    r"\u624b\u673a(?:\u53f7)?\u9a8c\u8bc1|"
    r"\u9a8c\u8bc1\u624b\u673a(?:\u53f7)?|"
    r"\u77ed\u4fe1\u9a8c\u8bc1\u7801|"
    r"\u8bf7\u8f93\u5165(?:\u77ed\u4fe1)?\u9a8c\u8bc1\u7801|"
    r"\u83b7\u53d6\u9a8c\u8bc1\u7801|"
    r"\u53d1\u9001\u9a8c\u8bc1\u7801|"
    r"\u8eab\u4efd\u9a8c\u8bc1|"
    r"\u5b89\u5168\u624b\u673a\u53f7"
)

__all__ = ["XHS_PHONE_VERIFICATION_PATTERN"]
