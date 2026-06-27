"""Customer-service domain pack.

Registers a conversational agent (no business skills/tools) intended to run in
``mode: "chat"`` so the runtime serves it through ``ConversationManager`` with
short-term + long-term memory. The agent's behavior is driven entirely by its
persona prompt plus retrieved/summarized conversation memory.
"""

from __future__ import annotations

from agentkit.core.contracts import AgentProfile
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry

DOMAIN = "support.customer_service"


def register(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    tenant_config: dict,
) -> None:
    prompt_file = tenant_config.get("prompt_files", {}).get("agents.customer_service", "")
    agents.register(
        AgentProfile(
            name="customer_service",
            domain=DOMAIN,
            description=(
                "Conversational customer-service assistant with short-term and " "long-term memory."
            ),
            allowed_skills=[],
            allowed_tools=[],
            prompt_file=prompt_file,
        )
    )
