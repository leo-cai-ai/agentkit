"""Prompt files / personas are actually injected into the LLM nodes."""

from agentkit.core.governance import (
    DEFAULT_OUTPUT_REVIEW_SYSTEM,
    DEFAULT_PLAN_REVIEW_SYSTEM,
    OutputReviewer,
    PlanReviewer,
)
from agentkit.core.intent import DEFAULT_INTENT_SYSTEM, IntentDecomposer
from agentkit.core.prompt_library import PromptLibrary
from agentkit.core.registry import AgentRegistry, SkillRegistry
from agentkit.core.router import IntentRouter


def test_intent_default_without_library():
    node = IntentDecomposer(tenant_config={})
    assert node._llm_system_prompt() == DEFAULT_INTENT_SYSTEM


def test_intent_node_override_is_injected():
    library = PromptLibrary(overrides={"intent": "CUSTOM INTENT PROMPT"})
    node = IntentDecomposer(tenant_config={}, prompt_library=library)
    assert node._llm_system_prompt() == "CUSTOM INTENT PROMPT"


def test_router_persona_is_prepended():
    library = PromptLibrary(personas={"router": "ROUTER PERSONA"})
    node = IntentRouter(
        agents=AgentRegistry(),
        skills=SkillRegistry(),
        prompt_library=library,
    )
    prompt = node._llm_system_prompt()
    assert prompt.startswith("ROUTER PERSONA\n\n")
    assert "能力解析节点" in prompt


def test_plan_review_override_is_injected():
    library = PromptLibrary(overrides={"plan_review": "CUSTOM PLAN REVIEW"})
    node = PlanReviewer({}, prompt_library=library)
    assert node._llm_system_prompt() == "CUSTOM PLAN REVIEW"


def test_output_review_default_without_library():
    assert OutputReviewer({})._llm_system_prompt() == DEFAULT_OUTPUT_REVIEW_SYSTEM


def test_plan_review_default_constant_matches():
    assert PlanReviewer({})._llm_system_prompt() == DEFAULT_PLAN_REVIEW_SYSTEM
