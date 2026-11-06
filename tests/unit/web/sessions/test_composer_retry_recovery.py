"""SQLite route proofs for retry eligibility after an interrupted turn."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import litellm
import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers.composer_operations import current_head_state_id
from tests.unit.web.sessions.test_freeform_route_custody import _file_app
from tests.unit.web.sessions.test_routes import (
    _compose_session_operation_context,
    _create_canonical_pipeline_route_proposal,
)


def _refuse_sdk(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record any physical SDK call; admission refusals must make none."""
    calls: list[dict] = []

    async def record(**kwargs):
        calls.append(kwargs)
        raise AssertionError("an admission refusal reached the provider")

    monkeypatch.setattr(litellm, "acompletion", record)
    return calls


async def _assert_recompose_refused_at_admission(client, session_id, body, expected_error_type):
    """Continuation checks recompose eligibility in the admission transaction: 409, no row."""
    refused = await client.post(f"/api/sessions/{session_id}/recompose", json=body)
    assert refused.status_code == 409, refused.text
    error_type = refused.json()["detail"]["error_type"]
    if expected_error_type is not None:
        assert error_type == expected_error_type
    missing = await client.get(f"/api/sessions/{session_id}/operations/{body['operation_id']}")
    assert missing.status_code == 404, missing.text
    return error_type


class _PhasedSDK:
    """Serve the interrupted send, then the retry, whatever the call count per phase.

    ``send``: the first primary call applies one real set_metadata; any later
    call in that phase raises, so the turn is interrupted after its tool turn
    committed. ``retry``: the primary replies with text and END admits.
    """

    def __init__(self, narration: str) -> None:
        self.narration = narration
        self.phase = "send"
        self.app = None
        self.requests: list[dict] = []

    async def __call__(self, **kwargs):
        from tests.helpers.continuation_app import ADVISOR_MODEL, end_admit, sdk_reply, tool_call

        self.requests.append({"phase": self.phase, **kwargs})
        sent = [request for request in self.requests if request["phase"] == self.phase]
        if self.phase == "send":
            if len(sent) == 1:
                call = tool_call(self.app, "call_done", "set_metadata", {"patch": {"name": "Already set"}})
                return sdk_reply(self.narration or None, [call])
            raise litellm.APIConnectionError(message="interrupted after the tool turn", llm_provider="openai", model=kwargs["model"])
        if kwargs["model"] == ADVISOR_MODEL:
            return litellm.ModelResponse(
                model=ADVISOR_MODEL,
                choices=[
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(end_admit()), "tool_calls": None}}
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )
        return sdk_reply("Recovered reply.")

    def retry_requests(self) -> list[dict]:
        return [request for request in self.requests if request["phase"] == "retry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("narration", ("", "I updated the pipeline name."))
async def test_partial_tool_turn_retries_from_current_state_without_replaying_tools(tmp_path, monkeypatch, narration: str) -> None:
    """A retry after a partial tool turn composes from the current state without replaying tool rows."""
    from tests.helpers.continuation_app import PRIMARY_MODEL, continuation_app, install_sdk, submit_and_settle

    sdk = _PhasedSDK(narration)
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app = harness.app
        service = harness.app.state.session_service
        session_id = harness.session_id
        interrupted = await submit_and_settle(harness, {"content": "Set the name once", "operation_id": str(uuid4()), "state_id": None})
        assert interrupted.status == "failed", interrupted.model_dump_json()
        before = await service.get_messages(session_id, limit=None)
        user = next(row for row in before if row.role == "user")
        state = await service.get_current_state(session_id)
        assert state is not None and state.metadata_["name"] == "Already set"
        tool_rows = [row.id for row in before if row.role == "tool"]
        assert len(tool_rows) == 1
        sdk.phase = "retry"
        body = {"expected_user_message_id": str(user.id), "operation_id": str(uuid4()), "state_id": str(state.id)}
        settled = await submit_and_settle(harness, body, route="recompose")
        # Integration point (U-40): on this branch a Continuation recompose
        # settles audit_integrity_error before its first provider call.
        primaries = [request for request in sdk.retry_requests() if request["model"] == PRIMARY_MODEL]
        assert primaries, settled.model_dump_json()
        # The retry's primary request carries the user turn, never the
        # already-applied tool call or its result.
        messages = primaries[0]["messages"]
        assert user.content in json.dumps(messages)
        assert all(not message.get("tool_calls") for message in messages if message["role"] == "assistant")
        assert not [message for message in messages if message["role"] == "tool"]
        assert "Already set" in json.dumps(messages)
        after = await service.get_messages(session_id, limit=None)
        assert after[: len(before)] == before
        assert [row.id for row in after if row.role == "tool"] == tool_rows
        assert len([row for row in after if row.role == "user"]) == 1
        assert settled.status == "completed", settled.model_dump_json()
        async with harness.client() as client:
            again = {
                "expected_user_message_id": str(user.id),
                "operation_id": str(uuid4()),
                "state_id": await current_head_state_id(client, session_id),
            }
            await _assert_recompose_refused_at_admission(client, session_id, again, "recompose_already_completed")


@pytest.mark.asyncio
async def test_newer_user_identity_blocks_old_partial_turn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    sdk_calls = _refuse_sdk(monkeypatch)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Newer turn"})).json()["id"])
            old = await service.add_message(session_id, "user", "Old request", writer_principal="route_user_message")
            await service.add_message(session_id, "user", "New request", writer_principal="route_user_message")
            body = {"expected_user_message_id": str(old.id), "operation_id": str(uuid4()), "state_id": None}
            await _assert_recompose_refused_at_admission(client, session_id, body, "recompose_user_message_mismatch")
            assert sdk_calls == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("pending", "committed", "rejected"))
async def test_saved_pipeline_proposal_lifecycle_controls_second_planning(tmp_path, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    app, service, _pipeline, session_id, proposal, endpoint = await _create_canonical_pipeline_route_proposal(
        tmp_path, monkeypatch, tool_call_id="saved_retry_proposal", origin_text="Build the pipeline"
    )
    sdk_calls = _refuse_sdk(monkeypatch)
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
            body = {
                "expected_user_message_id": str(proposal.user_message_id),
                "operation_id": str(uuid4()),
                "state_id": await current_head_state_id(client, session_id),
            }
            if status == "rejected":
                # A rejected proposal no longer controls planning: the retry is admitted.
                admitted = await client.post(f"/api/sessions/{session_id}/recompose", json=body)
                assert admitted.status_code == 202, admitted.text
                assert admitted.json()["operation_id"] == body["operation_id"]
            else:
                error_type = await _assert_recompose_refused_at_admission(client, session_id, body, None)
                assert error_type in {"recompose_saved_proposal", "recompose_already_completed"}
            assert sdk_calls == []
            assert [row.id for row in await service.list_composition_proposals(session_id)] == [proposal.id]
    finally:
        service._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ("Saved answer", "ELSPETH could not safely complete the pipeline. Review the saved state."))
async def test_settled_reply_is_not_retryable_based_on_prose(tmp_path, monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    sdk_calls = _refuse_sdk(monkeypatch)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = UUID((await client.post("/api/sessions", json={"title": "Settled reply"})).json()["id"])
            user = await service.add_message(session_id, "user", "Finish", writer_principal="route_user_message")
            await service.add_message(session_id, "assistant", reply, writer_principal="compose_loop")
            body = {"expected_user_message_id": str(user.id), "operation_id": str(uuid4()), "state_id": None}
            await _assert_recompose_refused_at_admission(client, session_id, body, "recompose_already_completed")
            assert sdk_calls == []
    finally:
        engine.dispose()
