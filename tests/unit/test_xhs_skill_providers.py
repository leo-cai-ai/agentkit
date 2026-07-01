from __future__ import annotations

from pathlib import Path

from agentkit.config import Settings
from agentkit.runtime.declarative_catalog import _load_entrypoint

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "skills" / "xhs-growth-campaign"


def test_xhs_skill_provider_applies_observation_only_to_headed_browser(tmp_path) -> None:
    build_provider = _load_entrypoint(
        REPO_ROOT,
        SKILL_ROOT,
        "scripts.providers:build_playwright_publishing_provider",
    )
    settings = Settings(_env_file=None, xhs_publishing_provider="playwright")
    common = {
        "browser_publish_observation_seconds": 15,
        "browser_profile_root": str(tmp_path / "profiles"),
        "publish_asset_root": str(tmp_path / "assets"),
        "publish_ledger_path": str(tmp_path / "publish.sqlite"),
    }

    headed = build_provider(settings, {**common, "browser_headless": "false"})
    headless = build_provider(settings, {**common, "browser_headless": "true"})

    assert headed.adapter.observation_seconds == 15.0
    assert headless.adapter.observation_seconds == 0.0
