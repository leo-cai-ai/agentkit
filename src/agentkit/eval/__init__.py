"""LLM evaluation harness: golden datasets, checks, LLM-as-judge, regression gate."""

from __future__ import annotations

from .case import CaseResult, CheckOutcome, CheckSpec, EvalCase, EvalReport
from .checks import run_check
from .dataset import load_cases
from .judge import JudgeResult, LLMJudge
from .runner import run_case, run_eval
from .targets import extract_text, llm_target, make_gateway_target, make_gateway_trace_target

__all__ = [
    "CheckSpec",
    "EvalCase",
    "CheckOutcome",
    "CaseResult",
    "EvalReport",
    "run_check",
    "load_cases",
    "JudgeResult",
    "LLMJudge",
    "run_case",
    "run_eval",
    "llm_target",
    "make_gateway_target",
    "make_gateway_trace_target",
    "extract_text",
]
