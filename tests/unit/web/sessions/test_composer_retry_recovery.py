"""SQLite route proofs for retry eligibility after an interrupted turn."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Request
from openai import APIConnectionError

from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.composer_operations import SettledComposerOperation, current_head_state_id, submit_and_settle
from tests.unit.web.sessions.test_freeform_route_custody import _file_app
from tests.unit.web.sessions.test_routes import (
    _compose_session_operation_context,
    _create_canonical_pipeline_route_proposal,
    _make_composer_mock,
    _save_test_composition_state,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("narration", ("", "I updated the pipeline name."))
async def test_partial_tool_turn_retries_from_current_state_without_replaying_tools(tmp_path, narration: str) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    composer = _make_composer_mock(response_text="Recovered reply.")
    app.state.composer_service = composer
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Partial turn"})).json()["id"])
            user = await service.add_message(session_id, "user", "Set the name once", writer_principal="route_user_message")
            state = await _save_test_composition_state(
                service, session_id, CompositionStateData(metadata_={"name": "Already set", "description": ""}), provenance="tool_call"
            )
            assistant = await service.add_message(
                session_id,
                "assistant",
                narration,
                writer_principal="compose_loop",
                tool_calls=[{"id": "call_done", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}],
                composition_state_id=state.id,
            )
            await service.add_message(
                session_id,
                "tool",
                '{"success":true}',
                writer_principal="compose_loop",
                tool_call_id="call_done",
                parent_assistant_id=assistant.id,
                composition_state_id=state.id,
            )
            before = await service.get_messages(session_id, limit=None)
            settled = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={"expected_user_message_id": str(user.id), "operation_id": str(uuid4()), "state_id": str(state.id)},
            )
            assert settled.final.status_code == 200, settled.final.text
            settled.result()
            composer.compose.assert_awaited_once()
            call = composer.compose.call_args
            assert call.args[0] == user.content
            assert call.args[2].metadata.name == "Already set"
            assert call.kwargs["current_state_id"] == str(state.id)
            assert call.kwargs["user_message_id"] == str(user.id)
            assert call.args[1] == [
                {
                    "role": "user",
                    "content": user.content,
                    "_elspeth_user_authored": True,
                    "_elspeth_user_message_id": str(user.id),
                },
                {"role": "assistant", "content": narration},
            ]
            assert all("tool_calls" not in message for message in call.args[1])
            after = await service.get_messages(session_id, limit=None)
            assert after[: len(before)] == before
            assert [row.id for row in after if row.role == "tool"] == [before[-1].id]
            assert len([row for row in after if row.role == "user"]) == 1
            again = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={
                    "expected_user_message_id": str(user.id),
                    "operation_id": str(uuid4()),
                    "state_id": await current_head_state_id(client, session_id),
                },
            )
            again_status, again_body = again.error()
            assert again_status == 409
            assert again_body["detail"]["error_type"] == "recompose_already_completed"
            composer.compose.assert_awaited_once()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_completed_send_response_lost_is_retrieved_and_cannot_be_recomposed(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Lost reply"})).json()["id"])
            request_id = str(uuid4())
            original_body = {"content": "Finish this", "operation_id": request_id, "state_id": None}
            sent = await submit_and_settle(client, app, path=f"/api/sessions/{session_id}/messages", body=original_body)
            sent_result = sent.result()
            rows = await service.get_messages(session_id, limit=None)
            user = next(row for row in rows if row.role == "user")
            replay = await client.post(f"/api/sessions/{session_id}/messages", json=original_body)
            assert replay.status_code == 202, replay.text
            replay_final = await client.get(f"/api/sessions/{session_id}/operations/{request_id}")
            assert replay_final.json() == sent.final.json()
            replay_record = app.state.composer_async_operation_authority.get(session_id=session_id, operation_id=request_id)
            assert replay_record is not None and replay_record.user_message_id == user.id
            retry = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={
                    "expected_user_message_id": str(user.id),
                    "operation_id": str(uuid4()),
                    "state_id": await current_head_state_id(client, session_id),
                },
            )
            status, body = retry.error()
            assert status == 409
            assert body["detail"]["error_type"] == "recompose_already_completed"
            history = await client.get(f"/api/sessions/{session_id}/messages")
            assert any(row["content"] == sent_result["message"]["content"] for row in history.json())
            assert await service.get_messages(session_id, limit=None) == rows
            assert composer.calls == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("cancelled", "provider_error"))
async def test_interrupted_provider_retry_preserves_accepted_identity(tmp_path, failure: str) -> None:
    app, service, engine, recovered_composer = _file_app(tmp_path)
    started = asyncio.Event()
    owners: list[asyncio.Task] = []

    class InterruptedComposer:
        async def compose(self, *args, **kwargs):
            owner = asyncio.current_task()
            assert owner is not None
            owners.append(owner)
            started.set()
            if failure == "provider_error":
                raise APIConnectionError(request=Request("POST", "http://offline.test"))
            await asyncio.Event().wait()

    app.state.composer_service = InterruptedComposer()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Interrupted provider"})).json()["id"])
            request_id = str(uuid4())
            accepted = await client.post(
                f"/api/sessions/{session_id}/messages",
                json={"content": "Recover", "operation_id": request_id, "state_id": None},
            )
            assert accepted.status_code == 202, accepted.text
            drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
            await asyncio.wait_for(started.wait(), timeout=10)
            if failure == "cancelled":
                # Local owner loss is the historical actor: no durable Stop marker.
                owners[0].cancel(COMPOSER_SHUTDOWN)
            await drive
            final = await client.get(f"/api/sessions/{session_id}/operations/{request_id}")
            interrupted = SettledComposerOperation(accepted, final)
            status, body = interrupted.error()
            if failure == "provider_error":
                assert status == 502, body
            else:
                assert status == 503, body
                assert final.json()["cancel_requested"] is False
            rows = await service.get_messages(session_id, limit=None)
            user = next(row for row in rows if row.role == "user")
            assert user.operation_id == UUID(request_id)
            app.state.composer_service = recovered_composer
            retry = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={
                    "expected_user_message_id": str(user.id),
                    "operation_id": str(uuid4()),
                    "state_id": await current_head_state_id(client, session_id),
                },
            )
            retry.result()
            assert recovered_composer.calls == 1
            assert [row.id for row in await service.get_messages(session_id, limit=None) if row.role == "user"] == [user.id]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_newer_user_identity_blocks_old_partial_turn(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Newer turn"})).json()["id"])
            old = await service.add_message(session_id, "user", "Old request", writer_principal="route_user_message")
            await service.add_message(session_id, "user", "New request", writer_principal="route_user_message")
            settled = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={"expected_user_message_id": str(old.id), "operation_id": str(uuid4()), "state_id": None},
            )
            status, body = settled.error()
            assert status == 409
            assert body["detail"]["error_type"] == "recompose_user_message_mismatch"
            assert composer.calls == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_retries_recheck_saved_reply_after_lock(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_compose = composer.compose

    async def parked_compose(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_compose(*args, **kwargs)

    composer.compose = parked_compose
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Concurrent retries"})).json()["id"])
            user = await service.add_message(session_id, "user", "Finish once", writer_principal="route_user_message")
            path = f"/api/sessions/{session_id}/recompose"
            first_body = {"expected_user_message_id": str(user.id), "operation_id": str(uuid4()), "state_id": None}
            accepted = await client.post(path, json=first_body)
            assert accepted.status_code == 202, accepted.text
            drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
            await asyncio.wait_for(entered.wait(), timeout=10)
            active_retry = await client.post(
                path,
                json={"expected_user_message_id": str(user.id), "operation_id": str(uuid4()), "state_id": None},
            )
            assert active_retry.status_code == 409, active_retry.text
            release.set()
            await drive
            first_final = await client.get(f"/api/sessions/{session_id}/operations/{first_body['operation_id']}")
            SettledComposerOperation(accepted, first_final).result()
            second = await submit_and_settle(
                client,
                app,
                path=path,
                body={
                    "expected_user_message_id": str(user.id),
                    "operation_id": str(uuid4()),
                    "state_id": await current_head_state_id(client, session_id),
                },
            )
            status, body = second.error()
            assert status == 409
            assert body["detail"]["error_type"] == "recompose_already_completed"
            assert composer.calls == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("pending", "committed", "rejected"))
async def test_saved_pipeline_proposal_lifecycle_controls_second_planning(tmp_path, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    app, service, _pipeline, session_id, proposal, endpoint = await _create_canonical_pipeline_route_proposal(
        tmp_path, monkeypatch, tool_call_id="saved_retry_proposal", origin_text="Build the pipeline"
    )
    composer = _make_composer_mock()
    app.state.composer_service = composer
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            if status == "committed":
                assert proposal.pipeline_metadata is not None
                accepted = await client.post(endpoint, json={"draft_hash": proposal.pipeline_metadata.draft_hash})
                assert accepted.status_code == 200, accepted.text
            elif status == "rejected":
                assert proposal.pipeline_metadata is not None
                async with _compose_session_operation_context(service, session_id) as context:
                    await service.reject_pipeline_composition_proposal(
                        session_id=session_id,
                        proposal_id=proposal.id,
                        draft_hash=proposal.pipeline_metadata.draft_hash,
                        reason="request_cancelled",
                        dispatch=None,
                        actor="composer-web:user:alice",
                        session_operation_context=context,
                    )
            settled = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={
                    "expected_user_message_id": str(proposal.user_message_id),
                    "operation_id": str(uuid4()),
                    "state_id": await current_head_state_id(client, session_id),
                },
            )
            if status == "rejected":
                assert settled.final.status_code == 200, settled.final.text
                settled.result()
                composer.compose.assert_awaited_once()
            else:
                status_code, response_body = settled.error()
                assert status_code == 409, settled.final.text
                assert response_body["detail"]["error_type"] in {"recompose_saved_proposal", "recompose_already_completed"}
                composer.compose.assert_not_awaited()
            assert [row.id for row in await service.list_composition_proposals(session_id)] == [proposal.id]
    finally:
        service._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ("Saved answer", "ELSPETH could not safely complete the pipeline. Review the saved state."))
async def test_settled_reply_is_not_retryable_based_on_prose(tmp_path, reply: str) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Settled reply"})).json()["id"])
            user = await service.add_message(session_id, "user", "Finish", writer_principal="route_user_message")
            await service.add_message(session_id, "assistant", reply, writer_principal="compose_loop")
            settled = await submit_and_settle(
                client,
                app,
                path=f"/api/sessions/{session_id}/recompose",
                body={"expected_user_message_id": str(user.id), "operation_id": str(uuid4()), "state_id": None},
            )
            status, body = settled.error()
            assert status == 409
            assert body["detail"]["error_type"] == "recompose_already_completed"
            assert composer.calls == 0
    finally:
        engine.dispose()
