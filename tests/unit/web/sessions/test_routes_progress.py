"""Composer progress, in-flight visibility and cancellation route tests."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.sessions.protocol import CompositionStateData
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.helpers.composer_operations import current_head_state_id, message_body, recompose_body
from tests.unit.web.sessions.test_routes import (
    _EMPTY_STATE,
    _advisor_blocked_preflight,
    _cancel_registered_composer,
    _CancellableComposer,
    _cancelled_error_with_llm_call,
    _llm_call,
    _llm_call_audit_rows,
    _make_progress_route_app,
    _ProgressAwareComposer,
    _save_test_composition_state,
    _settled_composer_response_async,
    _state_data_with_preflight,
)
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines


class TestComposerProgressRoutes:
    @pytest.mark.asyncio
    async def test_progress_endpoint_returns_latest_snapshot_for_owned_session(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        registry = app.state.composer_progress_registry
        await registry.publish(
            session_id=str(service.session.id),
            request_id="message-1",
            user_id=service.session.user_id,
            event=ComposerProgressEvent(
                phase="using_tools",
                headline="The model requested plugin schemas.",
                evidence=("Checking available source, transform, and sink tools.",),
                likely_next="ELSPETH will use the schemas to choose a pipeline shape.",
            ),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/sessions/{service.session.id}/composer-progress")

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == str(service.session.id)
        assert body["request_id"] == "message-1"
        assert body["phase"] == "using_tools"
        assert body["headline"] == "The model requested plugin schemas."
        assert body["evidence"] == ["Checking available source, transform, and sink tools."]

    @pytest.mark.asyncio
    async def test_progress_endpoint_enforces_session_ownership(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        wire_test_pipeline_user_authority(
            app,
            identity_id="bob",
            engine=app.state.session_engine,
        )

        async def bob_user():
            return UserIdentity(user_id="bob", username="bob")

        app.dependency_overrides[get_current_user] = bob_user

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/sessions/{service.session.id}/composer-progress")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_progress_endpoint_translates_identity_revoked_between_checks(self, tmp_path) -> None:
        """An identity disabled after auth but before the authority's snapshot is a 401, not a 500.

        The durable authority re-checks the committed identity and ownership
        state inside its own snapshot and denies with a typed
        ``PermissionError``; the route owns translating that expected
        concurrent access change into the same non-disclosing responses the
        earlier checks give (elspeth polling audit 2026-09-22, finding 4).
        """
        from elspeth.web.coordination.composer_progress_authority import ComposerProgressIdentityInactive

        app, service = _make_progress_route_app(tmp_path)

        class _RevokingRegistry:
            async def get_latest(self, session_id: str, user_id: str) -> Any:
                raise ComposerProgressIdentityInactive("Composer progress identity is inactive")

        app.state.composer_progress_registry = _RevokingRegistry()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/sessions/{service.session.id}/composer-progress")

        # The same opaque answer the per-request token check gives a revoked
        # principal (auth/session_token.py): this endpoint must not tell a
        # revoked user more than every other endpoint does.
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid token"

    @pytest.mark.asyncio
    async def test_progress_endpoint_translates_session_archived_between_checks(self, tmp_path) -> None:
        """A session archived after the route's ownership check is a 404, matching the earlier check."""
        from elspeth.web.coordination.composer_progress_authority import ComposerProgressSessionUnavailable

        app, service = _make_progress_route_app(tmp_path)

        class _ArchivedRegistry:
            async def get_latest(self, session_id: str, user_id: str) -> Any:
                raise ComposerProgressSessionUnavailable("Composer progress session is unavailable")

        app.state.composer_progress_registry = _ArchivedRegistry()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/sessions/{service.session.id}/composer-progress")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"

    @pytest.mark.asyncio
    async def test_send_message_marks_terminal_progress_with_user_message_id(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        composer = _ProgressAwareComposer()
        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _settled_composer_response_async(
                client,
                app,
                path=f"/api/sessions/{service.session.id}/messages",
                body=message_body("Exploit HTML into JSON", state_id=await current_head_state_id(client, service.session.id)),
            )
            messages = (await client.get(f"/api/sessions/{service.session.id}/messages")).json()
            progress = (await client.get(f"/api/sessions/{service.session.id}/composer-progress")).json()

        assert response.status_code == 200
        assert composer.progress_sink_seen is True
        user_message_id = next(message["id"] for message in messages if message["role"] == "user")
        assert progress["request_id"] == user_message_id
        assert progress["phase"] == "complete"
        assert progress["headline"] == "The composer has updated the pipeline."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("envelope_writer", ["production_writer", "carry_forward"])
    async def test_send_message_hands_the_prior_rows_advisor_block_to_the_composer(self, tmp_path, envelope_writer: str) -> None:
        """Ruling 2026-09-22 (elspeth-032ec69c41): the persisted fact must reach ``compose`` as the writer wrote it.

        The END gate's skip is decided on ``completion_gates``; the unit tests
        inject a hand-built fact, so this is the one place that proves the
        route derives that object from the prior state row's envelope rather
        than from something else (or nothing). Seeds the envelope through BOTH
        writers — the compose-save writer (explicit advisor decision) and the
        recovery-save carry-forward (``completion_gates_meta_from_facts``) —
        so a drift in either serializer shows here.
        """
        from elspeth.web.composer.advisor_decision import AdvisorBlockCause
        from elspeth.web.execution.completion_gates import (
            COMPLETION_GATES_META_KEY,
            AdvisorSignoffGateFact,
            CompletionGateFacts,
            completion_gate_fingerprint,
            completion_gates_meta_from_facts,
        )

        seeded_state = CompositionState(
            source=SourceSpec(plugin="csv", on_success="main", options={}, on_validation_failure="discard"),
            nodes=(),
            edges=(),
            outputs=(OutputSpec(name="main", plugin="json", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(name="blocked graph"),
            version=4,
        )
        blocked_fact = AdvisorSignoffGateFact(
            detail="Completion advisory review did not clear after the available attempts.",
            suggestion="Review the pipeline.",
            for_graph=completion_gate_fingerprint(seeded_state),
            note=None,
            cause=AdvisorBlockCause.GRAPH_REJECTED,
        )
        seeded_fact = CompletionGateFacts(advisor_signoff=blocked_fact)
        if envelope_writer == "production_writer":
            from elspeth.web.composer.advisor_decision import AdvisorGateBlocked

            data = await _state_data_with_preflight(
                seeded_state,
                _advisor_blocked_preflight(seeded_state),
                advisor_gate_decision=AdvisorGateBlocked(blocked_fact),
            )
            envelope = data.composer_meta[COMPLETION_GATES_META_KEY]
        else:
            envelope = completion_gates_meta_from_facts(seeded_fact)
        app, service = _make_progress_route_app(tmp_path)
        seeded_d = seeded_state.to_dict()
        await _save_test_composition_state(
            service,
            service.session.id,
            CompositionStateData(
                sources=seeded_d["sources"],
                nodes=seeded_d["nodes"],
                edges=seeded_d["edges"],
                outputs=seeded_d["outputs"],
                metadata_=seeded_d["metadata"],
                is_valid=True,
                validation_errors=None,
                composer_meta={COMPLETION_GATES_META_KEY: envelope},
            ),
            provenance="session_seed",
        )
        received: list[object] = []

        class _FactRecordingComposer:
            async def compose(self, message: str, chat_messages, state: CompositionState, **kwargs) -> ComposerResult:
                del message, chat_messages
                received.append(kwargs["completion_gates"])
                return ComposerResult(message="What the block means is …", state=state)

        app.state.composer_service = _FactRecordingComposer()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _settled_composer_response_async(
                client,
                app,
                path=f"/api/sessions/{service.session.id}/messages",
                body=message_body(
                    "What does this block mean, and what are my options?", state_id=await current_head_state_id(client, service.session.id)
                ),
            )

        assert response.status_code == 200
        assert received == [seeded_fact]

    @pytest.mark.asyncio
    async def test_send_message_hands_no_advisor_block_when_the_prior_row_recorded_none(self, tmp_path) -> None:
        """Control for the test above: an envelope-less row reaches ``compose`` as ``None`` (the always-review default)."""
        app, service = _make_progress_route_app(tmp_path)
        seeded_d = _EMPTY_STATE.to_dict()
        await _save_test_composition_state(
            service,
            service.session.id,
            CompositionStateData(
                sources=seeded_d["sources"],
                nodes=seeded_d["nodes"],
                edges=seeded_d["edges"],
                outputs=seeded_d["outputs"],
                metadata_=seeded_d["metadata"],
                is_valid=True,
                validation_errors=None,
                composer_meta={},
            ),
            provenance="session_seed",
        )
        received: list[object] = []

        class _FactRecordingComposer:
            async def compose(self, message: str, chat_messages, state: CompositionState, **kwargs) -> ComposerResult:
                del message, chat_messages
                received.append(kwargs["completion_gates"])
                return ComposerResult(message="reply", state=state)

        app.state.composer_service = _FactRecordingComposer()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _settled_composer_response_async(
                client,
                app,
                path=f"/api/sessions/{service.session.id}/messages",
                body=message_body("hello", state_id=await current_head_state_id(client, service.session.id)),
            )

        assert response.status_code == 200
        assert received == [None]

    @pytest.mark.asyncio
    async def test_recompose_marks_terminal_progress_with_last_user_message_id(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        composer = _ProgressAwareComposer("Retry reply")
        app.state.composer_service = composer
        user_msg = await service.add_message(service.session.id, "user", "Exploit HTML into JSON", writer_principal="route_user_message")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _settled_composer_response_async(
                client,
                app,
                path=f"/api/sessions/{service.session.id}/recompose",
                body=recompose_body(str(user_msg.id), state_id=await current_head_state_id(client, service.session.id)),
            )
            progress = (await client.get(f"/api/sessions/{service.session.id}/composer-progress")).json()

        assert response.status_code == 200
        assert composer.progress_sink_seen is True
        assert progress["request_id"] == str(user_msg.id)
        assert progress["phase"] == "complete"


class TestComposerInFlightEndpoint:
    """The /_active endpoint is the cross-session view added with elspeth-29e8bd8a1f.

    Per-session /composer-progress polling already worked, but answered "what's
    happening with session X?". Operators needed "what's running on this server
    right now?" — without that view, an in-flight composer request was invisible
    in the journal until the response completed (uvicorn access log timing).
    """

    @pytest.mark.asyncio
    async def test_returns_only_authenticated_users_in_flight_sessions(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        registry = app.state.composer_progress_registry

        # alice has one in-flight and one completed session.
        await registry.publish(
            session_id=str(service.session.id),
            request_id="msg-1",
            user_id="alice",
            event=ComposerProgressEvent(
                phase="calling_model",
                headline="The model is composing.",
                evidence=("Prompt was built.",),
            ),
        )
        await registry.publish(
            session_id="alice-completed-session",
            request_id="msg-completed",
            user_id="alice",
            event=ComposerProgressEvent(
                phase="complete",
                headline="The composer response is ready.",
                evidence=("Saved.",),
                reason="composer_complete",
            ),
        )
        # bob has one in-flight session — must not appear in alice's view.
        await registry.publish(
            session_id="bob-active-session",
            request_id="msg-bob",
            user_id="bob",
            event=ComposerProgressEvent(
                phase="using_tools",
                headline="Bob's request.",
                evidence=("Active.",),
            ),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/sessions/_active")

        assert resp.status_code == 200
        body = resp.json()
        assert {snap["session_id"] for snap in body} == {str(service.session.id)}
        # phase must be a non-terminal value; counters depend on this invariant.
        assert all(snap["phase"] in {"starting", "calling_model", "using_tools", "validating", "saving"} for snap in body)

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_sessions_in_flight(self, tmp_path) -> None:
        """Empty list, not 404 — it's a successful query with zero results."""
        app, _ = _make_progress_route_app(tmp_path)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/sessions/_active")

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_active_list_translates_identity_revoked_between_checks(self, tmp_path) -> None:
        """An identity disabled after auth but before the authority's read is a 401, not a 500."""
        from elspeth.web.coordination.composer_progress_authority import ComposerProgressIdentityInactive

        app, _ = _make_progress_route_app(tmp_path)

        class _RevokingRegistry:
            async def list_active(self, *, user_id: str) -> Any:
                raise ComposerProgressIdentityInactive("Composer progress identity is inactive")

        app.state.composer_progress_registry = _RevokingRegistry()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/sessions/_active")

        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid token"

    @pytest.mark.asyncio
    async def test_route_path_does_not_collide_with_session_id_route(self, tmp_path) -> None:
        """_active must beat the /{session_id} pattern. Otherwise FastAPI tries
        to parse 'underscore_active' as a UUID and returns 422 before the
        operator ever sees the snapshot list.
        """
        app, _ = _make_progress_route_app(tmp_path)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/sessions/_active")

        # If the route order were wrong, this would be 422 (UUID parse error).
        assert resp.status_code != 422


class TestComposerCancellationLifecycle:
    """elspeth-29e8bd8a1f acceptance criteria: client cancellations are
    distinguished from server timeouts, both in the progress snapshot
    (phase=cancelled, reason=client_cancelled) and in the terminal
    counter (status=cancelled vs status=timed_out).
    """

    @pytest.mark.asyncio
    async def test_send_message_publishes_cancelled_snapshot_on_cancellation(self, tmp_path) -> None:
        """When compose() is cancelled mid-flight (real disconnect or asyncio
        cancel), the route MUST publish a phase=cancelled / reason=client_cancelled
        snapshot before re-raising. This proves the except-CancelledError +
        asyncio.shield contract.

        The fake composer raises CancelledError directly. A live ASGI
        http.disconnect propagates as a CancelledError on the route task
        — the route handler cannot tell the two apart, so this test
        exercises the contract that matters.
        """
        app, service = _make_progress_route_app(tmp_path)

        composer = _CancellableComposer(None)

        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _cancel_registered_composer(
                client, app, path=f"/api/sessions/{service.session.id}/messages", body=message_body("Will be cancelled"), composer=composer
            )
            assert response.status_code == 499

        registry = app.state.composer_progress_registry
        snapshot = await registry.get_latest(str(service.session.id))
        assert snapshot.phase == "cancelled"
        assert snapshot.reason == "client_cancelled"
        # list_active must NOT include cancelled phases — otherwise the
        # /_active operator view would persistently show stale cancellations.
        active = await registry.list_active(user_id=service.session.user_id)
        assert active == ()

    @pytest.mark.asyncio
    async def test_send_message_persists_cancelled_llm_call_audit_sidecar(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        llm_call = _llm_call(
            status=ComposerLLMCallStatus.CANCELLED,
            model_returned=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            provider_request_id=None,
            error_class="CancelledError",
            error_message="CancelledError",
        )
        cancelled = _cancelled_error_with_llm_call(llm_call)

        composer = _CancellableComposer(cancelled)

        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _cancel_registered_composer(
                client, app, path=f"/api/sessions/{service.session.id}/messages", body=message_body("Will be cancelled"), composer=composer
            )
            assert response.status_code == 499

        llm_audit_rows = _llm_call_audit_rows(await service.get_messages(service.session.id, limit=None))
        assert len(llm_audit_rows) == 1
        _row, tool_call = llm_audit_rows[0]
        assert tool_call["call"]["status"] == "cancelled"
        assert tool_call["call"]["messages_hash"] == llm_call.messages_hash

    @pytest.mark.asyncio
    async def test_send_message_increments_terminal_counter_with_cancelled_status(self, tmp_path, monkeypatch) -> None:
        """The terminal counter must record status=cancelled separately from
        status=timed_out so a Grafana board can distinguish "the client gave
        up" from "the server gave up".
        """
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[int, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((value, dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_REQUEST_TERMINAL_COUNTER", FakeCounter())

        app, service = _make_progress_route_app(tmp_path)

        composer = _CancellableComposer(None)

        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _cancel_registered_composer(
                client, app, path=f"/api/sessions/{service.session.id}/messages", body=message_body("Will be cancelled"), composer=composer
            )
            assert response.status_code == 499

        terminal_events = [(v, attrs) for v, attrs in emitted if attrs.get("endpoint") == "send_message"]
        assert len(terminal_events) == 1, f"expected 1 terminal event, got {terminal_events}"
        value, attrs = terminal_events[0]
        assert value == 1
        assert attrs == {"endpoint": "send_message", "status": "cancelled"}

    @pytest.mark.asyncio
    async def test_send_message_increments_terminal_counter_with_completed_status(self, tmp_path, monkeypatch) -> None:
        """Successful path: status=completed. Pinned so a future refactor that
        forgets to set terminal_status before return falls back to the
        pessimistic default of 'failed' — and this test catches it.
        """
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[int, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((value, dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_REQUEST_TERMINAL_COUNTER", FakeCounter())

        app, service = _make_progress_route_app(tmp_path)
        app.state.composer_service = _ProgressAwareComposer()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _settled_composer_response_async(
                client,
                app,
                path=f"/api/sessions/{service.session.id}/messages",
                body=message_body("Hello", state_id=await current_head_state_id(client, service.session.id)),
            )

        assert resp.status_code == 200
        terminal_events = [(v, attrs) for v, attrs in emitted if attrs.get("endpoint") == "send_message"]
        assert terminal_events == [(1, {"endpoint": "send_message", "status": "completed"})]

    @pytest.mark.asyncio
    async def test_recompose_publishes_cancelled_snapshot_on_cancellation(self, tmp_path) -> None:
        """Drift guard: recompose mirrors send_message structurally, so the
        cancellation contract MUST remain identical. A 4-line parallel test
        is cheap insurance against future refactors that fix one path and
        forget the other.
        """
        app, service = _make_progress_route_app(tmp_path)
        user_msg = await service.add_message(
            service.session.id, "user", "Will be cancelled on retry", writer_principal="route_user_message"
        )

        composer = _CancellableComposer(None)

        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _cancel_registered_composer(
                client, app, path=f"/api/sessions/{service.session.id}/recompose", body=recompose_body(str(user_msg.id)), composer=composer
            )
            assert response.status_code == 499

        registry = app.state.composer_progress_registry
        snapshot = await registry.get_latest(str(service.session.id))
        assert snapshot.phase == "cancelled"
        assert snapshot.reason == "client_cancelled"

    @pytest.mark.asyncio
    async def test_recompose_persists_cancelled_llm_call_audit_sidecar(self, tmp_path) -> None:
        app, service = _make_progress_route_app(tmp_path)
        user_msg = await service.add_message(
            service.session.id, "user", "Will be cancelled on retry", writer_principal="route_user_message"
        )
        llm_call = _llm_call(
            status=ComposerLLMCallStatus.CANCELLED,
            model_returned=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            provider_request_id=None,
            error_class="CancelledError",
            error_message="CancelledError",
        )
        cancelled = _cancelled_error_with_llm_call(llm_call)

        composer = _CancellableComposer(cancelled)

        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _cancel_registered_composer(
                client, app, path=f"/api/sessions/{service.session.id}/recompose", body=recompose_body(str(user_msg.id)), composer=composer
            )
            assert response.status_code == 499

        llm_audit_rows = _llm_call_audit_rows(await service.get_messages(service.session.id, limit=None))
        assert len(llm_audit_rows) == 1
        _row, tool_call = llm_audit_rows[0]
        assert tool_call["call"]["status"] == "cancelled"
        assert tool_call["call"]["messages_hash"] == llm_call.messages_hash

    @pytest.mark.asyncio
    async def test_recompose_increments_terminal_counter_with_cancelled_status(self, tmp_path, monkeypatch) -> None:
        """Counter parity with send_message — the endpoint attribute on the
        terminal counter must be 'recompose' (not 'send_message'), so a
        Grafana board can split the two routes' cancellation rates.
        """
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[int, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((value, dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_REQUEST_TERMINAL_COUNTER", FakeCounter())

        app, service = _make_progress_route_app(tmp_path)
        user_msg = await service.add_message(
            service.session.id, "user", "Will be cancelled on retry", writer_principal="route_user_message"
        )

        composer = _CancellableComposer(None)

        app.state.composer_service = composer

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await _cancel_registered_composer(
                client, app, path=f"/api/sessions/{service.session.id}/recompose", body=recompose_body(str(user_msg.id)), composer=composer
            )
            assert response.status_code == 499

        terminal_events = [(v, attrs) for v, attrs in emitted if attrs.get("endpoint") == "recompose"]
        assert terminal_events == [(1, {"endpoint": "recompose", "status": "cancelled"})]

    @pytest.mark.asyncio
    async def test_inflight_gauge_increments_then_decrements_across_request(self, tmp_path, monkeypatch) -> None:
        """The UpDownCounter must net to zero across one successful request.
        If the finally clause is removed or the increment is mis-placed,
        the gauge drifts and a Grafana 'currently in flight' panel lies.
        """
        # ``_COMPOSER_REQUESTS_INFLIGHT.add`` is called from the send_message
        # route handler in messages.py (the /messages route this test drives),
        # which bound the name at import — so the patch must target that
        # calling module, not the defining _helpers module.
        from elspeth.web.sessions import composer_turn as messages_module

        emitted: list[tuple[int, dict[str, str]]] = []

        class FakeUpDownCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((value, dict(attributes)))

        monkeypatch.setattr(messages_module, "_COMPOSER_REQUESTS_INFLIGHT", FakeUpDownCounter())

        app, service = _make_progress_route_app(tmp_path)
        app.state.composer_service = _ProgressAwareComposer()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _settled_composer_response_async(
                client,
                app,
                path=f"/api/sessions/{service.session.id}/messages",
                body=message_body("Hello", state_id=await current_head_state_id(client, service.session.id)),
            )

        assert resp.status_code == 200
        send_events = [(v, attrs) for v, attrs in emitted if attrs.get("endpoint") == "send_message"]
        assert send_events == [
            (1, {"endpoint": "send_message"}),
            (-1, {"endpoint": "send_message"}),
        ], "in-flight gauge must net to zero across one request"
