from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.core.context.errors import ContextInputMissingError
from agentkit.core.context.models import ContextDefinitionModel, ContextInputModel


def test_context_definition_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ContextDefinitionModel.model_validate(
            {
                "id": "runtime.intent",
                "version": 1,
                "owner": "runtime",
                "templates": {"system": "system.md", "user": "user.md"},
                "limits": {"max_input_tokens": 1000, "response_reserve_tokens": 200},
                "unexpected": True,
            }
        )


def test_context_input_uses_deterministic_defaults() -> None:
    value = ContextInputModel.model_validate(
        {
            "name": "message",
            "source": "request.message",
            "required": True,
            "priority": 100,
            "max_chars": 2000,
        }
    )

    assert value.serializer == "text"
    assert value.truncate == "tail"


def test_context_errors_expose_stable_code() -> None:
    error = ContextInputMissingError("runtime.intent", "message")

    assert error.code == "context_input_missing"
    assert error.context_id == "runtime.intent"
    assert "message" in str(error)
