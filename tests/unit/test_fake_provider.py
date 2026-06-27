import pytest

from agentkit.llm.base import LLMProvider, LLMRequiredError, extract_text
from agentkit.llm.fake import FakeProvider


def test_fake_is_llmprovider():
    fp = FakeProvider(responses=["x"])
    assert isinstance(fp, LLMProvider)
    assert fp.name == "fake"


def test_fake_queue():
    fp = FakeProvider(responses=["a", "b"])
    assert fp.complete("s", "u") == "a"
    assert fp.complete("s", "u") == "b"
    with pytest.raises(LLMRequiredError):
        fp.complete("s", "u")


def test_fake_responder_dispatches_on_inputs():
    fp = FakeProvider(responder=lambda system, user: f"{system}|{user}")
    assert fp.complete("S", "U") == "S|U"


def test_extract_text_handles_list_parts():
    class R:
        content = [{"text": "he"}, "llo"]

    assert extract_text(R()) == "hello"

    class R2:
        content = "plain"

    assert extract_text(R2()) == "plain"
