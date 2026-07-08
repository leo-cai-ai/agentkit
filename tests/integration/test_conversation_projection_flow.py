from tests.unit.test_multi_agent_service import _prepared_request, _projection_service


def test_general_success_projects_one_canonical_turn(tmp_path) -> None:
    service, _, _, _, contexts, projection = _projection_service(tmp_path)
    request = _prepared_request(
        projection,
        message="你好",
        client_message_id="integration-1",
    )

    response = service.handle(request)

    timeline = projection.timeline(
        conversation_id=response.conversation_id,
        tenant_id="tenant-a",
        user_id="u1",
    )
    turn = timeline.turns[0]
    attempt = turn["attempts"][0]
    assert turn["canonical_attempt_id"] == attempt["id"]
    assert attempt["status"] == "succeeded"
    assert [item["content"] for item in attempt["messages"]] == [
        "我是 General Agent，可以协调业务助手。"
    ]
    assert contexts.builds[0]["exclude_turn_id"] == turn["id"]
