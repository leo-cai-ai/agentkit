from agentkit.core.prompt_library import PromptLibrary


def test_system_returns_default_when_no_override():
    library = PromptLibrary()
    assert library.system("intent", "DEFAULT INTENT") == "DEFAULT INTENT"


def test_node_override_replaces_default():
    library = PromptLibrary(overrides={"intent": "CUSTOM INTENT"})
    assert library.system("intent", "DEFAULT INTENT") == "CUSTOM INTENT"


def test_persona_is_prepended_as_preamble():
    library = PromptLibrary(personas={"router": "You route requests."})
    result = library.system("intent", "DEFAULT", persona="router")
    assert result == "You route requests.\n\nDEFAULT"


def test_missing_persona_does_not_error_and_returns_base():
    library = PromptLibrary(personas={"general": "persona"})
    assert library.system("intent", "DEFAULT", persona="router") == "DEFAULT"
    assert library.system("intent", "DEFAULT", persona=None) == "DEFAULT"


def test_blank_persona_is_ignored():
    library = PromptLibrary(personas={"router": "   "})
    assert library.system("intent", "DEFAULT", persona="router") == "DEFAULT"


def test_override_and_persona_combine():
    library = PromptLibrary(
        overrides={"intent": "CUSTOM"},
        personas={"router": "Persona."},
    )
    assert library.system("intent", "DEFAULT", persona="router") == "Persona.\n\nCUSTOM"


def test_from_tenant_config_splits_nodes_and_agents():
    library = PromptLibrary.from_tenant_config(
        {
            "prompts": {
                "nodes.intent": "node override",
                "agents.router": "router persona",
                "agents.general": "general persona",
            }
        }
    )
    assert library.system("intent", "DEFAULT") == "node override"
    assert library.persona("router") == "router persona"
    assert library.persona("general") == "general persona"


def test_from_tenant_config_handles_missing_prompts():
    library = PromptLibrary.from_tenant_config({})
    assert library.system("intent", "DEFAULT") == "DEFAULT"
    assert library.persona("router") == ""
