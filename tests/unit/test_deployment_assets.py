from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_docker_image_contains_current_declarative_runtime_assets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY agents ./agents" in dockerfile
    assert "COPY contexts ./contexts" in dockerfile
    assert "COPY skills ./skills" in dockerfile
    assert "COPY prompts ./prompts" not in dockerfile


def test_compose_mounts_current_declarative_runtime_assets() -> None:
    for filename in ("docker-compose.yml", "docker-compose.external.yml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")

        assert "./agents:/app/agents:ro" in compose
        assert "./contexts:/app/contexts:ro" in compose
        assert "./skills:/app/skills:ro" in compose
        assert "./prompts:/app/prompts:ro" not in compose
