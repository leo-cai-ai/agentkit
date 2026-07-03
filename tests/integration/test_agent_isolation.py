from concurrent.futures import ThreadPoolExecutor

from agentkit.core.contracts import TaskRequest
from tests.integration.test_unified_agent_graph import _build_gateway


def test_concurrent_runs_keep_state_isolated(tmp_path) -> None:
    gateway = _build_gateway(tmp_path)
    requests = [
        TaskRequest(
            user_id=f"u{index}",
            roles=[],
            text="执行",
            context={
                "agent": "customer_service",
                "skill": "customer_service.echo",
                "skill_args": {"marker": f"M-{index}"},
            },
        )
        for index in range(20)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(gateway.handle, requests))

    assert len({response.run_id for response in responses}) == 20
    assert [response.output["marker"] for response in responses] == [
        f"M-{index}" for index in range(20)
    ]
