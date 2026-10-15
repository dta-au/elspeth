"""Authenticated retained failure outcomes survive browser custody and later turns."""

from uuid import uuid4

import httpx
import pytest
from tests.helpers.composer_operations import build_composer_operation_app, message_body


async def _stop_queued(client: httpx.AsyncClient, session_id, operation_id: str) -> None:
    admitted = await client.post(
        f"/api/sessions/{session_id}/messages",
        json=message_body("Synthetic private authoring content", operation_id=operation_id),
    )
    assert admitted.status_code == 202, admitted.text
    stopped = await client.post(f"/api/sessions/{session_id}/operations/{operation_id}/cancel")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_retained_failure_history_is_typed_private_and_independent_of_browser_custody(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path)
    operation_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test", headers={"Authorization": f"Bearer {setup.token}"}
    ) as client:
        await _stop_queued(client, setup.session_id, operation_id)
        history = await client.get(f"/api/sessions/{setup.session_id}/operations")
        assert history.status_code == 200, history.text
        assert history.headers["cache-control"] == "no-store"
        page = history.json()
        assert set(page) == {"session_id", "items", "next_cursor"}
        assert page["session_id"] == str(setup.session_id)
        assert page["next_cursor"] is None
        (outcome,) = page["items"]
        assert set(outcome) == {
            "session_id",
            "operation_id",
            "kind",
            "user_message_id",
            "settled_at",
            "failure_code",
            "error_type",
            "diagnostic_id",
            "request_id",
            "saved_revisions",
        }
        assert outcome["operation_id"] == operation_id
        assert outcome["failure_code"] == "request_cancelled"
        assert outcome["user_message_id"] is None
        assert outcome["diagnostic_id"] is None
        assert outcome["request_id"] is not None
        assert outcome["saved_revisions"] == {
            "attribution": "unstarted",
            "count": 0,
            "latest_state_id": None,
            "latest_version": None,
        }
        assert "Synthetic private authoring content" not in history.text
        assert "body" not in outcome
    # A new authenticated browser client has no submission descriptor to replay.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test", headers={"Authorization": f"Bearer {setup.token}"}
    ) as reloaded:
        retained = await reloaded.get(f"/api/sessions/{setup.session_id}/operations")
        assert retained.status_code == 200
        assert retained.json() == page
    assert setup.composer.calls == 0


@pytest.mark.asyncio
async def test_history_keyset_paging_and_exact_lookup_stay_session_scoped(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path)
    operation_ids = [str(uuid4()) for _ in range(3)]
    other = await setup.app.state.session_service.create_session(
        user_id="transport-user", title="Separate owned session", auth_provider_type="local"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test", headers={"Authorization": f"Bearer {setup.token}"}
    ) as client:
        for operation_id in operation_ids:
            await _stop_queued(client, setup.session_id, operation_id)
        endpoint = f"/api/sessions/{setup.session_id}/operations"
        first = await client.get(endpoint, params={"limit": 2})
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert len(first_page["items"]) == 2
        assert first_page["next_cursor"] is not None
        second = await client.get(endpoint, params={"limit": 2, "cursor": first_page["next_cursor"]})
        assert second.status_code == 200
        second_page = second.json()
        assert second_page["next_cursor"] is None
        actual_ids = [item["operation_id"] for item in first_page["items"] + second_page["items"]]
        assert set(actual_ids) == set(operation_ids)
        assert len(actual_ids) == len(set(actual_ids))
        exact = await client.get(endpoint, params={"operation_id": operation_ids[0]})
        assert exact.status_code == 200
        assert [item["operation_id"] for item in exact.json()["items"]] == [operation_ids[0]]
        foreign = await client.get(f"/api/sessions/{other.id}/operations", params={"operation_id": operation_ids[0]})
        assert foreign.status_code == 200
        assert foreign.json()["items"] == []
        for params in [
            {"limit": 0},
            {"limit": 101},
            {"cursor": "malformed"},
            {"cursor": first_page["next_cursor"], "operation_id": operation_ids[0]},
        ]:
            rejected = await client.get(endpoint, params=params)
            assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_history_requires_authentication_and_owned_session(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url="http://test") as client:
        denied = await client.get(f"/api/sessions/{setup.session_id}/operations")
        assert denied.status_code == 401
        missing = await client.get(f"/api/sessions/{uuid4()}/operations", headers={"Authorization": f"Bearer {setup.token}"})
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"
