"""Legacy accept terminalizes unusable arguments and content-identical changes."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.composer.test_planner_authoring_aids import _empty_state, _guardrail_profile_view
from tests.unit.web.composer.test_queue_scope_admission import QUEUE, SCOPE, complete_queue, private_snapshot, scope_message
from tests.unit.web.composer.test_required_control_autowire import _bare_llm_candidate
from tests.unit.web.sessions.routes.composer.test_proposal_accept_preflight_failure import _set_pipeline_arguments
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state
from tests.unit.web.sessions.test_routes import _compose_session_operation_context, _create_test_composition_proposal, _make_app

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.interpretation_state import REQUIRED_CONTROL_AUTO_WIRED_USER_TERM, pipeline_decision_artifact_hash
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes.composer import proposals as routes


def _proposal(
    service: Any,
    session_id: UUID,
    tool_name: str,
    arguments: dict[str, Any],
    base_state_id: UUID | None = None,
    *,
    user_message_id: UUID | None = None,
) -> Any:
    return asyncio.run(
        _create_test_composition_proposal(
            service,
            session_id=session_id,
            tool_call_id=f"call_{tool_name}",
            tool_name=tool_name,
            summary="Legacy change",
            rationale="Operator requested change",
            affects=("graph",),
            arguments_json=arguments,
            arguments_redacted_json={},
            base_state_id=base_state_id,
            actor="composer-web:user:alice",
            user_message_id=user_message_id,
            composer_model_identifier="test-model",
            composer_model_version="test-model-v1",
            composer_provider="test",
            composer_skill_hash=stable_hash("test-skill"),
            tool_arguments_hash=stable_hash(arguments),
        )
    )


@pytest.mark.parametrize("override", [*({key: value} for key, value in SCOPE.items()), SCOPE, {"scope_policy": []}])
def test_legacy_queue_scope_replay_terminalizes_without_changing_private_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Queue replay"}).json()["id"])
    _, built, _ = complete_queue(tmp_path, session_id=str(session_id))
    data = built.to_dict()
    saved = asyncio.run(
        _save_composition_state(
            service,
            session_id,
            CompositionStateData(
                sources=data["sources"], nodes=data["nodes"], edges=data["edges"], outputs=data["outputs"], metadata_=data["metadata"]
            ),
            provenance="session_seed",
        )
    )
    before = private_snapshot(state_from_record(saved))
    proposal = _proposal(service, session_id, "upsert_node", {**QUEUE, "description": "Do not apply", **override}, saved.id)
    real_dispatch = routes.execute_tool
    results = []
    attempts = []

    def observe(*args: Any, **kwargs: Any) -> Any:
        attempts.append(True)
        result = real_dispatch(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(routes, "execute_tool", observe)
    endpoint = f"/api/sessions/{session_id}/proposals/{proposal.id}/accept"
    response = client.post(endpoint)
    assert response.status_code == 422, response.text
    malformed = override == {"scope_policy": []}
    assert response.json()["detail"]["error_type"] == "proposal_validation_failed"
    assert len(attempts) == 1
    if not malformed:
        (result,) = results
        assert not result.success and result.affected_nodes == () and result.applied_component is None
        error = result.validation.errors[0]
        assert (error.component, error.error_code, error.rejected_component) == (
            "rejected_mutation",
            "queue_config_invalid",
            "node:inbound",
        )
        assert error.message == scope_message(override)
        assert private_snapshot(result.updated_state) == before
    else:
        assert len(results) == 1 and not results[0].success
        assert results[0].validation.errors[0].error_code != "queue_config_invalid"
        assert private_snapshot(results[0].updated_state) == before
    current = asyncio.run(service.get_current_state(session_id))
    assert current is not None and current.id == saved.id
    assert private_snapshot(state_from_record(current)) == before
    with service._engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1
    assert (
        asyncio.run(service.get_authoritative_composition_proposal(session_id=session_id, proposal_id=proposal.id)).row.status == "rejected"
    )
    events = [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id]
    assert [event.event_type for event in events] == ["proposal.created", "proposal.rejected"]
    assert client.post(endpoint).status_code == 409
    assert len(attempts) == 1
    assert [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id] == events


@pytest.mark.parametrize(
    ("tool_name", "arguments", "changed"),
    [
        ("get_pipeline_state", {}, False),
        ("set_metadata", {"patch": {}}, False),
        ("set_metadata", {"patch": {"name": "accepted-proposal"}}, False),
        ("set_metadata", {"patch": {"name": "actually changed"}}, True),
    ],
    ids=["same-version", "empty-patch", "same-value-version-increment", "changed"],
)
def test_content_identity_controls_legacy_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str, arguments: dict[str, Any], changed: bool
) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Terminal no-op"}).json()["id"])
    input_path = tmp_path / "blobs" / str(session_id) / "input.csv"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("value\n1\n", encoding="utf-8")
    seed = _proposal(service, session_id, "set_pipeline", _set_pipeline_arguments(input_path))
    seeded = client.post(f"/api/sessions/{session_id}/proposals/{seed.id}/accept")
    assert seeded.status_code == 200, seeded.text
    before = asyncio.run(service.get_current_state(session_id))
    assert before is not None
    proposal = _proposal(service, session_id, tool_name, arguments, before.id)
    endpoint = f"/api/sessions/{session_id}/proposals/{proposal.id}/accept"
    preflights = []
    dispatches = []
    from elspeth.web.sessions.routes import _helpers

    real_preflight = _helpers._runtime_preflight_for_state
    real_dispatch = routes.execute_tool

    async def track_preflight(*args: Any, **kwargs: Any) -> Any:
        preflights.append(True)
        return await real_preflight(*args, **kwargs)

    def track_dispatch(*args: Any, **kwargs: Any) -> Any:
        result = real_dispatch(*args, **kwargs)
        dispatches.append(result)
        return result

    monkeypatch.setattr(_helpers, "_runtime_preflight_for_state", track_preflight)
    monkeypatch.setattr(routes, "execute_tool", track_dispatch)
    response = client.post(endpoint)
    assert response.status_code == (200 if changed else 422), response.text
    if not changed:
        assert response.json()["detail"]["error_type"] == "proposal_no_change"
    assert len(dispatches) == 1
    assert dispatches[0].success
    assert (dispatches[0].updated_state.version == before.version) == (tool_name == "get_pipeline_state")
    assert len(preflights) == int(changed)
    after = asyncio.run(service.get_current_state(session_id))
    assert after is not None
    assert (after.id != before.id) == changed
    with service._engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1 + int(changed)
    events = [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id]
    assert [event.event_type for event in events] == ["proposal.created", "proposal.accepted" if changed else "proposal.rejected"]
    assert events[-1].actor == ("user:alice" if changed else "system:no-change:user:alice")
    assert client.post(endpoint).status_code == 409
    assert len(dispatches) == 1
    assert [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id] == events
    listed = client.get(f"/api/sessions/{session_id}/proposals?status=pending")
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.parametrize("patch", [{}, {"name": "Assess tickets"}], ids=["empty-patch", "same-value"])
def test_legacy_accept_compares_content_after_required_control_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: dict[str, Any]
) -> None:
    from elspeth.web.sessions.routes import _helpers

    app, service = _make_app(tmp_path)
    view, snapshot = _guardrail_profile_view(tmp_path)
    assert view._profiles is not None
    app.state.catalog_service = view._full
    app.state.operator_profile_registry = view._profiles
    # Preserve the compiled control modes when projecting this policy to the
    # route principal; the general static factory omits those modes.
    route_snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=snapshot.policy_hash,
        principal_scope="local:alice",
        available=snapshot.available,
        unavailable=snapshot.unavailable,
        selected=snapshot.selected,
        usable_profile_aliases=snapshot.usable_profile_aliases,
        selected_profile_aliases=snapshot.selected_profile_aliases,
        binding_generation_fingerprint=snapshot.binding_generation_fingerprint,
        control_modes=snapshot.control_modes,
    )
    app.state.plugin_snapshot_factory = lambda _user: route_snapshot
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Required controls"}).json()["id"])
    (tmp_path / "outputs").mkdir()
    arguments = _bare_llm_candidate()
    message = asyncio.run(
        service.add_message(session_id, "user", arguments["source"]["inline_blob"]["content"], writer_principal="route_user_message")
    )

    async def build_bare_graph() -> Any:
        async with _compose_session_operation_context(service, session_id) as context:
            return await run_sync_in_worker(
                routes.execute_tool,
                "set_pipeline",
                arguments,
                _empty_state(),
                view,
                plugin_snapshot=snapshot,
                data_dir=str(tmp_path),
                session_engine=service._engine,
                session_id=str(session_id),
                session_operation_context=context,
                session_operation_authority=service.session_operation_authority,
                user_message_id=str(message.id),
                user_message_content=message.content,
                validate_arguments=True,
            )

    built = asyncio.run(build_bare_graph())
    assert built.success, built.validation.errors
    bare = built.updated_state
    assert [node.id for node in bare.nodes] == ["assess_ticket"]
    # A previously persisted graph can predate the deployment's required
    # controls. Seed the real builder's authored state under a real authority.
    bare_data = bare.to_dict()
    before = asyncio.run(
        _save_composition_state(
            service,
            session_id,
            CompositionStateData(
                sources=bare_data["sources"],
                nodes=bare_data["nodes"],
                edges=bare_data["edges"],
                outputs=bare_data["outputs"],
                metadata_=bare_data["metadata"],
            ),
            provenance="session_seed",
        )
    )
    current = state_from_record(before)
    proposal = _proposal(service, session_id, "set_metadata", {"patch": patch}, before.id)
    dispatch_results = []
    preflight_states = []
    real_dispatch = routes.execute_tool
    real_preflight = _helpers._runtime_preflight_for_state

    def observe_dispatch(*args: Any, **kwargs: Any) -> Any:
        result = real_dispatch(*args, **kwargs)
        dispatch_results.append(result)
        return result

    async def observe_preflight(state: Any, **kwargs: Any) -> Any:
        preflight_states.append(state)
        return await real_preflight(state, **kwargs)

    monkeypatch.setattr(routes, "execute_tool", observe_dispatch)
    monkeypatch.setattr(_helpers, "_runtime_preflight_for_state", observe_preflight)
    response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")

    assert len(dispatch_results) == 1
    dispatched = dispatch_results[0]
    assert dispatched.success
    assert dispatched.validation.is_valid, dispatched.validation.errors
    assert dispatched.updated_state.version == current.version + 1
    assert composition_content_hash(dispatched.updated_state) == composition_content_hash(current)
    assert [node.id for node in dispatched.updated_state.nodes] == ["assess_ticket"]
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "committed"
    assert len(preflight_states) == 1, "the finalized graph must reach real runtime preflight"
    after = asyncio.run(service.get_current_state(session_id))
    assert after is not None and after.id != before.id
    assert response.json()["committed_state_id"] == str(after.id)
    finalized = state_from_record(after)
    assert composition_content_hash(finalized) == composition_content_hash(preflight_states[0])
    assert composition_content_hash(finalized) != composition_content_hash(current)
    nodes = {node.id: node for node in finalized.nodes}
    assert set(nodes) == {"assess_ticket", "prompt_shield_auto_1", "content_safety_auto_1"}
    assert nodes["prompt_shield_auto_1"].options["profile"] == "prompt-approved"
    assert nodes["content_safety_auto_1"].options["profile"] == "content-approved"
    for node_id in ("prompt_shield_auto_1", "content_safety_auto_1"):
        requirements = nodes[node_id].options["interpretation_requirements"]
        assert len(requirements) == 1
        assert requirements[0]["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
        assert requirements[0]["kind"] == "pipeline_decision"
        assert requirements[0]["id"]
        assert requirements[0]["status"] == "pending"
        assert requirements[0]["accepted_artifact_hash"] is None
        assert len(pipeline_decision_artifact_hash(nodes[node_id], finalized.nodes, user_term=REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)) == 64
    coverage = [finding for finding in view.validate_authored_state(finalized).findings if finding.stage == "required_control_coverage"]
    assert coverage == []
    with service._engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 2
    events = [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id]
    assert [event.event_type for event in events] == ["proposal.created", "proposal.accepted"]
    assert events[-1].actor == "user:alice"


def test_real_malformed_handler_arguments_terminalize_without_leaking_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Malformed blob"}).json()["id"])
    message = asyncio.run(service.add_message(session_id, "user", "Create a blob", writer_principal="route_user_message"))
    proposal = _proposal(
        service, session_id, "create_blob", {"filename": "..", "mime_type": "text/csv", "content": "PRIVATE"}, user_message_id=message.id
    )
    endpoint = f"/api/sessions/{session_id}/proposals/{proposal.id}/accept"
    response = client.post(endpoint)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error_type"] == "proposal_argument_invalid"
    assert "filename" in response.json()["detail"]["detail"]
    assert "PRIVATE" not in response.text
    assert "'..'" not in response.text
    events = [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id]
    assert [event.event_type for event in events] == ["proposal.created", "proposal.rejected"]
    assert events[-1].actor == "system:argument-invalid:user:alice"
    assert asyncio.run(service.get_current_state(session_id)) is None

    def forbidden_dispatch(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("terminal proposal redispatched")

    monkeypatch.setattr(routes, "execute_tool", forbidden_dispatch)
    assert client.post(endpoint).status_code == 409
    assert [event for event in asyncio.run(service.list_proposal_events(session_id)) if event.proposal_id == proposal.id] == events


def test_unexpected_dispatch_value_error_is_not_terminalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Unexpected failure"}).json()["id"])
    proposal = _proposal(service, session_id, "set_metadata", {"patch": {}})
    original = ValueError("unexpected dispatch failure")

    def fail_dispatch(*args: Any, **kwargs: Any) -> Any:
        raise original

    monkeypatch.setattr(routes, "execute_tool", fail_dispatch)
    with pytest.raises(ValueError) as caught:
        client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")
    assert caught.value is original
    assert (
        asyncio.run(service.get_authoritative_composition_proposal(session_id=session_id, proposal_id=proposal.id)).row.status == "pending"
    )
    assert [event.event_type for event in asyncio.run(service.list_proposal_events(session_id))] == ["proposal.created"]


def test_legacy_create_blob_keeps_existing_acceptance_refusal_without_noop_rejection(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Legacy blob"}).json()["id"])
    before = asyncio.run(
        _save_composition_state(
            service, session_id, CompositionStateData(metadata_={"name": "Blob", "description": ""}), provenance="session_seed"
        )
    )
    message = asyncio.run(service.add_message(session_id, "user", "Create a report", writer_principal="route_user_message"))
    proposal = _proposal(
        service,
        session_id,
        "create_blob",
        {"filename": "report.csv", "mime_type": "text/csv", "content": "value\n1\n"},
        before.id,
        user_message_id=message.id,
    )
    response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")
    # create_blob is not an approval-required tool and has no proposal-effect
    # receipt. Legacy replay can write the blob, but cannot commit acceptance.
    # Preserve that refusal rather than falsely terminalizing durable work as
    # a graph no-op. Update/delete receipts support actual acceptance.
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Accepted proposal did not change composition state."
    assert asyncio.run(service.get_current_state(session_id)) == before
    blobs = asyncio.run(BlobServiceImpl(service._engine, tmp_path).list_blobs(session_id))
    assert len(blobs) == 1
    assert Path(blobs[0].storage_path).read_text(encoding="utf-8") == "value\n1\n"
    assert blobs[0].creating_provider == "test"
    assert [event.event_type for event in asyncio.run(service.list_proposal_events(session_id))] == ["proposal.created"]
    assert (
        asyncio.run(service.get_authoritative_composition_proposal(session_id=session_id, proposal_id=proposal.id)).row.status == "pending"
    )


async def _refusal_route(tmp_path: Path, refusal: str) -> tuple[Any, Any, UUID, Any, str]:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Refusal custody", "local")
    message = await service.add_message(session.id, "user", "Create a blob", writer_principal="route_user_message")
    tool_name = "create_blob" if refusal == "argument" else "get_pipeline_state"
    arguments = {"filename": "..", "mime_type": "text/csv", "content": "PRIVATE"} if refusal == "argument" else {}
    proposal = await _create_test_composition_proposal(
        service,
        session_id=session.id,
        tool_call_id="refusal-call",
        tool_name=tool_name,
        summary="Legacy refusal",
        rationale="Requested by the operator",
        affects=("graph",),
        arguments_json=arguments,
        arguments_redacted_json={},
        base_state_id=None,
        actor="composer-web:user:alice",
        user_message_id=message.id,
        composer_model_identifier="test-model",
        composer_model_version="test-model-v1",
        composer_provider="test",
        composer_skill_hash=stable_hash("test-skill"),
        tool_arguments_hash=stable_hash(arguments),
    )
    return app, service, session.id, proposal, f"/api/sessions/{session.id}/proposals/{proposal.id}/accept"


@pytest.mark.asyncio
@pytest.mark.parametrize(("refusal", "winner"), [("no-change", "committed"), ("argument", "rejected"), ("no-change", "rejected")])
async def test_terminal_race_preserves_winner_evidence_and_truthful_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str, winner: str
) -> None:
    app, service, session_id, proposal, endpoint = await _refusal_route(tmp_path, refusal)
    real_reject = service.reject_composition_proposal
    real_accept = service.accept_composition_proposal
    won = []

    async def race_rejection(**kwargs: Any) -> Any:
        if winner == "rejected":
            won.append(await real_reject(**{**kwargs, "actor": "user:alice"}))
        else:
            won.append(
                await real_accept(
                    session_id=session_id,
                    proposal_id=proposal.id,
                    expected_current_state_id=None,
                    state=CompositionStateData(metadata_={"name": "Winner", "description": ""}),
                    actor="user:alice",
                    session_operation_context=kwargs["session_operation_context"],
                )
            )
        return await real_reject(**kwargs)

    monkeypatch.setattr(service, "reject_composition_proposal", race_rejection)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(endpoint)
        assert response.status_code == 422, response.text
        assert "already terminal" in response.json()["detail"]["detail"]
        assert "automatically rejected" not in response.text
        events = [event for event in await service.list_proposal_events(session_id) if event.proposal_id == proposal.id]
        assert len(events) == 2
        assert events[-1].actor == "user:alice"
        assert won[0].audit_event_id == events[-1].id
        assert (await service.get_authoritative_composition_proposal(session_id=session_id, proposal_id=proposal.id)).row.status == winner
        assert (await client.post(endpoint)).status_code == 409
        assert [event for event in await service.list_proposal_events(session_id) if event.proposal_id == proposal.id] == events


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["argument", "no-change"])
@pytest.mark.parametrize("gate", ["dispatch", "before_reject", "after_reject", "lease_close"])
async def test_cancellation_drains_legacy_refusal_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str, gate: str
) -> None:
    from elspeth.web.sessions.routes.composer.pipeline_settlement import _DeferredCancellationState

    app, service, session_id, proposal, endpoint = await _refusal_route(tmp_path, refusal)
    real_reject = service.reject_composition_proposal
    real_close = SessionOperationLease.close
    real_retain = _DeferredCancellationState.retain
    entered = asyncio.Event()
    release = asyncio.Event()
    release_dispatch = threading.Event()
    loop = asyncio.get_running_loop()
    real_dispatch = routes.execute_tool
    order = []
    cancellations = []

    def gated_dispatch(*args: Any, **kwargs: Any) -> Any:
        if gate == "dispatch":
            loop.call_soon_threadsafe(entered.set)
            assert release_dispatch.wait(10), "dispatch was never released"
        return real_dispatch(*args, **kwargs)

    def capture_cancellation(state: Any, error: asyncio.CancelledError) -> None:
        cancellations.append(error)
        real_retain(state, error)

    async def gated_reject(**kwargs: Any) -> Any:
        if gate == "before_reject":
            entered.set()
            await release.wait()
        row = await real_reject(**kwargs)
        order.append("rejected")
        if gate == "after_reject":
            entered.set()
            await release.wait()
        return row

    async def gated_close(lease: SessionOperationLease) -> None:
        if gate == "lease_close":
            entered.set()
            await release.wait()
        await real_close(lease)
        order.append("closed")

    monkeypatch.setattr(service, "reject_composition_proposal", gated_reject)
    monkeypatch.setattr(routes, "execute_tool", gated_dispatch)
    monkeypatch.setattr(SessionOperationLease, "close", gated_close)
    monkeypatch.setattr(_DeferredCancellationState, "retain", capture_cancellation)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(client.post(endpoint))
        await asyncio.wait_for(entered.wait(), timeout=5)
        request.cancel("legacy-refusal-cancel")
        request.cancel("legacy-refusal-cancel")
        await asyncio.sleep(0)
        assert not request.done()
        assert "closed" not in order
        release.set()
        release_dispatch.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(request, timeout=5)
        if gate != "lease_close":
            assert caught.value is cancellations[0]
            assert caught.value.args == ("legacy-refusal-cancel",)
        assert order == ["rejected", "closed"]
        assert (await client.post(endpoint)).status_code == 409
    assert await service.get_current_state(session_id) is None
    events = [event for event in await service.list_proposal_events(session_id) if event.proposal_id == proposal.id]
    assert [event.event_type for event in events] == ["proposal.created", "proposal.rejected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["argument", "no-change"])
@pytest.mark.parametrize("error_type", [KeyError, AuditIntegrityError])
async def test_refusal_preserves_original_failure_during_cancellation_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str, error_type: type[Exception]
) -> None:
    original = error_type("exact rejection fault")
    app, service, session_id, proposal, endpoint = await _refusal_route(tmp_path, refusal)
    real_close = SessionOperationLease.close
    rejection_entered = asyncio.Event()
    release_rejection = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_failure = RuntimeError("exact cleanup failure")

    async def fail_reject(**kwargs: Any) -> Any:
        rejection_entered.set()
        await release_rejection.wait()
        raise original

    async def fail_close(lease: SessionOperationLease) -> None:
        cleanup_entered.set()
        await release_cleanup.wait()
        await real_close(lease)
        raise cleanup_failure

    monkeypatch.setattr(service, "reject_composition_proposal", fail_reject)
    monkeypatch.setattr(SessionOperationLease, "close", fail_close)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(client.post(endpoint))
        await asyncio.wait_for(rejection_entered.wait(), timeout=5)
        request.cancel("cancelled during rejection")
        release_rejection.set()
        await asyncio.wait_for(cleanup_entered.wait(), timeout=5)
        request.cancel("cancelled during cleanup")
        await asyncio.sleep(0)
        assert not request.done()
        release_cleanup.set()
        with pytest.raises(type(original)) as caught:
            await asyncio.wait_for(request, timeout=5)
    assert caught.value is original
    assert original.__notes__ == [
        "Composer proposal lease cleanup also failed with RuntimeError.",
        "Composer proposal lease cleanup also observed request cancellation.",
    ]
    assert (await service.get_authoritative_composition_proposal(session_id=session_id, proposal_id=proposal.id)).row.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["argument", "no-change"])
@pytest.mark.parametrize(
    "original", [KeyError("loaded row missing"), AuditIntegrityError("integrity fault"), ValueError("unexpected rejection failure")]
)
async def test_refusal_does_not_hide_named_rejection_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str, original: Exception
) -> None:
    app, service, session_id, proposal, endpoint = await _refusal_route(tmp_path, refusal)

    async def fail_reject(**kwargs: Any) -> Any:
        raise original

    monkeypatch.setattr(service, "reject_composition_proposal", fail_reject)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(type(original)) as caught:
            await client.post(endpoint)
    assert caught.value is original
    assert (await service.get_authoritative_composition_proposal(session_id=session_id, proposal_id=proposal.id)).row.status == "pending"
    assert [event.event_type for event in await service.list_proposal_events(session_id)] == ["proposal.created"]
