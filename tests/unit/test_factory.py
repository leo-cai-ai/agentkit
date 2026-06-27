import importlib

import pytest

from agentkit.config import Settings
from agentkit.llm.base import LLMRequiredError
from agentkit.llm.factory import build_provider


def test_build_fake():
    s = Settings(_env_file=None, llm_provider="fake")
    p = build_provider(s)
    assert p.name == "fake"


def test_build_customer_band_missing_creds_raises():
    # The customer_band provider module is kept local (gitignored); skip when absent.
    pytest.importorskip("agentkit.llm.customer_band")
    s = Settings(_env_file=None, llm_provider="customer_band")
    with pytest.raises(LLMRequiredError):
        build_provider(s)


def test_build_customer_band_passes_prebuilt_rate_limiter(monkeypatch):
    provider_mod = pytest.importorskip("agentkit.llm.customer_band")

    captured = {}

    class _FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(provider_mod, "CustomerBandProvider", _FakeProvider)
    s = Settings(
        _env_file=None,
        llm_provider="customer_band",
        llm_requests_per_second=3.0,
        llm_rate_limiter_enabled=True,
    )
    build_provider(s)
    # Factory now builds the rate limiter centrally and injects it, so the
    # provider receives a prebuilt limiter object (not raw rps/enabled flags).
    assert "rate_limiter" in captured
    assert captured["rate_limiter"] is not None
    assert captured["timeout_seconds"] == s.llm_timeout_seconds


def test_build_customer_band_rate_limiter_disabled(monkeypatch):
    provider_mod = pytest.importorskip("agentkit.llm.customer_band")

    captured = {}

    class _FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(provider_mod, "CustomerBandProvider", _FakeProvider)
    s = Settings(_env_file=None, llm_provider="customer_band", llm_rate_limiter_enabled=False)
    build_provider(s)
    assert captured["rate_limiter"] is None


def test_build_openai_missing_fields_raises():
    s = Settings(_env_file=None, llm_provider="openai")
    with pytest.raises(LLMRequiredError):
        build_provider(s)


def test_no_failover_without_fallbacks():
    s = Settings(_env_file=None, llm_provider="fake")
    p = build_provider(s)
    assert p.name == "fake"  # single provider, not wrapped


def test_failover_wraps_when_fallbacks_set(monkeypatch):
    import agentkit.llm.factory as factory

    class _Dummy:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(factory, "_build_single", lambda name, settings: _Dummy(name))
    s = Settings(_env_file=None, llm_provider="fake", llm_fallback_providers="b, c")
    p = build_provider(s)
    from agentkit.llm.resilient import FailoverProvider

    assert isinstance(p, FailoverProvider)
    assert [pr.name for pr in p.providers] == ["fake", "b", "c"]


def test_failover_dedupes_primary_and_skips_unbuildable(monkeypatch):
    import agentkit.llm.factory as factory

    class _Dummy:
        def __init__(self, name):
            self.name = name

    def _build(name, settings):
        if name == "broken":
            raise LLMRequiredError("missing creds")
        return _Dummy(name)

    monkeypatch.setattr(factory, "_build_single", _build)
    # "fake" duplicate of primary is dropped; "broken" is skipped -> single -> no wrap.
    s = Settings(_env_file=None, llm_provider="fake", llm_fallback_providers="fake, broken")
    p = build_provider(s)
    assert p.name == "fake"


def test_import_customer_band_module_is_side_effect_free():
    provider_mod = pytest.importorskip("agentkit.llm.customer_band")

    importlib.reload(provider_mod)
    assert hasattr(provider_mod, "CustomerBandProvider")
    assert not hasattr(provider_mod, "model")  # eager module-level model removed
