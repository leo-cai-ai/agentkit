"""Eval 报告 Web 展示页集成测试。

覆盖：报告列表、门禁状态、逐 Case 明细、基线对比与空状态。
"""

from __future__ import annotations

import json

import pytest

import agentkit.config as config_mod


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    monkeypatch.setenv("AGENTKIT_LLM_PROVIDER", "fake")
    config_mod.get_settings.cache_clear()

    import agentkit.runtime.bootstrap as bootstrap_mod
    import agentkit.web.app as web_app
    from agentkit.web.app import app, clear_runtime_cache
    from agentkit.web.security import configure_security

    monkeypatch.setattr(bootstrap_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web_app, "AGENTKIT_ROOT", tmp_path)
    configure_security(app)
    clear_runtime_cache()
    yield app.test_client()
    clear_runtime_cache()
    config_mod.get_settings.cache_clear()


def _login(client) -> None:
    resp = client.post("/login", data={"token": "secret-token"})
    assert resp.status_code == 302


def _report_payload(*, passed: bool) -> dict:
    return {
        "schema_version": "1.0",
        "created_at": "2026-08-08T10:00:00+00:00",
        "suite": {"id": "strategy-trajectory", "version": "1"},
        "target": "gateway-trace",
        "environment": {
            "tenant_id": "AI-ABC",
            "provider": "fake",
            "model": "gpt-x",
            "git_commit": "abc123",
            "context_manifest_hash": "ctx-hash-1",
        },
        "datasets": {"../datasets/trajectory.jsonl": "sha256:deadbeef"},
        "execution": {"repetitions": 1, "concurrency": 1},
        "gate": {
            "passed": passed,
            "min_pass_rate": 1.0,
            "min_mean_score": 1.0,
        },
        "summary": {
            "total": 2,
            "passed": 2 if passed else 1,
            "pass_rate": 1.0 if passed else 0.5,
            "mean_score": 0.95,
        },
        "results": [
            {
                "case_id": "direct-customer-answer",
                "attempt": 1,
                "passed": True,
                "score": 1.0,
                "tags": ["direct"],
                "outcomes": [
                    {
                        "type": "json_path_equals",
                        "passed": True,
                        "score": 1.0,
                        "detail": "",
                        "weight": 1.0,
                        "skipped": False,
                    }
                ],
                "output": "ok",
            },
            {
                "case_id": "workflow-refund",
                "attempt": 1,
                "passed": passed,
                "score": 0.9 if passed else 0.4,
                "tags": ["workflow"],
                "outcomes": [
                    {
                        "type": "event_sequence",
                        "passed": passed,
                        "score": 0.9 if passed else 0.4,
                        "detail": "missing run_paused" if not passed else "",
                        "weight": 1.0,
                        "skipped": False,
                    }
                ],
                "output": "paused",
            },
        ],
    }


def _write_report(root, name: str, payload: dict) -> None:
    reports = root / "evaluation" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / name).write_text(json.dumps(payload), encoding="utf-8")


def test_eval_page_lists_reports_and_case_detail(client, tmp_path) -> None:
    _login(client)
    _write_report(
        tmp_path,
        "strategy-trajectory-20260808T100000.json",
        _report_payload(passed=True),
    )
    _write_report(
        tmp_path,
        "strategy-trajectory-20260808T090000.json",
        _report_payload(passed=False),
    )

    response = client.get("/evaluations")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "strategy-trajectory" in html
    assert "direct-customer-answer" in html
    assert "workflow-refund" in html
    assert "json_path_equals" in html
    assert "event_sequence" in html
    assert "PASS" in html
    assert "gateway-trace" in html
    # 最新（通过）报告默认选中，门禁显示达标。
    assert "100.0%" in html
    assert "ctx-hash-1" in html


def test_eval_page_selects_specific_report(client, tmp_path) -> None:
    _login(client)
    _write_report(tmp_path, "a-20260808T100000.json", _report_payload(passed=False))
    _write_report(tmp_path, "b-20260808T110000.json", _report_payload(passed=True))

    response = client.get("/evaluations?report=a-20260808T100000.json")
    html = response.get_data(as_text=True)
    # 失败报告的 pass rate 50% 可见。
    assert "50.0%" in html
    assert "missing run_paused" in html


def test_eval_page_empty_state(client) -> None:
    _login(client)
    response = client.get("/evaluations")
    assert response.status_code == 200
    assert "暂无 Eval 报告" in response.get_data(as_text=True)


def test_eval_page_nav_entry_present(client) -> None:
    _login(client)
    html = client.get("/evaluations").get_data(as_text=True)
    assert 'href="/evaluations"' in html
    assert "flask" in html
